"""The card: mirror the selected playlists onto a registered microSD card without ever hurting it.

Safety model
- A card is written only if its volume UUID matches a registered device. The Mac's own disk is never accepted.
- The manifest (device_tracks) records exactly what runsync put on the card. Only manifest files are ever deleted,
  so anything else on the card is never touched, and nothing is ever listed-and-cleaned.
- Every file is written to `<name>.runsync-tmp`, fsynced, verified and renamed into place: a final name never holds
  a partial file. A run that was interrupted (card pulled) is finished by the next run: the plan is recomputed from
  the manifest, and leftover temp files are removed.
- The plan is read-only and has an id; applying needs the id of the plan the owner saw (see cli.card).
- The last run can be undone from the journal.
"""
from __future__ import annotations

import hashlib
import logging
import os
import plistlib
import shutil
import sqlite3
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .dedupe import winner_of
from .player import safe_filename

log = logging.getLogger("runsync.card")

TMP_SUFFIX = ".runsync-tmp"
SPACE_MARGIN = 100 * 1024 * 1024    # keep this much free on the card
JUNK = (".DS_Store", ".Spotlight-V100", ".fseventsd", ".Trashes", ".TemporaryItems", ".VolumeIcon.icns")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def is_junk(name: str) -> bool:
    return name in JUNK or name.startswith("._")


# ---- volumes ----------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class VolumeInfo:
    mount: Path
    name: str
    uuid: str
    fs: str
    free_bytes: int
    dev: int          # st_dev of the mount: tells whether it is the same disk as the library
    internal: bool = False    # a fixed disk inside the Mac (Recovery, Preboot ...), not something you pull out
    removable: bool = True


def parse_volume_plist(data: dict, mount: Path, free_bytes: int, dev: int) -> VolumeInfo | None:
    """From `diskutil info -plist`. None when there is no volume UUID (not a mounted filesystem we can identify)."""
    uuid = data.get("VolumeUUID")
    if not uuid:
        return None
    return VolumeInfo(Path(mount), data.get("VolumeName") or Path(mount).name, str(uuid),
                      data.get("FilesystemName") or data.get("FilesystemType") or "?", free_bytes, dev,
                      bool(data.get("Internal", False)), bool(data.get("RemovableMedia", data.get("Removable", True))))


def volume_info(mount: Path) -> VolumeInfo | None:
    """Read-only: asks macOS about an already mounted volume. Never mounts, creates or changes anything."""
    try:
        out = subprocess.run(["diskutil", "info", "-plist", str(mount)], capture_output=True, timeout=20,
                             check=True).stdout
        data = plistlib.loads(out)
        return parse_volume_plist(data, mount, shutil.disk_usage(mount).free, os.stat(mount).st_dev)
    except (OSError, subprocess.SubprocessError, plistlib.InvalidFileException, ValueError):
        return None


def candidate_volumes(library: Path, volumes_dir: Path = Path("/Volumes")) -> list[VolumeInfo]:
    """Mounted volumes that could be a card: everything under /Volumes except disks that hold the library."""
    out = []
    own = os.stat(library).st_dev
    try:
        entries = sorted(volumes_dir.iterdir())
    except OSError:
        return []
    for p in entries:
        if p.is_symlink() or not p.is_dir():   # the boot volume is a symlink to /
            continue
        info = volume_info(p)
        if info and info.dev != own and not (info.internal and not info.removable):
            out.append(info)
    return out


# ---- devices ----------------------------------------------------------------------------------------------------

def register_device(conn: sqlite3.Connection, info: VolumeInfo, name: str, library: Path, subpath: str = "Music") -> int:
    """Remember a card by its UUID. Refuses the disk that holds the library (the Mac's own disk)."""
    if info.dev == os.stat(library).st_dev:
        raise ValueError(f"{info.mount} is the same disk as your library: refusing to treat it as a card")
    if conn.execute("SELECT 1 FROM devices WHERE volume_uuid = ?", (info.uuid,)).fetchone():
        raise ValueError("this card is already registered")
    cur = conn.execute("INSERT INTO devices (name, volume_uuid, subpath, registered_at) VALUES (?,?,?,?)",
                       (name, info.uuid, subpath, _now()))
    conn.commit()
    return cur.lastrowid


def devices(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM devices ORDER BY id").fetchall()


def find_card(conn: sqlite3.Connection, volumes: list[VolumeInfo]) -> tuple[sqlite3.Row, VolumeInfo] | None:
    """A registered device that is mounted right now. Matching is by UUID only."""
    by_uuid = {v.uuid: v for v in volumes}
    for d in devices(conn):
        if d["volume_uuid"] in by_uuid:
            return d, by_uuid[d["volume_uuid"]]
    return None


# ---- the plan ---------------------------------------------------------------------------------------------------

@dataclass
class Item:
    video_id: str
    filename: str
    src: Path
    size: int
    mtime_ns: int
    seconds: int = 0
    label: str = ""


@dataclass
class Removal:
    video_id: str
    filename: str
    size: int


@dataclass
class CardPlan:
    adds: list[Item] = field(default_factory=list)
    updates: list[Item] = field(default_factory=list)
    removes: list[Removal] = field(default_factory=list)
    unchanged: int = 0
    playlists: dict[str, list[str]] = field(default_factory=dict)   # playlist file name -> audio file names in order
    info: dict[str, Item] = field(default_factory=dict)             # every wanted audio file
    adopted: list[Item] = field(default_factory=list)   # already on the card, byte-identical: recorded, not copied
    conflicts: list[str] = field(default_factory=list)  # a different file with our name that is not ours: never overwritten
    free_bytes: int = 0
    manifest_size: int = 0
    blocked: str | None = None   # "conflict" | "space" | "guard"

    @property
    def add_bytes(self) -> int:
        return sum(i.size for i in self.adds) + sum(i.size for i in self.updates)

    @property
    def changes(self) -> int:
        return len(self.adds) + len(self.updates) + len(self.removes)


def card_plan_id(plan: CardPlan) -> str:
    """Names exactly which files a plan would write and delete, like dedupe.plan_id."""
    lines = sorted([f"+{i.filename}:{i.size}" for i in plan.adds] + [f"~{i.filename}:{i.size}" for i in plan.updates]
                   + [f"-{r.filename}" for r in plan.removes] + [f"={i.filename}" for i in plan.adopted])
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()[:12]


def wanted_tracks(conn: sqlite3.Connection) -> tuple[dict[str, Item], dict[str, list[str]]]:
    """What should be on the card: tracks of the selected playlists (merged duplicates mapped to the track that was
    kept) that have a player copy. Returns ({filename: Item}, {playlist file name: [filenames in order]})."""
    items: dict[str, Item] = {}
    lists: dict[str, list[str]] = {}
    used: set[str] = set()
    for p in conn.execute("SELECT id, title FROM playlists WHERE selected = 1 ORDER BY title, id"):
        pname = (safe_filename(p["title"]) or f"Playlist {p['id']}") + ".m3u8"
        while pname in used:
            pname = pname.replace(".m3u8", f" ({p['id']}).m3u8")
        used.add(pname)
        seen: list[str] = []
        for r in conn.execute("SELECT video_id FROM playlist_items WHERE playlist_id = ? ORDER BY position", (p["id"],)):
            vid = winner_of(conn, r["video_id"])
            t = conn.execute("SELECT t.player_path, m.artist, m.title, t.probe_duration_s FROM tracks t "
                             "LEFT JOIN metadata m USING (video_id) WHERE t.video_id = ? AND t.status = 'downloaded' "
                             "AND t.player_path IS NOT NULL", (vid,)).fetchone()
            if not t or not Path(t["player_path"]).exists():
                continue
            src = Path(t["player_path"])
            if src.name not in items:
                st = src.stat()
                items[src.name] = Item(vid, src.name, src, st.st_size, st.st_mtime_ns, int(t["probe_duration_s"] or 0),
                                       f"{t['artist'] or ''} - {t['title'] or ''}".strip(" -"))
            if src.name not in seen:
                seen.append(src.name)
        lists[pname] = seen
    return items, lists


def build_card_plan(conn: sqlite3.Connection, device_id: int, mount: Path, subpath: str, free_bytes: int,
                    threshold: float = 0.20) -> CardPlan:
    """Read-only. Compares what should be on the card with the manifest and with what is really there."""
    wanted, lists = wanted_tracks(conn)
    folder = Path(mount) / subpath
    manifest = {r["filename"]: r for r in conn.execute(
        "SELECT filename, video_id, size, sha256, src_mtime_ns FROM device_tracks WHERE device_id = ? AND video_id != ''",
        (device_id,))}
    plan = CardPlan(playlists=lists, info=wanted, free_bytes=free_bytes, manifest_size=len(manifest))
    for name, item in sorted(wanted.items()):
        row = manifest.get(name)
        on_card = (folder / name).is_file()
        if row is None and on_card:                # a file we did not put there
            theirs = folder / name
            if theirs.stat().st_size == item.size and _sha(theirs) == _sha(item.src):
                plan.adopted.append(item)          # the very same file: fine, just remember it
            else:
                plan.conflicts.append(name)        # someone else's file: never overwritten
        elif row is None or not on_card:
            plan.adds.append(item)                 # new, or the owner removed it from the card: put it back
        elif row["src_mtime_ns"] != item.mtime_ns or row["size"] != item.size:
            plan.updates.append(item)              # the player copy was rebuilt (re-tagged)
        else:
            plan.unchanged += 1
    for name, row in sorted(manifest.items()):
        if name not in wanted:
            plan.removes.append(Removal(row["video_id"], name, row["size"]))
    if plan.manifest_size and len(plan.removes) > threshold * plan.manifest_size:
        plan.blocked = "guard"                     # applying needs --force
    if plan.conflicts:
        plan.blocked = "conflict"
    if plan.add_bytes + SPACE_MARGIN > free_bytes + sum(r.size for r in plan.removes):
        plan.blocked = "space"
    return plan


# ---- applying ---------------------------------------------------------------------------------------------------

def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def copy_verified(src: Path, folder: Path, filename: str) -> tuple[int, str]:
    """src -> folder/filename through a temp name: flush, fsync, size and checksum verified before the rename."""
    tmp = folder / f"{filename}{TMP_SUFFIX}"
    h = hashlib.sha256()
    try:
        with src.open("rb") as r, tmp.open("wb") as w:
            while chunk := r.read(1 << 20):
                h.update(chunk)
                w.write(chunk)
            w.flush()
            os.fsync(w.fileno())
        if tmp.stat().st_size != src.stat().st_size or _sha(tmp) != h.hexdigest():
            raise OSError(f"{filename}: what was written does not match the source")
        os.replace(tmp, folder / filename)
    finally:
        tmp.unlink(missing_ok=True)
    return src.stat().st_size, h.hexdigest()


def recover(conn: sqlite3.Connection, device_id: int, mount: Path, subpath: str) -> int:
    """After an interrupted run (card pulled): drop leftover temp files and mark the run interrupted."""
    folder = Path(mount) / subpath
    n = 0
    if folder.is_dir():
        for p in folder.glob(f"*{TMP_SUFFIX}"):
            p.unlink(missing_ok=True)
            n += 1
    conn.execute("UPDATE card_runs SET status = 'interrupted', finished_at = ? WHERE device_id = ? AND status = 'running'",
                 (_now(), device_id))
    conn.commit()
    return n


@dataclass
class ApplyResult:
    added: int = 0
    updated: int = 0
    removed: int = 0
    bytes: int = 0
    playlists: int = 0


def write_playlists(conn: sqlite3.Connection, device_id: int, folder: Path, plan: CardPlan) -> int:
    """One M3U8 per selected playlist, next to the audio: UTF-8, relative names, only tracks that are on the card.
    Playlist files of playlists that are no longer selected are removed (they are in the manifest)."""
    on_card = {r["filename"] for r in conn.execute(
        "SELECT filename FROM device_tracks WHERE device_id = ? AND video_id != ''", (device_id,))}
    keep = set(plan.playlists)
    for r in conn.execute("SELECT filename FROM device_tracks WHERE device_id = ? AND video_id = ''", (device_id,)).fetchall():
        if r["filename"] not in keep:
            (folder / r["filename"]).unlink(missing_ok=True)
            conn.execute("DELETE FROM device_tracks WHERE device_id = ? AND filename = ?", (device_id, r["filename"]))
    for pname, files in plan.playlists.items():
        lines = ["#EXTM3U"]
        for f in files:
            if f in on_card:
                it = plan.info[f]
                lines += [f"#EXTINF:{it.seconds},{it.label}", f]
        data = ("\n".join(lines) + "\n").encode("utf-8")
        tmp = folder / f"{pname}{TMP_SUFFIX}"
        try:
            with tmp.open("wb") as w:
                w.write(data)
                w.flush()
                os.fsync(w.fileno())
            os.replace(tmp, folder / pname)
        finally:
            tmp.unlink(missing_ok=True)
        conn.execute("INSERT OR REPLACE INTO device_tracks (device_id, filename, video_id, size, sha256, src_mtime_ns, "
                     "synced_at) VALUES (?,?,?,?,?,?,?)",
                     (device_id, pname, "", len(data), hashlib.sha256(data).hexdigest(), 0, _now()))
    conn.commit()
    return len(plan.playlists)


def apply_card_plan(conn: sqlite3.Connection, device_id: int, mount: Path, subpath: str, plan: CardPlan,
                    force: bool = False, copy: Callable[[Path, Path, str], tuple[int, str]] = copy_verified
                    ) -> ApplyResult:
    """Do the plan. Refuses a blocked plan before the first byte is written. `copy` is injectable for tests."""
    if plan.blocked == "space":
        raise OSError("not enough free space on the card for this plan")
    if plan.blocked == "conflict":
        raise OSError(f"{len(plan.conflicts)} file(s) on the card have our names but are not ours: "
                      + ", ".join(plan.conflicts[:3]) + "; rename or remove them first")
    if plan.blocked == "guard" and not force:
        raise OSError(f"{len(plan.removes)} of {plan.manifest_size} files would be removed (over the guard); "
                      "re-run with --force after checking")
    folder = Path(mount) / subpath
    folder.mkdir(parents=True, exist_ok=True)
    recover(conn, device_id, mount, subpath)
    run_id = conn.execute("INSERT INTO card_runs (device_id, started_at, status, plan_id) VALUES (?,?,'running',?)",
                          (device_id, _now(), card_plan_id(plan))).lastrowid
    conn.commit()
    res = ApplyResult()
    for it in plan.adopted:
        conn.execute("INSERT OR REPLACE INTO device_tracks (device_id, filename, video_id, size, sha256, src_mtime_ns, "
                     "synced_at) VALUES (?,?,?,?,?,?,?)",
                     (device_id, it.filename, it.video_id, it.size, _sha(it.src), it.mtime_ns, _now()))
    for op, items in (("add", plan.adds), ("update", plan.updates)):
        for it in items:
            size, sha = copy(it.src, folder, it.filename)
            conn.execute("INSERT OR REPLACE INTO device_tracks (device_id, filename, video_id, size, sha256, "
                         "src_mtime_ns, synced_at) VALUES (?,?,?,?,?,?,?)",
                         (device_id, it.filename, it.video_id, size, sha, it.mtime_ns, _now()))
            conn.execute("INSERT INTO card_journal (run_id, op, video_id, filename, src, size) VALUES (?,?,?,?,?,?)",
                         (run_id, op, it.video_id, it.filename, str(it.src), size))
            conn.commit()
            res.bytes += size
            if op == "add":
                res.added += 1
            else:
                res.updated += 1
    for r in plan.removes:   # only files the manifest says runsync put there
        (folder / r.filename).unlink(missing_ok=True)
        conn.execute("DELETE FROM device_tracks WHERE device_id = ? AND filename = ?", (device_id, r.filename))
        src = conn.execute("SELECT player_path FROM tracks WHERE video_id = ?", (r.video_id,)).fetchone()
        conn.execute("INSERT INTO card_journal (run_id, op, video_id, filename, src, size) VALUES (?,?,?,?,?,?)",
                     (run_id, "remove", r.video_id, r.filename, src["player_path"] if src else None, r.size))
        conn.commit()
        res.removed += 1
    res.playlists = write_playlists(conn, device_id, folder, plan)
    conn.execute("UPDATE card_runs SET status = 'done', finished_at = ? WHERE id = ?", (_now(), run_id))
    conn.commit()
    return res


def undo_last_run(conn: sqlite3.Connection, device_id: int, mount: Path, subpath: str, plan_after: CardPlan | None = None
                  ) -> ApplyResult:
    """Reverse the last finished run: remove what it added, put back what it removed (from player/).
    Rebuilt (updated) files are not reverted. Playlists are rewritten to match."""
    run = conn.execute("SELECT id FROM card_runs WHERE device_id = ? AND status = 'done' ORDER BY id DESC LIMIT 1",
                       (device_id,)).fetchone()
    if not run:
        raise ValueError("nothing to undo: no finished run for this card")
    folder = Path(mount) / subpath
    res = ApplyResult()
    for j in conn.execute("SELECT op, video_id, filename, src, size FROM card_journal WHERE run_id = ?", (run["id"],)).fetchall():
        if j["op"] == "add":
            (folder / j["filename"]).unlink(missing_ok=True)
            conn.execute("DELETE FROM device_tracks WHERE device_id = ? AND filename = ?", (device_id, j["filename"]))
            res.removed += 1
        elif j["op"] == "remove" and j["src"] and Path(j["src"]).is_file():
            src = Path(j["src"])
            size, sha = copy_verified(src, folder, j["filename"])
            conn.execute("INSERT OR REPLACE INTO device_tracks (device_id, filename, video_id, size, sha256, "
                         "src_mtime_ns, synced_at) VALUES (?,?,?,?,?,?,?)",
                         (device_id, j["filename"], j["video_id"], size, sha, src.stat().st_mtime_ns, _now()))
            res.added += 1
            res.bytes += size
        conn.commit()
    conn.execute("UPDATE card_runs SET status = 'undone', finished_at = ? WHERE id = ?", (_now(), run["id"]))
    conn.commit()
    if plan_after is not None:
        res.playlists = write_playlists(conn, device_id, folder, plan_after)
    return res


def card_state(conn: sqlite3.Connection, library: Path, volumes: list[VolumeInfo]) -> dict:
    """Facts for the CLI and the skill. Pure reads."""
    devs = devices(conn)
    if not devs:
        return {"state": "unregistered", "candidates": [_vol(v) for v in volumes]}
    found = find_card(conn, volumes)
    if not found:
        return {"state": "not_inserted", "device": devs[0]["name"]}
    d, v = found
    plan = build_card_plan(conn, d["id"], v.mount, d["subpath"], v.free_bytes)
    return {"state": "blocked" if plan.blocked else "ready", "device": d["name"], "mount": str(v.mount), "fs": v.fs,
            "free_bytes": v.free_bytes, "adds": len(plan.adds), "updates": len(plan.updates), "removes": len(plan.removes),
            "unchanged": plan.unchanged, "adopted": len(plan.adopted), "conflicts": plan.conflicts[:10],
            "add_bytes": plan.add_bytes, "blocked": plan.blocked,
            "playlists": [{"name": n, "tracks": len(f)} for n, f in plan.playlists.items()],
            "plan_id": card_plan_id(plan), "device_id": d["id"]}


def _vol(v: VolumeInfo) -> dict:
    return {"mount": str(v.mount), "name": v.name, "fs": v.fs, "free_bytes": v.free_bytes, "uuid": v.uuid}
