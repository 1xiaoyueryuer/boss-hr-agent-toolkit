# -*- coding: utf-8 -*-
"""Offline test isolation: block unmocked browser/network access and keep all
output roots and registry writes inside each test workspace.
"""
import os
import sys
import importlib
import socket
import subprocess

import pytest


@pytest.fixture(autouse=True)
def _block_live_browser_access(monkeypatch):
    """Fail immediately if an offline test misses a browser/network mock."""
    original_popen = subprocess.Popen

    def guarded_popen(args, *positional, **kwargs):
        executable = args[0] if isinstance(args, (list, tuple)) else args
        if "msedge" in str(executable).casefold():
            raise AssertionError("Offline tests must mock Edge process creation")
        return original_popen(args, *positional, **kwargs)

    def blocked_connect(*args, **kwargs):
        raise AssertionError("Offline tests must mock network/CDP connections")

    monkeypatch.setattr(subprocess, "Popen", guarded_popen)
    monkeypatch.setattr(socket.socket, "connect", blocked_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked_connect)


# 在 conftest 加载时立刻生效,优先于任何测试文件 import
# 但不动 sys.stdout 对象本身 —— 避免破坏 pytest capture 的 tmpfile。
if sys.platform == "win32":
    # 子进程用 subprocess 启动时已被 PYTHONIOENCODING=utf-8 覆盖。
    # 这里只做"保险":如果当前 stdout 编码还不是 utf-8，**只改属性**，不调 reconfigure()。
    if hasattr(sys.stdout, "encoding") and sys.stdout.encoding and not sys.stdout.encoding.lower().startswith("utf"):
        try:
            sys.stdout.encoding = "utf-8"  # type: ignore[attr-defined]
        except Exception:
            pass
    if hasattr(sys.stderr, "encoding") and sys.stderr.encoding and not sys.stderr.encoding.lower().startswith("utf"):
        try:
            sys.stderr.encoding = "utf-8"  # type: ignore[attr-defined]
        except Exception:
            pass


@pytest.fixture(autouse=True)
def _isolate_output_root(tmp_path, monkeypatch):
    """强制把每个测试的输出根隔离到临时目录，防止污染用户真实的
    ~/Desktop/boss-hr-output/。

    为什么需要这个：`output_manager.OUTPUT_ROOT` 是模块级常量，在 import
    那一刻就由 BOSS_HR_OUTPUT_DIR 环境变量固化。测试内部再改环境变量对
    已 import 的模块无效 —— 曾因此在用户桌面留下 9 个「测试XxxJob」空壳
    文件夹（2026-07-29）。所以必须直接 patch 模块属性。
    """
    monkeypatch.setenv("BOSS_HR_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-appdata"))
    registry_path = str(tmp_path / "jobs.json")
    monkeypatch.setenv("BOSS_HR_JOBS_REGISTRY", registry_path)

    _shared = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "shared"))
    if _shared not in sys.path:
        sys.path.insert(0, _shared)

    try:
        import output_manager
    except ImportError:
        # shared/ 不可用的测试（如纯算法测试）无需隔离
        yield
        return

    # Both import spellings occur in the services and worker scripts. Their
    # constants are captured at import time, including the registry path.
    # Patch both, and pass the registry override to child processes as well.
    for name in ("output_manager", "shared.output_manager"):
        module = importlib.import_module(name)
        monkeypatch.setattr(module, "OUTPUT_ROOT", str(tmp_path))
    for name in ("job_registry", "shared.job_registry"):
        module = importlib.import_module(name)
        monkeypatch.setattr(module, "JOBS_REGISTRY_PATH", registry_path)
    yield
