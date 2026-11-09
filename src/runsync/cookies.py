"""Cookie file health check. Never prints cookie values."""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

LOGIN_COOKIES = {"SID", "__Secure-1PSID", "__Secure-3PSID", "SAPISID", "__Secure-3PAPISID", "LOGIN_INFO"}


@dataclass
class CookieStatus:
    state: str  # ok | missing | expired | no_login
    detail: str = ""

    @property
    def usable(self) -> bool:
        return self.state == "ok"


def check_cookies(path: Path, now: float | None = None) -> CookieStatus:
    now = time.time() if now is None else now
    path = Path(path)
    if not path.exists():
        return CookieStatus("missing", f"{path} not found")
    expiries: list[int] = []
    for line in path.read_text(errors="ignore").splitlines():
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif line.startswith("#") or not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 7 or "youtube.com" not in parts[0]:
            continue
        if parts[5] in LOGIN_COOKIES:
            try:
                expiries.append(int(parts[4]))
            except ValueError:
                continue
    if not expiries:
        return CookieStatus("no_login", "no YouTube login cookies in file (not logged in when exported?)")
    # 0 = session cookie (no expiry recorded); treat as valid
    if all(e != 0 and e < now for e in expiries):
        return CookieStatus("expired", "YouTube login cookies have expired")
    return CookieStatus("ok")
