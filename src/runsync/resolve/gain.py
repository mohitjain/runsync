"""ReplayGain / loudness analysis with ffmpeg (ebur128). Read-only: nothing is re-encoded or modified."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

REFERENCE_LUFS = -18.0  # ReplayGain 2.0


def parse_ebur128(stderr: str) -> dict | None:
    """Pull integrated loudness and true peak from ffmpeg's ebur128 summary."""
    m_i = re.search(r"Integrated loudness:.*?I:\s+(-?\d+(?:\.\d+)?)\s+LUFS", stderr, re.DOTALL)
    m_p = re.search(r"True peak:.*?Peak:\s+(-?\d+(?:\.\d+)?)\s+dBFS", stderr, re.DOTALL)
    if not m_i:
        return None
    lufs = float(m_i.group(1))
    peak_db = float(m_p.group(1)) if m_p else 0.0
    return {"loudness_lufs": lufs, "replaygain_gain_db": round(REFERENCE_LUFS - lufs, 2),
            "replaygain_peak": round(10 ** (peak_db / 20), 6)}


def analyse(path: Path) -> dict | None:
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-vn", "-af", "ebur128=peak=true",
                        "-f", "null", "-"], capture_output=True, text=True, check=False, timeout=300)
    return parse_ebur128(r.stderr) if r.returncode == 0 else None
