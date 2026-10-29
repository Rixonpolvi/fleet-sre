"""D0 acceptance tests: scaffold and guardrails."""

import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]


def _run(args, cwd, env=None):
    return subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, timeout=600, env=env
    )


def _git(args, cwd):
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stderr}"
    return proc


def _init_repo(path):
    path.mkdir(parents=True, exist_ok=True)
    _git(["init"], path)
    _git(["config", "user.email", "d0@test.local"], path)
    _git(["config", "user.name", "d0"], path)
    (path / "README.md").write_text("base\n")
    _git(["add", "-A"], path)
    _git(["commit", "-m", "base"], path)
    return _git(["rev-parse", "HEAD"], path).stdout.strip()


def test_d0_t1_make_check():
    """D0-T1: `make check` exits 0 and runs ruff, mypy, import-linter, trace check, pytest."""
    if os.environ.get("FLEETSRE_INSIDE_MAKE_CHECK"):
        pytest.skip("inside make check; avoids pytest recursion")
    makefile = ROOT / "Makefile"
    assert makefile.exists(), "Makefile missing"
    text = makefile.read_text()
    assert "ruff" in text, "make check does not run ruff"
    assert "mypy" in text, "make check does not run mypy"
    assert (
        "import-linter" in text or "lint-imports" in text or "import_linter" in text
    ), "make check does not run import-linter"
    assert "check_trace" in text, "make check does not run the trace check"
    assert "pytest" in text, "make check does not run pytest"
    env = dict(os.environ, FLEETSRE_INSIDE_MAKE_CHECK="1")
    proc = _run(["make", "check"], ROOT, env=env)
    assert proc.returncode == 0, (
        f"make check exited {proc.returncode}:\n{proc.stdout}\n{proc.stderr}"
    )


def test_d0_t2_package_imports():
    """D0-T2: `import fleetsre` works, __version__ is set, requires-python >= 3.12."""
    import fleetsre

    assert getattr(fleetsre, "__version__", None), (
        "fleetsre.__version__ is not set"
    )
    with open(ROOT / "pyproject.toml", "rb") as fh:
        data = tomllib.load(fh)
    req = data["project"]["requires-python"]
    matched = re.search(r">=\s*(\d+)\.(\d+)", req)
    assert matched, f"requires-python {req!r} pins no minimum version"
    assert (int(matched.group(1)), int(matched.group(2))) >= (3, 12), (
        f"requires-python {req!r} allows Python < 3.12"
    )


def test_d0_t3_protected_guard(tmp_path):
    """D0-T3: check_protected.py exits 1 on protected edits, 0 elsewhere."""
    script = ROOT / "scripts" / "check_protected.py"
    assert script.exists(), "scripts/check_protected.py missing"
    protected = [
        "contracts/x.txt",
        "fixtures/x.txt",
        "specs/x.txt",
        "prompts/x.txt",
        "tests/acceptance/x.txt",
        "docs/x.txt",
        "AGENTS.md",
    ]
    for index, rel in enumerate(protected):
        repo = tmp_path / f"case{index}"
        base = _init_repo(repo)
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("protected change\n")
        _git(["add", "-A"], repo)
        _git(["commit", "-m", "head"], repo)
        head = _git(["rev-parse", "HEAD"], repo).stdout.strip()
        proc = subprocess.run(
            [sys.executable, str(script), "--base", base, "--head", head],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode == 1, (
            f"expected exit 1 for protected path {rel}, got {proc.returncode}:"
            f"\n{proc.stdout}\n{proc.stderr}"
        )
    for index, rel in enumerate(["fleetsre/new_module.py", "fleetsre/docs_helper.py"]):
        repo = tmp_path / f"case_clean{index}"
        base = _init_repo(repo)
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x = 1\n")
        _git(["add", "-A"], repo)
        _git(["commit", "-m", "head"], repo)
        head = _git(["rev-parse", "HEAD"], repo).stdout.strip()
        proc = subprocess.run(
            [sys.executable, str(script), "--base", base, "--head", head],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode == 0, (
            f"expected exit 0 for unprotected change {rel}, got {proc.returncode}:"
            f"\n{proc.stdout}\n{proc.stderr}"
        )


def test_d0_t4_ci_wiring():
    """D0-T4: ci.yml triggers on pull_request and gates check_protected.py on impl/* heads."""
    ci = ROOT / ".github" / "workflows" / "ci.yml"
    assert ci.exists(), ".github/workflows/ci.yml missing"
    data = yaml.safe_load(ci.read_text())
    assert isinstance(data, dict), "ci.yml does not parse to a mapping"
    jobs = data.get("jobs", {})
    assert jobs, "ci.yml defines no jobs"
    on = data.get("on", data.get(True))
    assert on is not None, "ci.yml has no `on:` trigger"
    assert "pull_request" in json.dumps(on), "ci.yml does not trigger on pull_request"
    runners = {
        name: job
        for name, job in jobs.items()
        if "check_protected.py" in json.dumps(job)
    }
    assert runners, "no CI job runs check_protected.py"
    for name, job in runners.items():
        conds = []
        if isinstance(job, dict):
            if "if" in job:
                conds.append(str(job["if"]))
            for step in job.get("steps", []) or []:
                if isinstance(step, dict) and "if" in step:
                    conds.append(str(step["if"]))
        assert any(
            "github.head_ref" in cond and "impl/" in cond for cond in conds
        ), f"job {name} has no `if` gating on github.head_ref and impl/"


def test_d0_t5_trace_check(tmp_path):
    """D0-T5: check_trace.py exits 1 when an ID lacks its test, 0 when all exist."""
    script = ROOT / "scripts" / "check_trace.py"
    assert script.exists(), "scripts/check_trace.py missing"
    doc = tmp_path / "doc.md"
    doc.write_text(
        "## D0 - Scaffold\n\n"
        "| ID | Test | Pass |\n"
        "| D0-T1 | foo | bar |\n"
        "| D0-T2 (live) | foo | bar |\n"
        "\n"
        "## D1 - Contracts\n\n"
        "| ID | Test | Pass |\n"
        "| D1-T1 | x | y |\n"
    )
    tests = tmp_path / "tests"
    (tests / "d0").mkdir(parents=True)
    target = tests / "d0" / "test_d0.py"
    target.write_text("def test_something_else():\n    pass\n")
    args = [sys.executable, str(script), "--doc", str(doc), "--tests", str(tests)]
    proc = subprocess.run(
        args, cwd=ROOT, capture_output=True, text=True, timeout=120
    )
    assert proc.returncode == 1, (
        f"expected exit 1 when tests missing, got {proc.returncode}:"
        f"\n{proc.stdout}\n{proc.stderr}"
    )
    target.write_text("def test_d0_t1_foo():\n    pass\n")
    proc = subprocess.run(
        args, cwd=ROOT, capture_output=True, text=True, timeout=120
    )
    assert proc.returncode == 1, (
        f"expected exit 1 when D0-T2 (live) lacks its test, got {proc.returncode}:"
        f"\n{proc.stdout}\n{proc.stderr}"
    )
    target.write_text(
        "def test_d0_t1_foo():\n    pass\ndef test_d0_t2_live_thing():\n    pass\n"
    )
    proc = subprocess.run(
        args, cwd=ROOT, capture_output=True, text=True, timeout=120
    )
    assert proc.returncode == 0, (
        f"expected exit 0 when d0 complete and d1 section ignored, got {proc.returncode}:"
        f"\n{proc.stdout}\n{proc.stderr}"
    )


def test_d0_t6_secret_scan(tmp_path):
    """D0-T6: scan_secrets.py flags each secrets file and exits 0 on repo files."""
    script = ROOT / "scripts" / "scan_secrets.py"
    assert script.exists(), "scripts/scan_secrets.py missing"
    begin = "-----" + "BEGIN " + "PRIV" + "ATE " + "KEY-----"
    end = "-----" + "END " + "PRIV" + "ATE " + "KEY-----"
    pem = f"{begin}\nMIIEvQIBADANBgkqhkiG9w0BAQEF\n{end}\n"
    sa = json.dumps(
        {
            "type": "service" + "_account",
            "private" + "_key": "x",
            "client_email": "a@b.iam.gserviceaccount.com",
        }
    )
    api = "AI" + "zaSy" + "DaGm" + "WKa4JsXZ-HjI4y5d4v8w9z0A1b2C3"
    cases = {
        "key.pem": pem,
        "sa.json": sa + "\n",
        "api.txt": "api" + f"_key={api}\n",
    }
    for name, content in cases.items():
        target = tmp_path / name
        target.write_text(content)
        proc = subprocess.run(
            [sys.executable, str(script), str(target)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode == 1, (
            f"expected exit 1 for {name}, got {proc.returncode}:"
            f"\n{proc.stdout}\n{proc.stderr}"
        )
        assert target.name in proc.stdout, (
            f"stdout does not name {name}:\n{proc.stdout}"
        )
    tracked = _git(["ls-files", "-co", "--exclude-standard"], ROOT).stdout.splitlines()
    tracked = [line for line in tracked if line.strip()]
    assert tracked, "git ls-files returned no files"
    proc = subprocess.run(
        [sys.executable, str(script), *tracked],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert proc.returncode == 0, (
        f"scan_secrets.py flagged clean repo files:\n{proc.stdout}\n{proc.stderr}"
    )


def test_d0_t7_denylist_hook(tmp_path):
    """D0-T7: check_denylist.py blocks denylisted terms; config references it; repo has none."""
    hits = [
        p
        for p in ROOT.rglob("*")
        if p.is_file()
        and p.name.lower() == "denylist.txt"
        and ".git" not in p.parts
        and ".venv" not in p.parts
    ]
    assert not hits, f"repo contains denylist file(s): {hits}"
    cfg = ROOT / ".pre-commit-config.yaml"
    assert cfg.exists(), ".pre-commit-config.yaml missing"
    assert "check_denylist.py" in cfg.read_text(), (
        "pre-commit config does not reference check_denylist.py"
    )
    script = ROOT / "scripts" / "check_denylist.py"
    assert script.exists(), "scripts/check_denylist.py missing"
    home = tmp_path / "home"
    deny_dir = home / ".config" / "fleetsre"
    deny_dir.mkdir(parents=True)
    (deny_dir / "denylist.txt").write_text("acmecorp\n")
    env = dict(os.environ, HOME=str(home))
    dirty = tmp_path / "dirty.txt"
    dirty.write_text("Contact AcmeCorp support\n")
    proc = subprocess.run(
        [sys.executable, str(script), str(dirty)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 1, (
        f"expected exit 1 for denylisted term, got {proc.returncode}:"
        f"\n{proc.stdout}\n{proc.stderr}"
    )
    clean = tmp_path / "clean.txt"
    clean.write_text("hello world\n")
    proc = subprocess.run(
        [sys.executable, str(script), str(clean)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, (
        f"expected exit 0 for clean file, got {proc.returncode}:"
        f"\n{proc.stdout}\n{proc.stderr}"
    )
    (deny_dir / "denylist.txt").unlink()
    proc = subprocess.run(
        [sys.executable, str(script), str(dirty)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, (
        f"expected exit 0 with no denylist file, got {proc.returncode}:"
        f"\n{proc.stdout}\n{proc.stderr}"
    )


def test_d0_t8_markers():
    """D0-T8: pytest registers live/gcp; make check deselects, test-live/test-gcp select."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--markers"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, f"pytest --markers failed:\n{proc.stderr}"
    assert "live" in proc.stdout, "marker `live` is not registered"
    assert "gcp" in proc.stdout, "marker `gcp` is not registered"
    makefile = ROOT / "Makefile"
    assert makefile.exists(), "Makefile missing"
    proc = _run(["make", "-n", "check"], ROOT)
    assert proc.returncode == 0, f"make -n check failed:\n{proc.stderr}"
    out = proc.stdout + proc.stderr
    assert "not live" in out and "not gcp" in out, (
        "make check does not deselect live and gcp"
    )
    proc = _run(["make", "-n", "test-live"], ROOT)
    assert proc.returncode == 0, f"make -n test-live failed:\n{proc.stderr}"
    out = proc.stdout + proc.stderr
    assert re.search("-m\\s+[\"']?live[\"']?(\\s|$)", out), (
        "make test-live does not select only live"
    )
    assert "not live" not in out, "make test-live deselects live"
    proc = _run(["make", "-n", "test-gcp"], ROOT)
    assert proc.returncode == 0, f"make -n test-gcp failed:\n{proc.stderr}"
    out = proc.stdout + proc.stderr
    assert re.search("-m\\s+[\"']?gcp[\"']?(\\s|$)", out), (
        "make test-gcp does not select only gcp"
    )
    assert "not gcp" not in out, "make test-gcp deselects gcp"


def test_d0_t9_agents_structure():
    """D0-T9: AGENTS.md contains the six required headings."""
    agents = ROOT / "AGENTS.md"
    assert agents.exists(), "AGENTS.md missing"
    text = agents.read_text()
    for heading in [
        "Project",
        "Hard rules",
        "Protected paths",
        "Architecture",
        "Commands",
        "Deliverable protocol",
    ]:
        pattern = re.compile(r"^#+\s+" + re.escape(heading) + r"\s*$", re.MULTILINE)
        assert pattern.search(text), f"AGENTS.md missing heading: {heading}"
