#!/usr/bin/env python3
"""Fail if the diff between two refs touches protected paths."""

import argparse
import subprocess
import sys

PROTECTED_DIRS = (
    "contracts/",
    "fixtures/",
    "specs/",
    "prompts/",
    "tests/acceptance/",
    "docs/",
)
PROTECTED_FILES = ("AGENTS.md",)


def is_protected(path: str) -> bool:
    normalized = path.strip()
    if normalized in PROTECTED_FILES:
        return True
    for prefix in PROTECTED_DIRS:
        if normalized.startswith(prefix):
            return True
    return False


def changed_files(base: str, head: str) -> list[str]:
    proc = subprocess.run(
        ["git", "diff", "--name-only", base, head],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if proc.returncode != 0:
        print(f"git diff failed: {proc.stderr.strip()}", file=sys.stderr)
        sys.exit(2)
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description="Check for protected-path edits.")
    parser.add_argument("--base", required=True, help="Base git ref")
    parser.add_argument("--head", required=True, help="Head git ref")
    args = parser.parse_args()
    offending = [p for p in changed_files(args.base, args.head) if is_protected(p)]
    if offending:
        print("Protected paths changed:")
        for path in offending:
            print(f"  {path}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
