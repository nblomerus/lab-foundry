"""
Operations / host telemetry for the floorplan's Operations wing.

    GET /ops/host    — CPU / memory / disk utilization (psutil)

GPU watts/util, electricity projection, and DeepSeek API spend already live at
GET /debug/costs and are reused by the Ops wing directly; this router only adds
the host gauges (CPU/memory/disk) the mockup needs. Best-effort: any read that
fails degrades to status='unavailable' rather than 500-ing the dashboard.
"""

from __future__ import annotations

import logging

import psutil
from fastapi import APIRouter

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ops", tags=["ops"])


@router.get("/host")
async def host_stats() -> dict:
    """CPU / memory / disk utilization for the Ops wing gauges.

    `cpu_percent(interval=None)` is non-blocking: it reports usage since the
    previous call, so a steadily-polling dashboard gets live values without
    stalling the event loop.
    """
    try:
        cpu = psutil.cpu_percent(interval=None)
        vm = psutil.virtual_memory()
        du = psutil.disk_usage("/")
        load = psutil.getloadavg() if hasattr(psutil, "getloadavg") else (0.0, 0.0, 0.0)
        return {
            "status": "ok",
            "cpu_percent": round(cpu, 1),
            "cpu_count": psutil.cpu_count(logical=True),
            "load_avg": [round(x, 2) for x in load],
            "memory_percent": round(vm.percent, 1),
            "memory_used_gb": round(vm.used / 1e9, 1),
            "memory_total_gb": round(vm.total / 1e9, 1),
            "disk_percent": round(du.percent, 1),
            "disk_used_gb": round(du.used / 1e9, 1),
            "disk_total_gb": round(du.total / 1e9, 1),
        }
    except Exception as e:  # noqa: BLE001 — never 500 the dashboard
        log.warning("host stats failed: %s", e)
        return {"status": "unavailable", "error": str(e)}
