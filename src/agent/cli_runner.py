"""agy / claude -p 를 서브프로세스로 실행하는 공통 래퍼.

- 각 호출은 새 프로세스 그룹에서 실행하고, 타임아웃 시 그룹 전체를 SIGKILL 한다.
- 두 CLI 모두 --output-format json 결과를 CLIResult 하나로 정규화한다.
- trusted=False 이면 도구/MCP 를 끄고 빈 임시 디렉터리에서 실행한다 (외부 텍스트 처리용).
  도구 제한이 강제되는 claude 만 허용하고, agy 는 거부한다.

실측 출력 형식 (2026-10-22):
  agy    -> {"conversation_id", "status": "SUCCESS"|"ERROR", "response", "error"?, "usage", ...}
  claude -> {"session_id", "is_error", "result", "subtype", "total_cost_usd", "usage", ...}
"""

from __future__ import annotations

import asyncio
import enum
import json
import logging
import os
import signal
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

# Linux 단일 인자 최대 길이(MAX_ARG_STRLEN=128KiB). agy 는 프롬프트를 argv 로만 받는다.
AGY_MAX_PROMPT_BYTES = 120 * 1024


class Engine(enum.StrEnum):
    AGY = "agy"
    CLAUDE = "claude"


@dataclass
class CLIResult:
    engine: Engine
    ok: bool
    text: str = ""
    session_id: str | None = None
    error: str | None = None
    timed_out: bool = False
    returncode: int | None = None
    duration: float = 0.0
    usage: dict[str, Any] = field(default_factory=dict)
    cost_usd: float | None = None
    raw: dict[str, Any] | None = None


@dataclass
class CLIRequest:
    engine: Engine
    prompt: str
    model: str | None = None
    resume: str | None = None  # agy: conversation_id, claude: session_id
    system_prompt: str | None = None  # claude 전용 (--append-system-prompt)
    trusted: bool = True
    cwd: str | None = None
    timeout: float = 120.0
    extra_args: list[str] = field(default_factory=list)
    wrapper: list[str] = field(default_factory=list)  # 예: bwrap 인자 (agy_profile.bwrap_prefix)
    env: dict[str, str] = field(default_factory=dict)


def build_command(req: CLIRequest, binary: str) -> tuple[list[str], bytes | None]:
    """실행할 argv 와 stdin 입력을 만든다."""
    if req.engine is Engine.AGY:
        if not req.trusted:
            # 실측(2026-10-22): agy -p 는 승인 없이 도구를 실행하고 --sandbox 로도 파일 쓰기가 막히지 않는다.
            raise ValueError("agy cannot run untrusted content (no enforced tool restriction); use claude")
        if len(req.prompt.encode()) > AGY_MAX_PROMPT_BYTES:
            raise ValueError(f"agy prompt exceeds {AGY_MAX_PROMPT_BYTES} bytes")
        cmd = [binary, "--output-format", "json"]
        if req.model:
            cmd += ["--model", req.model]
        if req.resume:
            cmd += ["--conversation", req.resume]
        cmd += req.extra_args
        # -p 바로 뒤에 프롬프트를 붙여야 다른 플래그를 프롬프트로 오인하지 않는다.
        cmd += ["--print", req.prompt]
        return cmd, None

    cmd = [binary, "-p", "--output-format", "json"]
    if req.model:
        cmd += ["--model", req.model]
    if req.resume:
        cmd += ["--resume", req.resume]
    if req.system_prompt:
        cmd += ["--append-system-prompt", req.system_prompt]
    if not req.trusted:
        cmd += ["--tools", "", "--strict-mcp-config"]
    cmd += req.extra_args
    # claude 는 stdin 프롬프트를 지원하므로 길이 제한을 피한다.
    return cmd, req.prompt.encode()


def parse_output(engine: Engine, stdout: bytes) -> CLIResult:
    try:
        data = json.loads(stdout.decode("utf-8", errors="replace").strip() or "null")
    except json.JSONDecodeError as e:
        return CLIResult(engine, ok=False, error=f"invalid JSON output: {e}")
    if not isinstance(data, dict):
        return CLIResult(engine, ok=False, error="empty or non-object JSON output")

    if engine is Engine.AGY:
        ok = data.get("status") == "SUCCESS"
        return CLIResult(
            engine,
            ok=ok,
            text=(data.get("response") or "").strip(),
            session_id=data.get("conversation_id") or None,
            error=None if ok else (data.get("error") or f"status={data.get('status')}"),
            usage=data.get("usage") or {},
            raw=data,
        )

    ok = not data.get("is_error") and data.get("subtype") == "success"
    return CLIResult(
        engine,
        ok=ok,
        text=(data.get("result") or "").strip(),
        session_id=data.get("session_id") or None,
        error=None if ok else (data.get("result") or f"subtype={data.get('subtype')}"),
        usage=data.get("usage") or {},
        cost_usd=data.get("total_cost_usd"),
        raw=data,
    )


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


async def run_cli(req: CLIRequest, binary: str | None = None) -> CLIResult:
    binary = binary or req.engine.value
    started = time.monotonic()
    try:
        cmd, stdin = build_command(req, binary)
    except ValueError as e:
        return CLIResult(req.engine, ok=False, error=str(e))
    cmd = [*req.wrapper, *cmd]

    env = {**os.environ, "TZ": os.environ.get("TZ", "Asia/Seoul"), **req.env}
    with tempfile.TemporaryDirectory(prefix="agent-untrusted-") as sandbox:
        cwd = req.cwd if req.trusted else sandbox
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
                start_new_session=True,  # 자식까지 한 번에 죽이기 위한 별도 프로세스 그룹
            )
        except FileNotFoundError:
            return CLIResult(req.engine, ok=False, error=f"binary not found: {binary}")

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(stdin), timeout=req.timeout)
        except (TimeoutError, asyncio.CancelledError) as e:
            _kill_group(proc)
            await proc.wait()
            if isinstance(e, asyncio.CancelledError):
                raise
            log.warning("%s timed out after %.0fs", req.engine, req.timeout)
            return CLIResult(
                req.engine,
                ok=False,
                error=f"timeout after {req.timeout:.0f}s",
                timed_out=True,
                returncode=proc.returncode,
                duration=time.monotonic() - started,
            )
        finally:
            # 정상 종료 후에도 남은 손자 프로세스가 있으면 정리
            _kill_group(proc)

    result = parse_output(req.engine, stdout)
    result.returncode = proc.returncode
    result.duration = time.monotonic() - started
    if proc.returncode != 0 and result.ok:
        result.ok = False
        result.error = f"exit code {proc.returncode}"
    if not result.ok and stderr:
        tail = stderr.decode("utf-8", errors="replace").strip()[-500:]
        result.error = f"{result.error}\n{tail}" if result.error else tail
    return result
