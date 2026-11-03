"""D8 acceptance: README structure and facts (d8-pins 20, Rows T8)."""

import re

from d8_helpers import ROOT

HEADINGS = [
    "What it is",
    "Run it locally",
    "Architecture",
    "Gates",
    "Results",
    "Limits",
    "Faithful versus invented",
    "Roles",
    "Built during the hackathon",
    "Links",
]


def _sections(text):
    """Map ## heading -> body text up to the next ## heading."""
    sections = {}
    current = None
    for line in text.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    return {name: "\n".join(body) for name, body in sections.items()}


def _mocks_table_cells():
    """Every non-empty cell of the docs/mocks.md §9 table."""
    lines = (ROOT / "docs" / "mocks.md").read_text().splitlines()
    start = next(
        i for i, line in enumerate(lines)
        if line.startswith("## 9. Faithful versus invented")
    )
    rows = []
    for line in lines[start + 1:]:
        if not line.startswith("|"):
            if rows:
                break
            continue
        rows.append(line)
    cells = []
    for row in rows[2:]:
        for cell in row.split("|")[1:-1]:
            cell = cell.strip()
            if cell:
                cells.append(cell)
    assert cells, "no §9 table cells parsed from docs/mocks.md"
    return cells


def test_d8_t8_readme():
    """D8-T8: the README has the required headings, facts and links.

    Headings in pin-20 order; Run it locally shows make demo and
    make demo-check; Gates names every rule family; Faithful versus
    invented carries every non-empty §9 cell of docs/mocks.md; Built
    during the hackathon names D0-D8; Links has the Repo and Video
    lines; Limits mentions 80.0 and the single-seed scorecard; and no
    live instance is linked.
    """
    text = (ROOT / "README.md").read_text()
    found = {}
    for i, line in enumerate(text.splitlines()):
        if line.startswith("## "):
            found.setdefault(line[3:].strip(), i)
    assert all(h in found for h in HEADINGS), found
    assert [found[h] for h in HEADINGS] == sorted(
        found[h] for h in HEADINGS
    )

    sections = _sections(text)
    assert "make demo" in sections["Run it locally"]
    assert "make demo-check" in sections["Run it locally"]
    for family in ("order", "hypotheses", "evidence", "diagnosis", "reads.cap"):
        assert family in sections["Gates"], family
    for cell in _mocks_table_cells():
        assert cell in sections["Faithful versus invented"], cell
    for n in range(9):
        assert re.search(rf"\bD{n}\b", sections["Built during the hackathon"])
    links = sections["Links"].splitlines()
    assert any(
        line.startswith("- Repo: https://github.com/Rixonpolvi/fleet-sre")
        for line in links
    )
    assert any(line.startswith("- Video:") for line in links)
    assert "80.0" in sections["Limits"]
    assert "single-seed scorecard" in sections["Limits"]
    assert "run.app" not in text
    assert "Live instance" not in text
