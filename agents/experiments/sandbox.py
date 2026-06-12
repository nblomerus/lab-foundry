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

# The harness's inference-only LLM broker (harness/llm_broker.py). When its unix socket
# exists at launch, it is bind-mounted into the container together with the stdlib
# helper — the sandbox stays --network none; inference is the ONLY re-admitted
# capability. Condition-driven: broker off → socket absent → nothing mounted.
LLM_SOCKET = os.environ.get("EXPERIMENT_LLM_SOCKET", "/tmp/labfoundry-llm-broker.sock")
_LLM_HELPER = os.path.join(os.path.dirname(__file__), "sandbox_llm.py")


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
    llm_socket: str | None = None,
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
        # Writable scratch (rootfs is read-only). mode=1777 so the non-root user
        # (uid 10001) can actually write — a root-owned tmpfs makes tempfile find
        # "no usable temporary directory" and `import torch` dies. Sized for torch's
        # import-time caches (triton kernels, etc.), not just the script.
        "/work:rw,size=1024m,exec,mode=1777",
        "-w",
        "/work",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "-e",
        "PYTHONUNBUFFERED=1",
        # The rootfs is --read-only, but torch & friends create temp/cache/config
        # dirs at IMPORT time (torch.distributed does tempfile.TemporaryDirectory()
        # on `import torch`). Point HOME / TMPDIR / the XDG + matplotlib caches at
        # the writable /work tmpfs, or every torch (i.e. every GPU/deep-learning)
        # experiment dies before it runs a line. CPU sklearn/xgboost don't need this.
        "-e",
        "HOME=/work",
        "-e",
        "TMPDIR=/work",
        "-e",
        "XDG_CACHE_HOME=/work/.cache",
        "-e",
        "MPLCONFIGDIR=/work/.mpl",
        "-v",
        f"{script_path}:/work/exp.py:ro",
    ]
    if requires_gpu:
        cmd += ["--gpus", f"device={gpu_device}" if gpu_device is not None else "all"]
    if datasets_dir:
        cmd += ["-v", f"{datasets_dir}:/data:ro"]
    if llm_socket:
        # The socket mount is rw (connecting writes); the helper is ro. Network stays none.
        cmd += ["-v", f"{llm_socket}:/sock/ollama.sock", "-v", f"{_LLM_HELPER}:/opt/lab/llm.py:ro"]
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
        llm_socket=LLM_SOCKET if os.path.exists(LLM_SOCKET) else None,
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


async def image_digest(image: str | None = None) -> str | None:
    """The immutable content id (sha256) of the experiment image — the reproducibility
    anchor in provenance, since the TAG is mutable (a rebuild repoints it). Uses the
    local image Id; returns None if docker/inspect is unavailable."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "image",
            "inspect",
            "--format",
            "{{.Id}}",
            image or IMAGE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        digest = (out or b"").decode().strip()
        return digest or None
    except Exception as e:  # noqa: BLE001 — provenance enrichment must never block a run
        log.warning("image digest lookup failed for %s: %s", image or IMAGE, e)
        return None


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
