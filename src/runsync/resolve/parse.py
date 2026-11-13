"""Title parser: strip junk, split Artist/Title, classify pipe segments. Pure functions, no I/O."""
from __future__ import annotations

import re

from .model import Candidate

JUNK_WORDS = (
    r"official(?:\s+(?:music|lyric|lyrics|audio|hd))?\s*(?:video|audio|mv|visuali[sz]er)?",
    r"(?:music|lyric|lyrics|lyrical|video|audio)\s+video", r"lyric(?:s|al)?(?:\s+video)?", r"with\s+lyrics",
    r"(?:full\s+)?(?:video\s+)?song", r"full\s+video", r"video\s+song", r"visuali[sz]er", r"m/?v", r"hd(?:\s+upgrade)?",
    r"hq", r"4k", r"1080p", r"720p", r"explicit", r"clean", r"audio", r"best\s+love\s+song",
    r"(?:new|latest|superhit|blockbuster|top|best)\s+(?:punjabi|hindi|bollywood)?\s*songs?(?:\s+of)?(?:\s+\d{4})?",
    r"new\s+song\s+\d{4}", r"(?:latest|new|superhit)\s+punjabi\s+songs?", r"punjabi\s+songs?", r"bollywood\s+songs?",
    r"(?:bollywood\s+)?cover\s+songs?", r"unplugged\s+cover\s+songs?", r"lyric\s+video(?:\s+\d{4})?", r"\d{4}\s+song",
)
JUNK_RE = re.compile(r"^\W*(?:" + "|".join(JUNK_WORDS) + r")\W*$", re.IGNORECASE)
# a group like "(Official Video)" or "[4K]" or "{full song}"
GROUP_RE = re.compile(r"[\(\[\{]([^\)\]\}]*)[\)\]\}]")
LABELS = {
    "t-series", "yrf", "sony music india", "speed records", "zee music company", "saregama music", "tips official",
    "times music", "humble music", "ishtar punjabi", "warner records vault", "white hill music", "t-series apna punjab",
    "lahari music", "eros now music", "vevo", "volume", "gully boy pkg", "basekSoch".lower(), "bollywood classics",
}
FEAT_RE = re.compile(r"\s*[\(\[]?\s*\b(?:feat\.?|ft\.?|featuring)\s+([^\)\]\|]+?)\s*[\)\]]?\s*(?=$|[\(\[\|]|\s[-–—]\s)", re.IGNORECASE)
SONG_BY_RE = re.compile(r"(?:song|sung|singer|singers)\s+by\s+([^{\(\[\|]+)", re.IGNORECASE)
DASH_RE = re.compile(r"\s+[-–—]\s*|\s*[-–—]\s+")
VERSION_MARKERS = {
    "live": r"\blive\b|\s@\s", "remix": r"\bre-?mix\b|\bmashup\b", "cover": r"\bcover\b", "acoustic": r"\bacoustic\b",
    "instrumental": r"\binstrumental\b", "unplugged": r"\bunplugged\b", "slowed": r"\bslowed\b|\breverb\b|\blofi\b",
    "karaoke": r"\bkaraoke\b", "remaster": r"re-?master",
}
NOISE_ARTISTS = {"backing vocals", "various artists", "unknown"}


def normalize(s: str) -> str:
    s = (s or "").replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    return re.sub(r"\s+", " ", s).strip()


def key(s: str) -> str:
    """Comparison key: lowercase letters/digits only."""
    return re.sub(r"[^\w]+", "", normalize(s).lower())


def tidy_case(s: str) -> str:
    """ALL CAPS -> Title Case (kept for short acronyms and things like AC/DC)."""
    if s.isupper() and len(s) >= 5 and "/" not in s:
        return s.title()
    return s


def channel_name(channel: str | None) -> str:
    return normalize(re.sub(r"\s*-\s*Topic$|VEVO$|\bOfficial$", "", channel or "", flags=re.IGNORECASE))


def is_label(channel: str | None) -> bool:
    c = channel_name(channel).lower()
    return c in LABELS or c.endswith((" records", " music", " films", " entertainment", " pkg"))


def split_names(s: str, strict: bool = False, amp: bool = False) -> list[str]:
    """Split a list of names on commas. strict=True also splits on | (YouTube's own artist fields).
    amp=True also splits on & / and, only where the title says "song by A & B": elsewhere "Above & Beyond" is one act."""
    sep = r"\s*(?:,|;|\|)\s*" if strict else (r"\s*(?:,|&|\band\b|;)\s*" if amp else r"\s*(?:,|;)\s*")
    parts = re.split(sep, s)
    out: list[str] = []
    for p in parts:
        p = tidy_case(normalize(p))
        if p and p.lower() not in NOISE_ARTISTS and key(p) not in {key(o) for o in out}:
            out.append(p)
    return out


def strip_junk_groups(s: str) -> str:
    """Remove bracketed groups that are only junk words ("(Official Video)", "[4K]")."""
    def repl(m: re.Match) -> str:
        inner = m.group(1).strip()
        return "" if JUNK_RE.match(inner) else m.group(0)
    return normalize(GROUP_RE.sub(repl, s))


def versions_in(s: str) -> list[str]:
    return [name for name, pat in VERSION_MARKERS.items() if re.search(pat, s, re.IGNORECASE)]


def split_remaster(title: str) -> tuple[str, int | None, bool]:
    """Remove "(2013 Remaster)" style tails. Returns (title, year hint, was_remaster)."""
    m = re.search(r"\s*[\(\[\-–]\s*(?:(\d{4})\s+)?re-?master(?:ed)?(?:\s+(\d{4}))?\s*[\)\]]?\s*$", title, re.IGNORECASE)
    if not m:
        return title, None, False
    year = m.group(1) or m.group(2)
    return normalize(title[: m.start()]), int(year) if year else None, True


def split_featured(title: str) -> tuple[str, list[str]]:
    m = FEAT_RE.search(title)
    if not m:
        return title, []
    return normalize(title[: m.start()] + title[m.end():]), split_names(m.group(1))


def clean_title(raw: str) -> tuple[str, list[str], int | None]:
    """(clean title, featured artists, remaster-year hint) from one title fragment."""
    t = strip_junk_groups(normalize(raw))
    t = re.sub(r"\s*[-–—]\s*(?:best\s+love\s+song|official.*|lyric.*)$", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+(?:hd|hq|4k)(?=\s*(?:[(\[]|$))", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\(\s+", "(", t)
    t = re.sub(r"\s+\)", ")", t)
    t = re.sub(r"(?:^|\s+)(?:full\s+(?:video\s+)?song|video\s+song|full\s+video|video)(?:\s+with\s+lyrics)?$", "", t,
               flags=re.IGNORECASE)
    t = re.sub(r"\s+lyrics?$", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s+(?:official\s+)?(?:music\s+)?video$", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*[(\[][^()\[\]]*\s@\s[^()\[\]]*[)\]]\s*$", "", t)   # "(ASOT500 @ Den Bosch)": venue, not title
    t, featured = split_featured(t)
    t, year, _ = split_remaster(t)
    return tidy_case(normalize(t).strip(" -–—:|")), featured, year


def _junk_segment(seg: str, channel: str | None) -> bool:
    s = strip_junk_groups(seg)
    c = channel_name(channel)
    if (not s) or JUNK_RE.match(s) or key(s) == key(c) or s.lower() in LABELS:
        return True
    return bool(re.search(r"\bsongs?\b", s, re.IGNORECASE) and re.search(
        r"\b(?:punjabi|hindi|bollywood|new|latest|best|superhit|top|dance|love|party|romantic|blockbuster)\b", s, re.IGNORECASE))


def _good_hint(name: str) -> bool:
    return bool(name) and not re.search(r"[()#\d]", name) and len(name.split()) <= 5


def parse(raw_title: str, channel: str | None = None) -> Candidate:
    """Best deterministic reading of an upload title. Confidence reflects how certain the pattern is."""
    raw = re.sub(r"\s*#\S+", "", normalize(raw_title))
    raw = re.sub(r"^(?:official|new|latest)\s*:\s*", "", raw, flags=re.IGNORECASE)
    vers = versions_in(raw)
    segs = [s.strip() for s in raw.split("|")]
    pipe_style = len(segs) > 1
    hints: list[str] = []
    alts: list[str] = []
    album: str | None = None
    artists: list[str] = []
    featured: list[str] = []
    year: int | None = None
    conf = 0.4
    note = ""

    first = strip_junk_groups(segs[0])
    # "(From "Movie")" names the film
    m = re.search(r"\s*[\(\[]\s*from\s+[\"'](.+?)[\"']\s*[\)\]]", first, re.IGNORECASE)
    if m:
        album = normalize(m.group(1))
        first = normalize(first[: m.start()] + first[m.end():])
    # "Song by X & Y" names the singers outright (in the first segment or a later one)
    for i, seg in enumerate(segs):
        sm = SONG_BY_RE.search(first if i == 0 else seg)
        if sm:
            artists = split_names(sm.group(1), amp=True)
            conf = 0.78
            if i == 0:
                first = re.sub(r"\s+cover$", "", normalize(first[: sm.start()]), flags=re.IGNORECASE)
            break
    # text after a removed junk group is usually names:  "Lahore (Official Video) Bhushan Kumar"
    if pipe_style:
        jm = next((g for g in GROUP_RE.finditer(segs[0]) if JUNK_RE.match(g.group(1).strip())), None)
        if jm and segs[0][jm.end():].strip():
            tail_txt = segs[0][jm.end():].strip()
            first = strip_junk_groups(segs[0][: jm.start()])
            tail_txt, ft = split_featured(tail_txt)
            featured += ft
            hints += split_names(tail_txt)

    quoted = re.match(r"^[\"'](.+?)[\"']\s*(.*)$", first)
    qa = re.match(r'^(.+?)\s+"(.+?)"(.*)$', first)
    dash = DASH_RE.split(first, maxsplit=1)
    song_names = re.match(r"^(.+?)\s+(?:full\s+)?(?:video\s+)?song\s+(\S.*)$", first, re.IGNORECASE)
    if quoted:                                         # "Tum Hi Ho" Aashiqui 2 Full Song With Lyrics
        title, f, year = clean_title(quoted.group(1))
        featured += f
        rest = quoted.group(2)
        rest = "" if JUNK_RE.match(rest) else clean_title(rest)[0]
        album = album or (rest or None)
        conf = max(conf, 0.5)
    elif qa and not pipe_style:                        # Andy Grammer "Fine By Me" Official Video
        artists = artists or split_names(qa.group(1))
        title, f, year = clean_title(qa.group(2))
        featured += f
        conf = 0.8
    elif ":" in first and pipe_style:                   # "Luka Chuppi: Duniyaa" / "Hardy Sandhu: HORNN BLOW"
        prefix, _, song = first.partition(":")
        title, f, year = clean_title(song)
        featured += f
        prefix = clean_title(prefix)[0]
        if key(prefix) == key(channel_name(channel)):
            artists = artists or [prefix]
            conf = max(conf, 0.8)
        elif prefix:
            hints.insert(0, prefix)                  # a film or the artist: MusicBrainz decides
            alts.append(prefix)
    elif pipe_style and song_names and len(dash) < 2:  # "Blue Eyes Full Video Song Yo Yo Honey Singh"
        title, f, year = clean_title(song_names.group(1))
        featured += f
        hints += split_names(song_names.group(2))
    elif len(dash) == 2:
        a, b = strip_junk_groups(dash[0]), strip_junk_groups(dash[1])
        chan = key(channel_name(channel))
        b_core = key(re.sub(r"[\(\[].*?[\)\]]", "", clean_title(b)[0]))
        a_core = key(re.sub(r"[\(\[].*?[\)\]]", "", clean_title(a)[0]))
        if JUNK_RE.match(b):                          # "Humari Adhuri Kahani - Full Song | ..."
            title, f, year = clean_title(a)
            featured += f
        elif pipe_style and FEAT_RE.search(" " + b + " ") or pipe_style and re.search(r"\bfeat\b|\bft\b", b, re.IGNORECASE):
            title, f, year = clean_title(a)                # "Naah - Harrdy Sandhu Feat. Nora Fatehi | ..."
            featured += f
            lead, ft = split_featured(b)
            artists = artists or split_names(lead)
            featured += ft
            conf = max(conf, 0.75)
        elif pipe_style:                               # Indian film convention: "Song - Movie | singers | cast"
            title, f, year = clean_title(a)
            featured += f
            album = album or clean_title(b)[0] or None
            hints.append(clean_title(b)[0])             # ...or "Song - Artist": MusicBrainz decides
        elif chan and b_core == chan:                  # "Song - Artist", channel is the artist
            title, f, year = clean_title(a)
            featured += f
            artists = artists or [re.sub(r"\s*[\(\[].*?[\)\]]", "", clean_title(b)[0]).strip()]
            conf = 0.85
        elif chan and a_core == chan:
            artists = artists or [re.sub(r"\s*[\(\[].*?[\)\]]", "", clean_title(a)[0]).strip()]
            title, f, year = clean_title(b)
            featured += f
            conf = 0.85
        else:                                          # default convention: "Artist - Title"
            artists = artists or split_names(clean_title(a)[0])
            title, f, year = clean_title(b)
            featured += f
            alts.append(clean_title(a)[0])               # the other reading, for MusicBrainz to settle
            conf = 0.8
    else:
        title, f, year = clean_title(first)
        featured += f
        if not pipe_style and not artists:
            c = channel_name(channel)
            if c and not is_label(channel):
                artists = [tidy_case(c)]
                conf = 0.5
                note = "artist taken from the channel name"

    # remaining pipe segments: junk, or names (singers / cast / composer) kept as hints
    for seg in segs[1:]:
        if _junk_segment(seg, channel) or SONG_BY_RE.search(seg):
            continue
        s, ft = split_featured(strip_junk_groups(re.sub(r"\{.*?\}", "", seg)))
        names = [h for h in split_names(s) if _good_hint(h)]
        if ft and names and not artists:               # "Sidhu Moose Wala ft. BYG BYRD": left side is the artist
            artists = names
            conf = max(conf, 0.7)
        else:
            hints += names
        featured += ft

    if artists:
        artists += [f for f in featured if key(f) not in {key(a) for a in artists}]
    else:
        hints = featured + hints                       # featured guests with no known lead are only hints
    if not title:
        title, conf = tidy_case(raw), 0.2
    if not artists and conf > 0.3:
        conf = 0.4
    taken = {key(a) for a in artists}
    return Candidate(tier="parser", title=title, artists=artists, album=album, year=year, confidence=conf,
                     versions=vers, hints=[h for h in dict.fromkeys(hints) if key(h) not in taken | {key(title)}],
                     alts=alts, note=note)
