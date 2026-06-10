"""Run a generated Python experiment inside an ephemeral, hardened Docker
container — the lab's sandbox for autonomous code execution.

Each experiment runs in its own `docker run --rm` from the pinned
`labfoundry-experiment` image with: NO network (`--network none`), a read-only
rootfs + a single writable tmpfs `/work`, memory/cpu/pids caps, all capabilities
dropped, no-new-privileges, a non-root user, and (for GPU experiments) a single
assigned device. The only thing mounted in is the read-only script (and an
optional read-only datasets dir); nothing — no DB URL, API key, or repo source —
leaks in. The script's contract: print exactly one JSON object (its result) to
stdout. Teardown is `docker kill` / `--rm` (whole container, no orphan risk).

The Quartermaster owns the lifecycle: it launches `run_in_container` on its own
task pool (never a dispatcher slot), heartbeats while it runs, and can `kill()`
any experiment or `reap_orphans()` after a restart via the deterministic
`lf-exp-<id>` container name.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

IMAGE = os.environ.get("EXPERIMENT_IMAGE", "labfoundry-experiment:py311")
HEARTBEAT_S = float(os.environ.get("EXPERIMENT_HEARTBEAT_S", "15"))
_MAX_OUTPUT_BYTES = 256 * 1024  # cap captured stdout/stderr — a runaway print can't OOM the harness


def container_name(exp_id: int) -> str:
    return f"lf-exp-{exp_id}"


# exp_id -> container name, for the Quartermaster to target a specific experiment.
_RUNNING: dict[int, str] = {}


@dataclass
class SandboxResult:
    status: str  # 'completed' | 'failed' | 'killed'
    result: dict | None = None
    error: str | None = None
    usage: dict = field(default_factory=dict)  # exit_code, duration_s, stdout_bytes, killed


def _build_cmd(
    name: str,
    script_path: str,
    *,
    mem_mb: int,
    cpus: float,
    requires_gpu: bool,
    gpu_device: str | None,
    datasets_dir: str | None,
) -> list[str]:
    cmd = [
        "docker",
        "run",
        "--rm",
        "--name",
        name,
        "--network",
        "none",
        "--memory",
        f"{mem_mb}m",
        "--memory-swap",
        f"{mem_mb}m",
        "--cpus",
        str(cpus),
        "--pids-limit",
        "256",
        "--read-only",
        "--tmpfs",
        "/work:rw,size=512m,exec",
        "-w",
        "/work",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "-e",
        "PYTHONUNBUFFERED=1",
        "-v",
        f"{script_path}:/work/exp.py:ro",
    ]
    if requires_gpu:
        cmd += ["--gpus", f"device={gpu_device}" if gpu_device is not None else "all"]
    if datasets_dir:
        cmd += ["-v", f"{datasets_dir}:/data:ro"]
    cmd += [IMAGE, "python", "/work/exp.py"]
    return cmd


def _parse_result(stdout: str) -> dict | None:
    """The script prints one JSON object = its result. Be lenient: try the whole
    output, else scan lines from the bottom for the last JSON object."""
    stdout = (stdout or "").strip()
    if not stdout:
        return None
    try:
        v = json.loads(stdout)
        return v if isinstance(v, dict) else {"value": v}
    except ValueError:
        pass
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            with contextlib.suppress(ValueError):
                return json.loads(line)
    return None


async def _docker_kill(name: str) -> None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "kill", name, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        await proc.wait()
    except Exception as e:  # noqa: BLE001 — kill is best-effort; --rm still reaps the container
        log.warning("docker kill %s failed: %s", name, e)


async def run_in_container(
    exp_id: int,
    code: str,
    *,
    wall_clock_s: int = 600,
    mem_mb: int = 2048,
    cpus: float = 1.0,
    requires_gpu: bool = False,
    gpu_device: str | None = None,
    datasets_dir: str | None = None,
    on_heartbeat: Callable[[], Awaitable[None]] | None = None,
) -> SandboxResult:
    """Run `code` in a hardened container; return its parsed JSON result or the
    failure/kill outcome. Enforces the wall-clock budget by `docker kill`."""
    name = container_name(exp_id)
    workdir = tempfile.mkdtemp(prefix="lf-exp-")
    script_path = os.path.join(workdir, "exp.py")
    with open(script_path, "w") as f:
        f.write(code)
    cmd = _build_cmd(
        name,
        script_path,
        mem_mb=mem_mb,
        cpus=cpus,
        requires_gpu=requires_gpu,
        gpu_device=gpu_device,
        datasets_dir=datasets_dir,
    )
    _RUNNING[exp_id] = name
    start = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        comm = asyncio.create_task(proc.communicate())
        killed = False
        while True:
            done, _ = await asyncio.wait({comm}, timeout=HEARTBEAT_S)
            if comm in done:
                break
            if on_heartbeat is not None:
                with contextlib.suppress(Exception):
                    await on_heartbeat()
            if time.monotonic() - start > wall_clock_s:
                killed = True
                await _docker_kill(name)
                await comm  # container exits once killed; collect the pipes
                break
        out_b, err_b = comm.result()
        stdout = (out_b or b"")[:_MAX_OUTPUT_BYTES].decode("utf-8", "replace")
        stderr = (err_b or b"")[:_MAX_OUTPUT_BYTES].decode("utf-8", "replace")
        exit_code = proc.returncode
        usage = {
            "exit_code": exit_code,
            "duration_s": round(time.monotonic() - start, 2),
            "stdout_bytes": len(out_b or b""),
            "killed": killed,
        }
        if killed:
            return SandboxResult("killed", None, f"wall-clock budget {wall_clock_s}s exceeded", usage)
        if exit_code == 0:
            result = _parse_result(stdout)
            if result is None:
                return SandboxResult("failed", None, "experiment produced no JSON result on stdout", usage)
            return SandboxResult("completed", result, None, usage)
        # Non-zero exit (incl. 137 = OOM/SIGKILL from the mem cap).
        err = stderr.strip()[-2000:] or f"exited {exit_code}"
        return SandboxResult("failed", None, err, usage)
    except Exception as e:  # noqa: BLE001 — a launch failure is a failed experiment, never fatal
        log.exception("sandbox run for experiment %s failed to launch", exp_id)
        return SandboxResult(
            "failed", None, f"sandbox launch error: {e}", {"duration_s": round(time.monotonic() - start, 2)}
        )
    finally:
        _RUNNING.pop(exp_id, None)
        shutil.rmtree(workdir, ignore_errors=True)


async def kill(exp_id: int) -> bool:
    """Actively kill a running experiment's container (the Quartermaster's hammer)."""
    name = _RUNNING.get(exp_id) or container_name(exp_id)
    await _docker_kill(name)
    return True


async def list_lab_containers() -> list[str]:
    """Names of all `lf-exp-*` containers docker currently knows about (for orphan reaping)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "ps",
            "-a",
            "--filter",
            "name=lf-exp-",
            "--format",
            "{{.Names}}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        return [n for n in (out or b"").decode().split() if n]
    except Exception as e:  # noqa: BLE001
        log.warning("docker ps for orphan reap failed: %s", e)
        return []


async def force_remove(name: str) -> None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "rm", "-f", name, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        await proc.wait()
    except Exception as e:  # noqa: BLE001
        log.warning("docker rm -f %s failed: %s", name, e)
