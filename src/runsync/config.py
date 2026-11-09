"""Config loading. Library root holds config.yaml, state.db, master/, .trash/, logs/."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class Config:
    library: Path = field(default_factory=lambda: Path(os.environ.get("RUNSYNC_HOME", "~/Music/RunSync")).expanduser())
    cookies: Path = REPO_ROOT / "cookies.txt"
    max_downloads_per_run: int = 50
    pause_seconds: float = 5.0  # yt-dlp wiki recommends 5-10s between downloads on an account
    mass_delete_threshold: float = 0.20
    trash_days: int = 14
    near_tie_ratio: float = 0.10  # bitrates within 10% count as a tie -> prefer a natively playable format

    @property
    def master(self) -> Path:
        return self.library / "master"

    @property
    def trash(self) -> Path:
        return self.library / ".trash"

    @property
    def db_path(self) -> Path:
        return self.library / "state.db"

    @property
    def log_path(self) -> Path:
        return self.library / "logs" / "last_run.log"

    def ensure_dirs(self) -> None:
        for p in (self.library, self.master, self.trash, self.log_path.parent):
            p.mkdir(parents=True, exist_ok=True)


def load_config(library: Path | None = None) -> Config:
    cfg = Config()
    if library is not None:
        cfg.library = Path(library).expanduser()
    path = cfg.library / "config.yaml"
    if path.exists():
        data = yaml.safe_load(path.read_text()) or {}
        if "cookies" in data:
            cfg.cookies = Path(data["cookies"]).expanduser()
        for key in ("max_downloads_per_run", "pause_seconds", "mass_delete_threshold", "trash_days", "near_tie_ratio"):
            if key in data:
                setattr(cfg, key, data[key])
    return cfg
