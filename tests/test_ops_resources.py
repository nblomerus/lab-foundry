"""Unit tests for ops.resources — the one shared resource sampler.

Everything is mocked: psutil is monkeypatched to a fake exposing virtual_memory /
disk_usage / cpu_percent, and nvidia-smi is mocked by monkeypatching the module's
asyncio.create_subprocess_exec (and wait_for) to a fake proc whose communicate()
yields canned CSV — no real psutil reads, no nvidia-smi, no subprocess.
"""

from __future__ import annotations

import asyncio

import pytest

from ops import resources


# ── fakes ─────────────────────────────────────────────────────────────────────
class _VM:
    percent = 42.0
    available = 8 * 1024 * 1024 * 1024  # 8 GiB
    total = 16 * 1024 * 1024 * 1024  # 16 GiB


class _DU:
    percent = 55.5


class _FakePsutil:
    """Minimal psutil stand-in with the three calls sample_host() makes."""

    def __init__(self, *, boom: bool = False):
        self._boom = boom

    def virtual_memory(self):
        if self._boom:
            raise RuntimeError("psutil broken")
        return _VM()

    def disk_usage(self, _path):
        return _DU()

    def cpu_percent(self, interval=None):
        return 12.345


class _Proc:
    """Fake asyncio subprocess whose communicate() yields canned stdout."""

    def __init__(self, out: bytes):
        self._out = out

    async def communicate(self):
        return (self._out, b"")


def _patch_smi(monkeypatch, out: bytes):
    async def _exec(*_a, **_k):
        return _Proc(out)

    async def _wait_for(coro, timeout):
        return await coro

    monkeypatch.setattr(resources.asyncio, "create_subprocess_exec", _exec)
    monkeypatch.setattr(resources.asyncio, "wait_for", _wait_for)


# ── sample_host() ───────────────────────────────────────────────────────────
def test_sample_host_ok(monkeypatch):
    monkeypatch.setattr(resources, "psutil", _FakePsutil())
    out = resources.sample_host()
    assert set(out) == {"cpu_percent", "mem_percent", "mem_free_mb", "mem_total_mb", "disk_percent"}
    assert out["cpu_percent"] == 12.345
    assert out["mem_percent"] == 42.0
    assert out["mem_free_mb"] == 8 * 1024  # 8 GiB → MB
    assert out["mem_total_mb"] == 16 * 1024
    assert out["disk_percent"] == 55.5


def test_sample_host_degraded(monkeypatch):
    monkeypatch.setattr(resources, "psutil", _FakePsutil(boom=True))
    out = resources.sample_host()
    assert out == {"cpu_percent": None, "mem_percent": None, "mem_free_mb": None, "disk_percent": None}


# ── sample_gpus() ───────────────────────────────────────────────────────────
def test_sample_gpus_parses_csv(monkeypatch):
    _patch_smi(monkeypatch, b"0, NVIDIA, 12.0, 8192, 2048, 6144, 70.0\n")
    gpus = asyncio.run(resources.sample_gpus())
    assert gpus == [
        {
            "index": 0,
            "name": "NVIDIA",
            "util": 12.0,
            "mem_total_mb": 8192.0,
            "mem_used_mb": 2048.0,
            "mem_free_mb": 6144.0,
            "watts": 70.0,
        }
    ]


def test_sample_gpus_watts_na(monkeypatch):
    _patch_smi(monkeypatch, b"1, NVIDIA, 0.0, 8192, 0, 8192, [N/A]\n")
    gpus = asyncio.run(resources.sample_gpus())
    assert gpus[0]["index"] == 1
    assert gpus[0]["watts"] is None


def test_sample_gpus_malformed_line_skipped(monkeypatch):
    # second line has only 3 fields → skipped; first line parses
    _patch_smi(monkeypatch, b"0, NVIDIA, 12.0, 8192, 2048, 6144, 70.0\nbroken, line, only\n")
    gpus = asyncio.run(resources.sample_gpus())
    assert len(gpus) == 1
    assert gpus[0]["index"] == 0


def test_sample_gpus_empty_output(monkeypatch):
    _patch_smi(monkeypatch, b"\n")
    assert asyncio.run(resources.sample_gpus()) == []


def test_sample_gpus_no_nvidia_smi(monkeypatch):
    async def _exec(*_a, **_k):
        raise FileNotFoundError("no nvidia-smi")

    monkeypatch.setattr(resources.asyncio, "create_subprocess_exec", _exec)
    assert asyncio.run(resources.sample_gpus()) == []


# ── best_gpu_with_headroom() ────────────────────────────────────────────────
def test_best_gpu_with_headroom_picks_most_free():
    gpus = [
        {"index": 0, "mem_free_mb": 4000.0},
        {"index": 1, "mem_free_mb": 9000.0},
        {"index": 2, "mem_free_mb": 6000.0},
    ]
    # need 1000, reserve 1000 → all three have headroom; pick the most free (index 1)
    assert resources.best_gpu_with_headroom(gpus, need_mb=1000, reserve_mb=1000) == 1


def test_best_gpu_with_headroom_none_qualifies():
    gpus = [{"index": 0, "mem_free_mb": 1500.0}]
    # 1500 - 1000 = 500 < reserve 1000 → no headroom
    assert resources.best_gpu_with_headroom(gpus, need_mb=1000, reserve_mb=1000) is None


def test_best_gpu_with_headroom_skips_none_free():
    gpus = [{"index": 0, "mem_free_mb": None}, {"index": 1, "mem_free_mb": 8000.0}]
    assert resources.best_gpu_with_headroom(gpus, need_mb=1000, reserve_mb=1000) == 1


def test_best_gpu_with_headroom_empty():
    assert resources.best_gpu_with_headroom([], need_mb=1, reserve_mb=1) is None


def test_best_gpu_with_headroom_respects_allowlist():
    gpus = [
        {"index": 0, "mem_free_mb": 12000.0},  # most free, but NOT allowed (e.g. unsupported arch)
        {"index": 1, "mem_free_mb": 6000.0},
    ]
    # Without the allowlist it would pick 0 (most free); restricted to {1} it picks 1.
    assert resources.best_gpu_with_headroom(gpus, need_mb=1000, reserve_mb=1000) == 0
    assert resources.best_gpu_with_headroom(gpus, need_mb=1000, reserve_mb=1000, allowed={1}) == 1
    # Allowed device lacks headroom → None even though a disallowed one has plenty.
    assert resources.best_gpu_with_headroom(gpus, need_mb=5500, reserve_mb=1000, allowed={1}) is None


# ── sample_resources() ──────────────────────────────────────────────────────
def test_sample_resources_combines_host_and_gpus(monkeypatch):
    monkeypatch.setattr(resources, "psutil", _FakePsutil())
    _patch_smi(monkeypatch, b"0, NVIDIA, 12.0, 8192, 2048, 6144, 70.0\n")
    out = asyncio.run(resources.sample_resources())
    assert out["cpu_percent"] == 12.345
    assert out["mem_total_mb"] == 16 * 1024
    assert len(out["gpus"]) == 1
    assert out["gpus"][0]["index"] == 0


def test_sample_resources_host_degraded_no_gpus(monkeypatch):
    monkeypatch.setattr(resources, "psutil", _FakePsutil(boom=True))

    async def _exec(*_a, **_k):
        raise FileNotFoundError("no nvidia-smi")

    monkeypatch.setattr(resources.asyncio, "create_subprocess_exec", _exec)
    out = asyncio.run(resources.sample_resources())
    assert out["cpu_percent"] is None
    assert out["gpus"] == []


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
