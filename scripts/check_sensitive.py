#!/usr/bin/env python3
"""Block files that must never be committed: cookies, databases, logs, secrets, audio.

Usage:  check_sensitive.py FILE...     (pre-commit passes staged files)
        check_sensitive.py --all       (every tracked + untracked-not-ignored file; used in CI)
Checks both the file NAME and its CONTENT, so a renamed cookies.txt or state.db is still caught.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

SELF = {"scripts/check_sensitive.py", "tests/test_check_sensitive.py"}

NAME_PATTERNS = [
    (r"(^|/)[^/]*cookies[^/]*\.txt$", "YouTube cookie export (it is your login session)"),
    (r"\.(db|sqlite|sqlite3)(-wal|-shm|-journal)?$", "SQLite database"),
    (r"\.(bak|bak-.*)$|\.db\.bak", "database backup"),
    (r"(^|/)\.sync\.lock$", "run lock file"),
    (r"(^|/)logs/|\.log(\.\d+)?$", "log file"),
    (r"(^|/)\.env(\..*)?$", "environment file"),
    (r"\.(pem|key)$", "private key"),
    (r"\.(m4a|flac|opus|webm|mp3|wav|ogg|m3u8|part)$", "audio/music file"),
    (r"(^|/)(master|player|\.trash)/", "music library directory"),
]

CONTENT_PATTERNS = [
    (rb"^SQLite format 3\x00", "SQLite database header"),
    (rb"# Netscape HTTP Cookie File", "Netscape cookie file"),
    (rb"\.youtube\.com\tTRUE\t", "YouTube cookie line"),
    (rb"-----BEGIN [A-Z ]*PRIVATE KEY-----", "private key"),
    (rb"sk-ant-[A-Za-z0-9_\-]{20,}", "Anthropic API key"),
]

MAX_READ = 2_000_000


def tracked_and_new() -> list[str]:
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                         capture_output=True, text=True, check=True).stdout
    return [line for line in out.splitlines() if line]


def check(path: str) -> list[str]:
    if path in SELF:
        return []
    problems = [f"{path}: {why} (by name)" for pat, why in NAME_PATTERNS if re.search(pat, path)]
    p = Path(path)
    if p.is_file():
        data = p.read_bytes()[:MAX_READ]
        problems += [f"{path}: {why} (by content)" for pat, why in CONTENT_PATTERNS
                     if re.search(pat, data, re.MULTILINE)]
    return problems


def main(argv: list[str]) -> int:
    files = tracked_and_new() if argv == ["--all"] else argv
    problems = [msg for f in files for msg in check(f)]
    if problems:
        print("Refusing: sensitive files must not be committed:\n  " + "\n  ".join(problems), file=sys.stderr)
        print("\nFix: remove them from the commit (git rm --cached <file>) and make sure .gitignore covers them.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
