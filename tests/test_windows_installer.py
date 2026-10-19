"""Installer recovery and real cmd.exe exit/path behavior, without downloads."""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("installer", ROOT / "scripts/install_windows.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def test_index_failure_retries_separate_sources(monkeypatch, tmp_path):
    calls = []
    def run(args):
        calls.append(args)
        return len(calls) == 3
    monkeypatch.setattr(installer, "run", run)
    assert installer.install(["wheel"], tmp_path / "pip.log")
    assert len(calls) == 3
    assert "--index-url" not in calls[0]
    assert "https://pypi.org/simple" in calls[1]
    assert "https://pypi.tuna.tsinghua.edu.cn/simple" in calls[2]
    assert all("--retries" in c and "--timeout" in c and "--log" in c for c in calls)
    assert all("--extra-index-url" not in c for c in calls)


def test_all_sources_failed_returns_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(installer, "run", lambda *a, **kw: False)
    assert not installer.install(["wheel"], tmp_path / "pip.log")


@pytest.mark.parametrize("failed_stage", [0, 1])
def test_dependency_failure_stops_install(monkeypatch, failed_stage):
    calls = []
    monkeypatch.setattr(installer, "run", lambda *a, **kw: True)
    def install(args, log):
        calls.append(args)
        return len(calls) - 1 != failed_stage
    monkeypatch.setattr(installer, "install", install)
    assert installer.main() == 1
    assert len(calls) == failed_stage + 1


def test_missing_pip_bootstrapped_and_same_python_verified(monkeypatch):
    calls = []
    installs = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return len(calls) != 1
    monkeypatch.setattr(installer, "run", run)
    monkeypatch.setattr(installer, "install", lambda args, log: installs.append(args) or True)
    assert installer.main() == 0
    assert calls[1][0] == ["-m", "ensurepip", "--upgrade"]
    assert "--no-build-isolation" in installs[1]
    assert calls[-1][0] == ["-m", "boss_hr", "--help"]
    assert calls[-1][1]["cwd"] == ROOT.parent


@pytest.mark.skipif(os.name != "nt", reason="real Windows batch parsing")
@pytest.mark.parametrize("rc", [0, 7])
def test_batch_paths_and_exit_code(tmp_path, rc):
    root = tmp_path / "source with spaces ! and (brackets)"
    (root / "scripts").mkdir(parents=True)
    shutil.copyfile(ROOT / "install-windows.bat", root / "install-windows.bat")
    (root / "scripts/install_windows.py").write_text(f"import sys; sys.exit({rc})", encoding="ascii")
    proc = subprocess.run(["cmd.exe", "/d", "/c", str(root / "install-windows.bat"), "--no-pause"],
                          cwd=tmp_path, capture_output=True)
    assert proc.returncode == rc, proc.stdout + proc.stderr
