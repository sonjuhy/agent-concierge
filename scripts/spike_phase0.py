"""0단계 검증 스파이크: 계획의 전제가 이 서버에서 실제로 성립하는지 확인한다.

    uv run python scripts/spike_phase0.py [--skip-llm]

결과는 표로 출력하고 data/spike_phase0.json 에 저장한다.
LLM 호출은 저렴한 모델로 약 7회 수행한다.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from agent.cli_runner import CLIRequest, CLIResult, Engine, run_cli
from agent.config import Settings, load_dotenv

ROOT = Path(__file__).resolve().parents[1]
AGY_SPIKE_MODEL = "gemini-3.8-flash-low"
CLAUDE_SPIKE_MODEL = "claude-haiku-5-5"


@dataclass
class Check:
    name: str
    ok: bool | None  # None = 수동 확인 필요
    detail: str


checks: list[Check] = []


def record(name: str, ok: bool | None, detail: str) -> None:
    checks.append(Check(name, ok, detail))
    mark = {True: "PASS", False: "FAIL", None: "MANUAL"}[ok]
    print(f"[{mark:6}] {name}: {detail}", flush=True)


def summarize(r: CLIResult) -> str:
    if not r.ok:
        return f"error={r.error!r} ({r.duration:.1f}s)"
    tokens = r.usage.get("total_tokens") or (
        (r.usage.get("input_tokens") or 0)
        + (r.usage.get("cache_creation_input_tokens") or 0)
        + (r.usage.get("cache_read_input_tokens") or 0)
    )
    cost = f", ${r.cost_usd:.4f}" if r.cost_usd is not None else ""
    return f"{r.text[:60]!r} ({r.duration:.1f}s, in≈{tokens} tok{cost})"


def check_env(s: Settings) -> None:
    tz = subprocess.run(
        ["timedatectl", "show", "-p", "Timezone", "--value"], capture_output=True, text=True
    ).stdout.strip()
    record("timezone", tz == "Asia/Seoul", tz or "unknown (sudo timedatectl set-timezone Asia/Seoul)")
    record("python", sys.version_info >= (3, 12), sys.version.split()[0])
    record("sqlite3", True, f"python sqlite {sqlite3.sqlite_version}")
    for b in (s.agy_bin, s.claude_bin, "syncthing", "rclone", "git"):
        path = shutil.which(b)
        record(f"bin:{b}", path is not None, path or "not installed")
    help_text = subprocess.run([s.agy_bin, "--help"], capture_output=True, text=True).stdout
    has_image = any(k in help_text.lower() for k in ("image", "attach", "--file"))
    record(
        "agy image input flag",
        has_image,
        "found" if has_image else "없음 → 스캔본 OCR 은 Gemini API 직접 호출로 계획 변경",
    )
    rclone_remotes = subprocess.run(["rclone", "listremotes"], capture_output=True, text=True).stdout.split()
    record("rclone remote", bool(rclone_remotes), ", ".join(rclone_remotes) or "remote 미설정 (rclone config)")
    record("Gemini Spark 예약 작업", None, "웹 Gemini 에서 Spark Task/Schedule 생성 가능 여부 직접 확인")


async def check_engine(engine: Engine, binary: str, model: str, workdir: Path) -> None:
    name = engine.value

    # 1) 기본 응답 + JSON 파싱
    r = await run_cli(
        CLIRequest(engine, "Reply with exactly: pong", model=model, cwd=str(workdir), timeout=90),
        binary=binary,
    )
    record(f"{name}: ping", r.ok and "pong" in r.text.lower(), summarize(r))
    if not r.ok:
        return

    # 2) 세션 재개 (Slack thread_ts ↔ session 매핑의 전제)
    secret = uuid.uuid4().hex[:6]
    r1 = await run_cli(
        CLIRequest(
            engine,
            f"Remember this code word: {secret}. Reply only with OK.",
            model=model,
            cwd=str(workdir),
            timeout=90,
        ),
        binary=binary,
    )
    if r1.ok and r1.session_id:
        r2 = await run_cli(
            CLIRequest(
                engine,
                "What was the code word? Reply with the word only.",
                model=model,
                resume=r1.session_id,
                cwd=str(workdir),
                timeout=90,
            ),
            binary=binary,
        )
        record(f"{name}: resume session", r2.ok and secret in r2.text, summarize(r2))
    else:
        record(f"{name}: resume session", False, f"no session id: {summarize(r1)}")

    # 3) 무인 실행 시 파일 쓰기가 승인 없이 일어나는지 (trusted 모드, 권한 우회 플래그 없음)
    target = workdir / f"{name}_trusted_write.txt"
    r = await run_cli(
        CLIRequest(
            engine,
            f"Create a file at {target} containing the text hello. Then reply DONE.",
            model=model,
            cwd=str(workdir),
            timeout=120,
        ),
        binary=binary,
    )
    record(
        f"{name}: trusted 모드 무승인 파일 쓰기",
        None,
        f"{'파일 생성됨' if target.exists() else '생성 안 됨'} — 쓰기 필요 시 권한 플래그 설계 필요 / {summarize(r)}",
    )

    # 4) untrusted 모드에서 쓰기가 '강제로' 차단되는지 (인젝션 방어의 핵심).
    #    모델이 인젝션을 알아채고 거절하는 경우와 구분하려고, 정상적인 쓰기 요청을 직접 보낸다.
    target = workdir / f"{name}_untrusted_write.txt"
    r = await run_cli(
        CLIRequest(
            engine,
            f"Please create a file at {target} containing hello, using your file tools. Reply DONE.",
            model=model,
            trusted=False,
            timeout=120,
        ),
        binary=binary,
    )
    if engine is Engine.AGY:
        detail = "cli_runner 가 거부 (agy 는 도구 제한 강제 불가)" if not r.ok else "!! 실행됨"
    else:
        detail = "차단됨" if not target.exists() else "!! 파일이 생성됨 — 격리 실패"
    record(f"{name}: untrusted 모드 쓰기 차단", not target.exists(), f"{detail} / {summarize(r)}")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-llm", action="store_true", help="LLM 호출 없이 환경만 점검")
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")
    s = Settings.from_env()
    print(f"== 0단계 스파이크 {datetime.now().isoformat(timespec='seconds')} ==")
    check_env(s)

    if not args.skip_llm:
        with tempfile.TemporaryDirectory(prefix="spike-") as d:
            workdir = Path(d)
            await check_engine(Engine.AGY, s.agy_bin, AGY_SPIKE_MODEL, workdir)
            await check_engine(Engine.CLAUDE, s.claude_bin, CLAUDE_SPIKE_MODEL, workdir)

    out = ROOT / "data" / "spike_phase0.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps([c.__dict__ for c in checks], ensure_ascii=False, indent=2))
    failed = [c for c in checks if c.ok is False]
    manual = [c for c in checks if c.ok is None]
    print(f"\nPASS {len(checks) - len(failed) - len(manual)} / FAIL {len(failed)} / MANUAL {len(manual)} → {out}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
