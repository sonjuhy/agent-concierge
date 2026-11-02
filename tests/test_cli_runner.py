import json
import os
import time
from pathlib import Path

import pytest

from agent.cli_runner import (
    AGY_MAX_PROMPT_BYTES,
    CLIRequest,
    Engine,
    build_command,
    parse_output,
    run_cli,
)

FAKE = str(Path(__file__).parent / "fakes" / "fake_cli.py")


@pytest.fixture(autouse=True)
def _fake_exec():
    os.chmod(FAKE, 0o755)


def test_build_agy_puts_prompt_last_and_no_stdin():
    cmd, stdin = build_command(CLIRequest(Engine.AGY, "hi", model="m", resume="c1"), "agy")
    assert cmd[-2:] == ["--print", "hi"]
    assert ["--conversation", "c1"] == cmd[cmd.index("--conversation"):][:2]
    assert "--dangerously-skip-permissions" not in cmd
    assert stdin is None


async def test_agy_untrusted_rejected():
    with pytest.raises(ValueError):
        build_command(CLIRequest(Engine.AGY, "hi", trusted=False), "agy")
    r = await run_cli(CLIRequest(Engine.AGY, "hi", trusted=False), binary=FAKE)
    assert not r.ok and "untrusted" in r.error


def test_build_claude_untrusted_disables_tools_and_uses_stdin():
    cmd, stdin = build_command(CLIRequest(Engine.CLAUDE, "hi", trusted=False), "claude")
    i = cmd.index("--tools")
    assert cmd[i + 1] == ""
    assert "--strict-mcp-config" in cmd
    assert stdin == b"hi"


def test_agy_prompt_too_long_rejected():
    with pytest.raises(ValueError):
        build_command(CLIRequest(Engine.AGY, "x" * (AGY_MAX_PROMPT_BYTES + 1)), "agy")


def test_parse_agy_error():
    r = parse_output(Engine.AGY, json.dumps({"status": "ERROR", "error": "boom"}).encode())
    assert not r.ok and r.error == "boom"


def test_parse_bad_json():
    r = parse_output(Engine.CLAUDE, b"oops")
    assert not r.ok and "invalid JSON" in r.error


async def test_run_agy_ok(monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "agy_ok")
    r = await run_cli(CLIRequest(Engine.AGY, "안녕"), binary=FAKE)
    assert r.ok, r.error
    assert r.text == "echo:안녕"
    assert r.session_id == "conv-1"
    assert r.returncode == 0


async def test_run_claude_ok_via_stdin(monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "claude_ok")
    r = await run_cli(CLIRequest(Engine.CLAUDE, "긴 프롬프트"), binary=FAKE)
    assert r.ok, r.error
    assert r.text == "echo:긴 프롬프트"
    assert r.session_id == "sess-1"
    assert r.cost_usd == 0.01


async def test_untrusted_runs_in_empty_tempdir(monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_MODE", "claude_ok")
    r = await run_cli(CLIRequest(Engine.CLAUDE, "x", trusted=False, cwd=str(tmp_path)), binary=FAKE)
    cwd = r.raw["cwd"]
    assert cwd != str(tmp_path)
    assert "agent-untrusted-" in cwd
    assert not Path(cwd).exists()  # 실행 후 정리됨


async def test_trusted_uses_given_cwd(monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_MODE", "claude_ok")
    r = await run_cli(CLIRequest(Engine.CLAUDE, "x", cwd=str(tmp_path)), binary=FAKE)
    assert r.raw["cwd"] == str(tmp_path)


async def test_wrapper_and_env_are_applied(monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "agy_ok")
    req = CLIRequest(Engine.AGY, "x", wrapper=["/usr/bin/env", "WRAPPED=1"], env={"HOME": "/profile"})
    r = await run_cli(req, binary=FAKE)
    assert r.ok, r.error
    assert r.raw["env"] == {"HOME": "/profile", "WRAPPED": "1"}


async def test_cli_error_status(monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "agy_error")
    r = await run_cli(CLIRequest(Engine.AGY, "x"), binary=FAKE)
    assert not r.ok and "boom" in r.error


async def test_nonzero_exit_is_failure_with_stderr(monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "exit_nonzero")
    r = await run_cli(CLIRequest(Engine.AGY, "x"), binary=FAKE)
    assert not r.ok
    assert r.returncode == 3
    assert "fatal" in r.error


async def test_missing_binary():
    r = await run_cli(CLIRequest(Engine.AGY, "x"), binary="/nonexistent/agy")
    assert not r.ok and "not found" in r.error


async def test_timeout_kills_whole_process_group(monkeypatch, tmp_path):
    pidfile = tmp_path / "child.pid"
    monkeypatch.setenv("FAKE_MODE", "hang_with_child")
    monkeypatch.setenv("FAKE_PIDFILE", str(pidfile))
    started = time.monotonic()
    r = await run_cli(CLIRequest(Engine.AGY, "x", timeout=1.5), binary=FAKE)
    assert r.timed_out and not r.ok
    assert time.monotonic() - started < 5
    child = int(pidfile.read_text())
    # 손자 프로세스도 SIGKILL 되어야 한다 (좀비가 아직 reap 안 됐을 수 있으니 상태로 확인)
    time.sleep(0.2)
    stat = Path(f"/proc/{child}/stat")
    assert not stat.exists() or stat.read_text().split()[2] == "Z"
