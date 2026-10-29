#!/usr/bin/env python3
"""Scan files for accidentally committed secrets."""

import argparse
import json
import re
import sys
from pathlib import Path

_DASH5 = "-" * 5
_BEGIN = "BEG" + "IN"
_PRIV = "PRIV" + "ATE"
_PEM_RE = re.compile(_DASH5 + _BEGIN + r"[ A-Z]*" + _PRIV + r" KEY" + _DASH5)

_SA_TYPE = "service" + "_account"
_SA_KEY = "private" + "_key"

_API_PREFIX = "AI" + "za"
_API_RE = re.compile(_API_PREFIX + r"[A-Za-z0-9_\-]{35}")


def file_has_secret(path: Path) -> bool:
    try:
        raw = path.read_bytes()
    except OSError:
        return False
    try:
        text = raw.decode("utf-8", errors="replace")
    except ValueError:
        return False
    if _PEM_RE.search(text):
        return True
    if _API_RE.search(text):
        return True
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            data = json.loads(text)
        except ValueError:
            return False
        if isinstance(data, dict) and data.get("type") == _SA_TYPE:
            return True
        if isinstance(data, dict) and _SA_KEY in data and _SA_TYPE in text:
            return True
    return False


def iter_files(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    for item in paths:
        candidate = Path(item)
        if candidate.is_dir():
            for child in candidate.rglob("*"):
                if child.is_file():
                    out.append(child)
        elif candidate.is_file():
            out.append(candidate)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan files for secrets.")
    parser.add_argument("paths", nargs="+", help="Files or directories to scan")
    args = parser.parse_args()
    flagged: list[Path] = []
    for path in iter_files(args.paths):
        if file_has_secret(path):
            flagged.append(path)
    for path in flagged:
        print(str(path))
    return 1 if flagged else 0


if __name__ == "__main__":
    sys.exit(main())
