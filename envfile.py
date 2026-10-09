"""
envfile.py

Minimal .env reader, standard library only. Reads KEY=VALUE lines from a .env
file next to this module and adds them to os.environ. Variables already set
in the environment win, so a scheduled task or shell export can override the
file. Blank lines and lines starting with # are ignored; matching single or
double quotes around a value are removed.
"""

import os
from pathlib import Path


def load(path=None):
    path = Path(path) if path else Path(__file__).resolve().parent / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key, value)
