#!/usr/bin/env python3
"""Check that every deliverable ID has a matching acceptance test."""

import argparse
import re
import sys
from pathlib import Path

ID_RE = re.compile(r"\bD(\d+)-T(\d+)\b")
DIR_RE = re.compile(r"^d(\d+)$", re.IGNORECASE)
TEST_RE = re.compile(r"def\s+test_d(\d+)_t(\d+)_", re.IGNORECASE)


def collect_doc_ids(doc: Path) -> set[tuple[int, int]]:
    text = doc.read_text(encoding="utf-8", errors="replace")
    return {(int(n), int(m)) for n, m in ID_RE.findall(text)}


def existing_deliverables(tests_root: Path) -> set[int]:
    found: set[int] = set()
    if not tests_root.is_dir():
        return found
    for child in tests_root.iterdir():
        if child.is_dir():
            match = DIR_RE.match(child.name)
            if match:
                found.add(int(match.group(1)))
    return found


def collect_test_ids(tests_root: Path, deliverable: int) -> set[tuple[int, int]]:
    found: set[tuple[int, int]] = set()
    target = None
    for child in tests_root.iterdir():
        if child.is_dir() and DIR_RE.match(child.name):
            match = DIR_RE.match(child.name)
            if match and int(match.group(1)) == deliverable:
                target = child
                break
    if target is None:
        return found
    for pyfile in target.rglob("*.py"):
        try:
            text = pyfile.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for n, m in TEST_RE.findall(text):
            found.add((int(n), int(m)))
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description="Trace deliverable IDs to tests.")
    parser.add_argument("--doc", required=True, help="Path to deliverables doc")
    parser.add_argument("--tests", required=True, help="Root of acceptance tests")
    args = parser.parse_args()
    doc = Path(args.doc)
    tests_root = Path(args.tests)
    if not doc.is_file():
        print(f"doc not found: {doc}", file=sys.stderr)
        return 2
    if not tests_root.is_dir():
        print(f"tests root not found: {tests_root}", file=sys.stderr)
        return 2
    doc_ids = collect_doc_ids(doc)
    present = existing_deliverables(tests_root)
    missing: list[str] = []
    for n, m in sorted(doc_ids):
        if n not in present:
            continue
        if (n, m) not in collect_test_ids(tests_root, n):
            missing.append(f"D{n}-T{m}")
    if missing:
        print("Missing tests for IDs:")
        for item in missing:
            print(f"  {item}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
