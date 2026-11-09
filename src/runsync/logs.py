"""Logging setup. Files live in <library>/logs/ (outside the repo); never log cookie values."""
from __future__ import annotations

import logging
import platform
import sys
from logging.handlers import RotatingFileHandler

from .config import Config

FMT = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")


COOKIE_STATE = {"rotated": False}
ROTATED_MARKER = "cookies are no longer valid"


def cookies_rotated() -> bool:
    """True if yt-dlp reported (this process) that YouTube invalidated the cookie file."""
    return COOKIE_STATE["rotated"]


def reset_cookie_state() -> None:
    COOKIE_STATE["rotated"] = False


class YtdlpLogger:
    """Routes yt-dlp's own output into our log file instead of the terminal."""

    def __init__(self) -> None:
        self.log = logging.getLogger("runsync.ytdlp")

    def debug(self, msg: str) -> None:
        (self.log.debug if msg.startswith("[debug] ") else self.log.info)(msg.removeprefix("[debug] "))

    def info(self, msg: str) -> None:
        self.log.info(msg)

    def warning(self, msg: str) -> None:
        if ROTATED_MARKER in msg.lower():
            COOKIE_STATE["rotated"] = True
        self.log.warning(msg)

    def error(self, msg: str) -> None:
        self.log.error(msg)


def setup_logging(cfg: Config, command: str, verbose: bool = False) -> logging.Logger:
    """runsync.log: rotating history (5 x 1MB). last_run.log: this run only. Both at DEBUG."""
    cfg.log_path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("runsync")
    root.setLevel(logging.DEBUG)
    root.handlers.clear()
    root.propagate = False

    history = RotatingFileHandler(cfg.log_path.parent / "runsync.log", maxBytes=1_000_000, backupCount=5,
                                  encoding="utf-8")
    last = logging.FileHandler(cfg.log_path, mode="w", encoding="utf-8")
    for h in (history, last):
        h.setLevel(logging.DEBUG)
        h.setFormatter(FMT)
        root.addHandler(h)
    if verbose:
        console = logging.StreamHandler(sys.stderr)
        console.setLevel(logging.DEBUG)
        console.setFormatter(FMT)
        root.addHandler(console)

    try:
        import yt_dlp
        ytdlp_version = yt_dlp.version.__version__
    except Exception:  # noqa: BLE001
        ytdlp_version = "unknown"
    root.info("=== runsync %s | argv=%s", command, " ".join(sys.argv[1:]))
    root.info("python %s | %s | yt-dlp %s", platform.python_version(), platform.platform(), ytdlp_version)
    root.info("library=%s cookies=%s max_downloads=%s pause=%ss", cfg.library, cfg.cookies,
              cfg.max_downloads_per_run, cfg.pause_seconds)
    return root
