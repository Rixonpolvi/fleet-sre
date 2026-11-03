"""D7 unit tests for infra/gcp.sh (docs/d7-pins.md §Unit tests)."""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "infra" / "gcp.sh"


def test_gcp_sh_passes_bash_n():
    proc = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True)
    assert proc.returncode == 0, proc.stderr.decode()


def test_gcp_sh_passes_shellcheck():
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        pytest.skip("shellcheck is not installed")
    proc = subprocess.run(
        [shellcheck, str(SCRIPT)], capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_up_without_project_exits_before_any_gcloud_call(tmp_path):
    shim_dir = tmp_path / "bin"
    shim_dir.mkdir()
    marker = tmp_path / "gcloud-called"
    shim = shim_dir / "gcloud"
    shim.write_text(f"#!/bin/sh\ntouch {marker}\n")
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    env = {
        key: value
        for key, value in os.environ.items()
        if key != "FLEETSRE_GCP_PROJECT"
    }
    env["PATH"] = str(shim_dir) + os.pathsep + env.get("PATH", "")
    bash = shutil.which("bash") or "/bin/bash"
    proc = subprocess.run(
        [bash, str(SCRIPT), "up"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode != 0
    assert "FLEETSRE_GCP_PROJECT" in proc.stderr
    assert not marker.exists()
