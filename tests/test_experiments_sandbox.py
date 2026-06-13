"""Tests for `agents.experiments.sandbox` — the hardened-Docker experiment runner.

Everything that touches the outside world (the `docker` CLI) is mocked: the
module's `asyncio.create_subprocess_exec` is replaced with a fake that returns a
fake process object (async `communicate()` / `wait()` + a `.returncode`). No real
Docker, no network, no nvidia-smi, no Postgres, no LLM. The fake records the argv
it was launched with so we can assert the hardening flags.

The wall-clock-timeout path is driven deterministically by patching the module's
`time.monotonic` (so we advance "the clock" without real sleeping) and stubbing
`asyncio.wait` to report a timeout — never a real >budget sleep.
"""

from __future__ import annotations

import asyncio

import pytest

from agents.experiments import sandbox

# Applied to the async tests only (the pure-function tests below are sync).
_aio = pytest.mark.asyncio


# ── fake docker process ───────────────────────────────────────────────────────
class FakeProc:
    """A stand-in for an `asyncio` subprocess. `communicate()` returns the scripted
    (stdout, stderr) bytes; `wait()`/`returncode` model the exit. If `hang` is set,
    `communicate()` never resolves on its own (drives the wall-clock-kill path)."""

    def __init__(self, *, stdout=b"", stderr=b"", returncode=0, hang=False):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode
        self._hang = hang
        self.waited = False

    async def communicate(self):
        if self._hang:
            await asyncio.Event().wait()  # never set → pends forever (cancelled by the test loop)
        return (self._stdout, self._stderr)

    async def wait(self):
        self.waited = True
        return self.returncode


def _install_exec(monkeypatch, proc, *, record=None):
    """Patch the module's `create_subprocess_exec` to return `proc`, recording argv."""

    async def _fake_exec(*args, **_kw):
        if record is not None:
            record.append(list(args))
        return proc

    monkeypatch.setattr(sandbox.asyncio, "create_subprocess_exec", _fake_exec)
    return record


# ── _parse_result ─────────────────────────────────────────────────────────────
def test_parse_result_whole_stdout_json():
    assert sandbox._parse_result('{"acc": 0.9}') == {"acc": 0.9}


def test_parse_result_non_dict_json_wrapped():
    # A bare JSON value (not an object) is wrapped under "value".
    assert sandbox._parse_result("[1, 2, 3]") == {"value": [1, 2, 3]}


def test_parse_result_last_line_among_noise():
    noisy = 'loading...\nepoch 1 done\nnot-json {oops\n{"score": 7}\n'
    assert sandbox._parse_result(noisy) == {"score": 7}


def test_parse_result_picks_last_json_line():
    noisy = '{"first": 1}\nmiddle log line\n{"last": 2}'
    assert sandbox._parse_result(noisy) == {"last": 2}


def test_parse_result_empty_is_none():
    assert sandbox._parse_result("") is None
    assert sandbox._parse_result("   \n  ") is None


def test_parse_result_no_json_is_none():
    assert sandbox._parse_result("just some text\nno braces here") is None


def test_parse_result_brace_line_that_is_not_valid_json_is_none():
    # Looks like an object (starts '{' ends '}') but isn't valid JSON → suppressed → None.
    assert sandbox._parse_result("{not: valid, json}") is None


# ── container_name ────────────────────────────────────────────────────────────
def test_container_name():
    assert sandbox.container_name(42) == "lf-exp-42"


# ── run_in_container: completed ───────────────────────────────────────────────
@_aio
async def test_run_completed_parses_last_line_and_hardening_flags(monkeypatch):
    proc = FakeProc(stdout=b'setup noise\n{"accuracy": 0.91}\n', stderr=b"", returncode=0)
    argv = _install_exec(monkeypatch, proc, record=[])

    res = await sandbox.run_in_container(7, "print('x')", wall_clock_s=600)

    assert isinstance(res, sandbox.SandboxResult)
    assert res.status == "completed"
    assert res.result == {"accuracy": 0.91}
    assert res.error is None
    assert res.usage["exit_code"] == 0
    assert res.usage["killed"] is False
    assert res.usage["stdout_bytes"] == len(b'setup noise\n{"accuracy": 0.91}\n')

    # The docker argv carries every hardening flag.
    cmd = argv[0]
    assert cmd[0] == "docker"
    for flag in (
        "--network",
        "none",
        "--memory",
        "--cpus",
        "--pids-limit",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
    ):
        assert flag in cmd, f"missing hardening flag {flag!r}"
    # The deterministic container name is passed through.
    assert "lf-exp-7" in cmd
    # No GPU requested → no --gpus.
    assert "--gpus" not in cmd
    # The running registry is cleaned up after the run.
    assert 7 not in sandbox._RUNNING


@_aio
async def test_run_completed_whole_stdout_json(monkeypatch):
    proc = FakeProc(stdout=b'{"loss": 0.1}', stderr=b"", returncode=0)
    _install_exec(monkeypatch, proc)
    res = await sandbox.run_in_container(1, "print('x')")
    assert res.status == "completed"
    assert res.result == {"loss": 0.1}


@_aio
async def test_run_gpu_adds_gpus_flag_with_device(monkeypatch):
    proc = FakeProc(stdout=b'{"ok": true}', stderr=b"", returncode=0)
    argv = _install_exec(monkeypatch, proc, record=[])

    res = await sandbox.run_in_container(3, "print('x')", requires_gpu=True, gpu_device="2", datasets_dir="/mnt/data")
    assert res.status == "completed"
    cmd = argv[0]
    assert "--gpus" in cmd
    assert "device=2" in cmd
    # datasets_dir → a read-only /data mount.
    assert "/mnt/data:/data:ro" in cmd


@_aio
async def test_run_gpu_without_device_defaults_to_all(monkeypatch):
    proc = FakeProc(stdout=b'{"ok": true}', stderr=b"", returncode=0)
    argv = _install_exec(monkeypatch, proc, record=[])

    res = await sandbox.run_in_container(4, "print('x')", requires_gpu=True, gpu_device=None)
    assert res.status == "completed"
    cmd = argv[0]
    gi = cmd.index("--gpus")
    assert cmd[gi + 1] == "all"


# ── run_in_container: failed ──────────────────────────────────────────────────
@_aio
async def test_run_nonzero_exit_is_failed_with_stderr(monkeypatch):
    proc = FakeProc(stdout=b"", stderr=b"Traceback: boom\n", returncode=1)
    _install_exec(monkeypatch, proc)

    res = await sandbox.run_in_container(9, "raise SystemExit(1)")
    assert res.status == "failed"
    assert res.result is None
    assert "boom" in res.error
    assert res.usage["exit_code"] == 1


@_aio
async def test_run_nonzero_exit_empty_stderr_uses_exit_code(monkeypatch):
    proc = FakeProc(stdout=b"", stderr=b"", returncode=137)
    _install_exec(monkeypatch, proc)

    res = await sandbox.run_in_container(10, "x")
    assert res.status == "failed"
    assert res.error == "exited 137"


@_aio
async def test_run_exit0_but_no_json_is_failed(monkeypatch):
    proc = FakeProc(stdout=b"loading model...\nall done, no json here\n", stderr=b"", returncode=0)
    _install_exec(monkeypatch, proc)

    res = await sandbox.run_in_container(11, "print('no json')")
    assert res.status == "failed"
    assert "no JSON result" in res.error
    assert res.usage["exit_code"] == 0


# ── run_in_container: launch failure ──────────────────────────────────────────
@_aio
async def test_run_launch_failure_is_failed(monkeypatch):
    async def _boom_exec(*_a, **_k):
        raise FileNotFoundError("docker not installed")

    monkeypatch.setattr(sandbox.asyncio, "create_subprocess_exec", _boom_exec)

    res = await sandbox.run_in_container(12, "print('x')")
    assert res.status == "failed"
    assert "sandbox launch error" in res.error
    assert "docker not installed" in res.error
    # Registry still cleaned up in `finally`.
    assert 12 not in sandbox._RUNNING


# ── run_in_container: heartbeat fires before completion ───────────────────────
@_aio
async def test_run_heartbeat_invoked(monkeypatch):
    proc = FakeProc(stdout=b'{"done": 1}', stderr=b"", returncode=0)
    _install_exec(monkeypatch, proc)

    # First wait() reports a timeout (comm not done) → heartbeat fires; second
    # reports done so the loop exits cleanly.
    waits = {"n": 0}
    real_wait = asyncio.wait

    async def _fake_wait(tasks, *, timeout=None):
        waits["n"] += 1
        if waits["n"] == 1:
            # Let the comm task actually finish in the background, but report a
            # timeout this round so the heartbeat branch runs.
            await real_wait(tasks, timeout=0)
            return set(), set(tasks)
        return await real_wait(tasks, timeout=timeout)

    monkeypatch.setattr(sandbox.asyncio, "wait", _fake_wait)

    beats = {"n": 0}

    async def _beat():
        beats["n"] += 1

    res = await sandbox.run_in_container(13, "print('x')", wall_clock_s=600, on_heartbeat=_beat)
    assert res.status == "completed"
    assert beats["n"] >= 1


@_aio
async def test_run_heartbeat_exception_is_swallowed(monkeypatch):
    proc = FakeProc(stdout=b'{"done": 1}', stderr=b"", returncode=0)
    _install_exec(monkeypatch, proc)

    waits = {"n": 0}
    real_wait = asyncio.wait

    async def _fake_wait(tasks, *, timeout=None):
        waits["n"] += 1
        if waits["n"] == 1:
            await real_wait(tasks, timeout=0)
            return set(), set(tasks)
        return await real_wait(tasks, timeout=timeout)

    monkeypatch.setattr(sandbox.asyncio, "wait", _fake_wait)

    async def _bad_beat():
        raise RuntimeError("heartbeat persistence failed")

    # The heartbeat raising must NOT crash the run (it's suppressed).
    res = await sandbox.run_in_container(14, "print('x')", wall_clock_s=600, on_heartbeat=_bad_beat)
    assert res.status == "completed"


# ── run_in_container: wall-clock timeout → killed ─────────────────────────────
@_aio
async def test_run_wall_clock_timeout_kills(monkeypatch):
    # A hanging container: communicate() never resolves on its own. We drive the
    # clock past the budget and report a wait-timeout so the kill branch runs.
    proc = FakeProc(hang=True, returncode=137)

    async def _fake_exec(*_a, **_k):
        return proc

    monkeypatch.setattr(sandbox.asyncio, "create_subprocess_exec", _fake_exec)

    # `_docker_kill` must NOT touch real docker — and after it "kills", the hung
    # communicate must resolve so `await comm` returns. We replace the kill with a
    # stub that cancels the pending communicate task by flipping the proc.
    kill_calls = []

    async def _fake_kill(name):
        kill_calls.append(name)
        proc._hang = False  # next await of communicate would resolve; but comm is already running…

    monkeypatch.setattr(sandbox, "_docker_kill", _fake_kill)

    # The clock: 1st read = start (0), thereafter jump past the budget so the
    # wall-clock branch fires on the first heartbeat round.
    clock = {"t": 0.0}

    def _mono():
        clock["t"] += 100.0
        return clock["t"]

    monkeypatch.setattr(sandbox.time, "monotonic", _mono)

    # Report a wait-timeout (comm not done) so the loop reaches the budget check.
    async def _fake_wait(tasks, *, timeout=None):
        return set(), set(tasks)  # always "timed out"

    monkeypatch.setattr(sandbox.asyncio, "wait", _fake_wait)

    # `await comm` after kill: replace the comm task's awaiting with a resolved value.
    # Since communicate hangs, patch create_task so comm resolves to ('', '') once we
    # need its result. Simplest: give the proc a resolvable communicate after kill.
    async def _resolved_communicate():
        return (b"", b"partial stderr")

    proc.communicate = _resolved_communicate  # type: ignore[method-assign]

    res = await sandbox.run_in_container(15, "while True: pass", wall_clock_s=5)

    assert res.status == "killed"
    assert res.result is None
    assert "wall-clock budget 5s exceeded" in res.error
    assert res.usage["killed"] is True
    assert kill_calls == ["lf-exp-15"]
    assert 15 not in sandbox._RUNNING


# ── kill ──────────────────────────────────────────────────────────────────────
@_aio
async def test_kill_known_running_container(monkeypatch):
    killed = []

    async def _fake_kill(name):
        killed.append(name)

    monkeypatch.setattr(sandbox, "_docker_kill", _fake_kill)
    sandbox._RUNNING[20] = "lf-exp-20"
    try:
        ok = await sandbox.kill(20)
    finally:
        sandbox._RUNNING.pop(20, None)
    assert ok is True
    assert killed == ["lf-exp-20"]


@_aio
async def test_kill_unknown_falls_back_to_deterministic_name(monkeypatch):
    killed = []

    async def _fake_kill(name):
        killed.append(name)

    monkeypatch.setattr(sandbox, "_docker_kill", _fake_kill)
    ok = await sandbox.kill(99)
    assert ok is True
    assert killed == ["lf-exp-99"]


# ── _docker_kill ──────────────────────────────────────────────────────────────
@_aio
async def test_docker_kill_runs_docker_kill(monkeypatch):
    proc = FakeProc(returncode=0)
    argv = _install_exec(monkeypatch, proc, record=[])
    await sandbox._docker_kill("lf-exp-1")
    assert argv[0][:3] == ["docker", "kill", "lf-exp-1"]
    assert proc.waited is True


@_aio
async def test_docker_kill_swallows_errors(monkeypatch):
    async def _boom(*_a, **_k):
        raise OSError("no docker daemon")

    monkeypatch.setattr(sandbox.asyncio, "create_subprocess_exec", _boom)
    # Best-effort: an exception is logged, not raised.
    await sandbox._docker_kill("lf-exp-2")


# ── list_lab_containers ───────────────────────────────────────────────────────
@_aio
async def test_list_lab_containers_parses_names(monkeypatch):
    proc = FakeProc(stdout=b"lf-exp-1\nlf-exp-2\n\nlf-exp-3\n", returncode=0)
    argv = _install_exec(monkeypatch, proc, record=[])

    names = await sandbox.list_lab_containers()
    assert names == ["lf-exp-1", "lf-exp-2", "lf-exp-3"]
    cmd = argv[0]
    assert cmd[:3] == ["docker", "ps", "-a"]
    assert "name=lf-exp-" in cmd


@_aio
async def test_list_lab_containers_empty(monkeypatch):
    proc = FakeProc(stdout=b"", returncode=0)
    _install_exec(monkeypatch, proc)
    assert await sandbox.list_lab_containers() == []


@_aio
async def test_list_lab_containers_error_returns_empty(monkeypatch):
    async def _boom(*_a, **_k):
        raise OSError("docker ps exploded")

    monkeypatch.setattr(sandbox.asyncio, "create_subprocess_exec", _boom)
    assert await sandbox.list_lab_containers() == []


# ── force_remove ──────────────────────────────────────────────────────────────
@_aio
async def test_force_remove_runs_rm_f(monkeypatch):
    proc = FakeProc(returncode=0)
    argv = _install_exec(monkeypatch, proc, record=[])
    await sandbox.force_remove("lf-exp-5")
    assert argv[0] == ["docker", "rm", "-f", "lf-exp-5"]
    assert proc.waited is True


@_aio
async def test_force_remove_swallows_errors(monkeypatch):
    async def _boom(*_a, **_k):
        raise OSError("daemon down")

    monkeypatch.setattr(sandbox.asyncio, "create_subprocess_exec", _boom)
    await sandbox.force_remove("lf-exp-6")  # must not raise
