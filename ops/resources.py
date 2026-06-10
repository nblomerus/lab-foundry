"""One shared resource sampler — CPU / memory / disk (psutil) + per-GPU VRAM
and utilization (nvidia-smi). Used by the Quartermaster to gate experiment
allocation on live headroom and by the API/dashboard gauges.

Everything is best-effort: a probe that fails degrades to absent/None rather
than raising, so neither the watchdog nor the dashboard ever breaks on it.
"""

from __future__ import annotations

import asyncio
import logging

import psutil

log = logging.getLogger(__name__)


def sample_host() -> dict:
    """CPU / memory / disk. `cpu_percent(interval=None)` is non-blocking (usage
    since the previous call), so a steady poll gets live values without stalling."""
    try:
        vm = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        return {
            "cpu_percent": psutil.cpu_percent(interval=None),
            "mem_percent": vm.percent,
            "mem_free_mb": int(vm.available / (1024 * 1024)),
            "mem_total_mb": int(vm.total / (1024 * 1024)),
            "disk_percent": disk.percent,
        }
    except Exception as e:  # noqa: BLE001
        log.warning("host resource sample failed: %s", e)
        return {"cpu_percent": None, "mem_percent": None, "mem_free_mb": None, "disk_percent": None}


async def sample_gpus() -> list[dict]:
    """Per-GPU index/name/util + VRAM total/used/free (MB). Empty if no GPU/driver."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "nvidia-smi",
            "--query-gpu=index,name,utilization.gpu,memory.total,memory.used,memory.free,power.draw",
            "--format=csv,noheader,nounits",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        gpus = []
        for line in out.decode().strip().splitlines():
            p = [x.strip() for x in line.split(",")]
            if len(p) < 7:
                continue
            gpus.append(
                {
                    "index": int(p[0]),
                    "name": p[1],
                    "util": float(p[2]),
                    "mem_total_mb": float(p[3]),
                    "mem_used_mb": float(p[4]),
                    "mem_free_mb": float(p[5]),
                    "watts": float(p[6]) if p[6] not in ("", "[N/A]") else None,
                }
            )
        return gpus
    except Exception as e:  # noqa: BLE001 — no GPU / no driver / timeout → no GPU lane
        log.debug("gpu sample unavailable: %s", e)
        return []


async def sample_resources() -> dict:
    """The full snapshot the Quartermaster reasons over each tick."""
    host = sample_host()
    gpus = await sample_gpus()
    return {**host, "gpus": gpus}


def best_gpu_with_headroom(gpus: list[dict], need_mb: float, reserve_mb: float) -> int | None:
    """Pick the GPU index with the most free VRAM that still leaves `reserve_mb`
    free for Ollama after `need_mb` — or None if none has the headroom."""
    best, best_free = None, -1.0
    for g in gpus:
        free = g.get("mem_free_mb")
        if free is None:
            continue
        if free - need_mb >= reserve_mb and free > best_free:
            best, best_free = g["index"], free
    return best
