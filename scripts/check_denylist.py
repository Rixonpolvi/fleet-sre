#!/usr/bin/env python3
"""Block files containing denylisted terms from outside the repo."""

import argparse
import os
import sys
from pathlib import Path


def denylist_path() -> Path:
    home = os.environ.get("HOME")
    if home:
        base = Path(home)
    else:
        base = Path(os.path.expanduser("~"))
    return base / ".config" / "fleetsre" / "denylist.txt"


def load_terms(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    terms: list[str] = []
    for line in text.splitlines():
        term = line.strip()
        if not term or term.startswith("#"):
            continue
        terms.append(term.lower())
    return terms


def file_matches(path: Path, terms: list[str]) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    lowered = text.lower()
    return any(term in lowered for term in terms)


def main() -> int:
    parser = argparse.ArgumentParser(description="Check files against the denylist.")
    parser.add_argument("paths", nargs="*", help="Files to check")
    args = parser.parse_args()
    terms = load_terms(denylist_path())
    if not terms or not args.paths:
        return 0
    blocked: list[str] = []
    for item in args.paths:
        candidate = Path(item)
        if candidate.is_file() and file_matches(candidate, terms):
            blocked.append(str(candidate))
    for entry in blocked:
        print(f"denylisted term in {entry}")
    return 1 if blocked else 0


if __name__ == "__main__":
    sys.exit(main())
