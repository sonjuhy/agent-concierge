"""무인 운영 헬스체크: CLI 인증, Google 토큰, Syncthing 동기화, 색인기, 디스크.

아침 브리핑에 포함되고, 이상이 있으면 브리핑 맨 위에 경고로 올린다.
    uv run python -m agent.health      # 수동 점검
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import timezone
from pathlib import Path

from agent.cli_runner import CLIRequest, Engine, run_cli
from agent.config import Settings
from agent.context import KST
from agent.router import build_request
from agent.tools.google_auth import NotAuthorized, load_credentials, missing_scopes

SYNCTHING_CONFIG = Path("~/.local/state/syncthing/config.xml").expanduser()
DISK_WARN_RATIO = 0.9


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


async def check_agy(s: Settings) -> Check:
    _, req = build_request(s, "Reply with exactly: pong")
    req.timeout = 90
    r = await run_cli(req, binary=s.agy_bin)
    ok = r.ok and "pong" in r.text.lower()
    return Check("agy", ok, f"{r.duration:.0f}s" if ok else (r.error or r.text)[:200])


async def check_claude(s: Settings) -> Check:
    req = CLIRequest(Engine.CLAUDE, "Reply with exactly: pong", model=s.untrusted_model, trusted=False, timeout=90)
    r = await run_cli(req, binary=s.claude_bin)
    ok = r.ok and "pong" in r.text.lower()
    return Check("claude", ok, f"{r.duration:.0f}s" if ok else (r.error or r.text)[:200])


def check_google() -> Check:
    try:
        creds = load_credentials()
    except NotAuthorized as e:
        return Check("Google", False, str(e)[:200])
    if missing := missing_scopes():
        names = ", ".join(s.rsplit("/", 1)[-1] for s in missing)
        return Check("Google", False, f"재로그인 필요 — 추가된 권한 없음: {names} (uv run python -m agent.tools.google_auth)")
    expiry = creds.expiry.replace(tzinfo=timezone.utc).astimezone(KST) if creds.expiry else None
    return Check("Google", True, f"토큰 유효 (자동 갱신{f', 현재 토큰 {expiry:%H:%M}까지' if expiry else ''})")


def _syncthing_get(path: str, key: str, address: str):
    req = urllib.request.Request(f"http://{address}{path}", headers={"X-API-Key": key})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.load(resp)


def check_syncthing(config: Path = SYNCTHING_CONFIG) -> Check:
    try:
        root = ET.parse(config).getroot()
        key, address = root.findtext("gui/apikey") or "", root.findtext("gui/address") or "127.0.0.1:8384"
        me = _syncthing_get("/rest/system/status", key, address)["myID"]
        names = {d["deviceID"]: d["name"] for d in _syncthing_get("/rest/config/devices", key, address)}
        conns = _syncthing_get("/rest/system/connections", key, address)["connections"]
        folders = _syncthing_get("/rest/config/folders", key, address)
        problems = []
        for dev_id, c in conns.items():
            if dev_id != me and not c.get("connected"):
                problems.append(f"{names.get(dev_id, dev_id[:7])} 연결 끊김")
        for f in folders:
            st = _syncthing_get(f"/rest/db/status?folder={f['id']}", key, address)
            if st.get("state") == "error" or st.get("errors") or st.get("pullErrors"):
                problems.append(f"{f.get('label') or f['id']} 오류({st.get('errors') or st.get('pullErrors')})")
        if problems:
            return Check("Syncthing", False, ", ".join(problems))
        peers = [names.get(d, d[:7]) for d in conns if d != me]
        return Check("Syncthing", True, f"{', '.join(peers)} 연결, 폴더 {len(folders)}개 정상")
    except Exception as e:
        return Check("Syncthing", False, f"API 확인 실패: {e}"[:200])


def check_unit(unit: str, label: str) -> Check:
    """systemd 사용자 oneshot 서비스(색인, Drive 동기화)의 마지막 실행 결과."""
    out = subprocess.run(
        ["systemctl", "--user", "show", unit, "-p", "Result", "-p", "ExecMainExitTimestamp"],
        capture_output=True, text=True,
    ).stdout
    info = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    ok = info.get("Result") == "success"
    return Check(label, ok, f"마지막 실행 {info.get('ExecMainExitTimestamp') or '-'} ({info.get('Result', '?')})")


def check_disk(path: Path) -> Check:
    u = shutil.disk_usage(path)
    ratio = u.used / u.total
    return Check("디스크", ratio < DISK_WARN_RATIO, f"{ratio:.0%} 사용, 남은 공간 {u.free / 2**30:.0f}GB")


def check_family(users) -> list[Check]:
    """가족마다 agy·Google 로그인 상태 (토큰 파일이 있는지·권한이 다 있는지만 본다, 호출은 하지 않음)."""
    from agent.agy_profile import TOKEN_NAME, settings_path

    out = []
    for u in users.users:
        if u.is_owner:
            continue
        problems = []
        if not (settings_path(u.agy_home).parent / TOKEN_NAME).exists():
            problems.append("agy 로그인 안 됨")
        if not u.google_token.exists():
            problems.append("Google 연결 안 됨")
        elif missing := missing_scopes(u.google_token):
            problems.append("Google 권한 부족: " + ", ".join(s.rsplit("/", 1)[-1] for s in missing))
        out.append(Check(f"가족:{u.name}", not problems, ", ".join(problems) or "agy·Google 연결됨"))
    return out


async def run_all(s: Settings, users=None) -> list[Check]:
    agy, claude = await asyncio.gather(check_agy(s), check_claude(s))
    rest = await asyncio.gather(
        asyncio.to_thread(check_google),
        asyncio.to_thread(check_syncthing),
        asyncio.to_thread(check_unit, "indexer.service", "색인"),
        asyncio.to_thread(check_unit, "gdrive-sync.service", "Drive 동기화"),
        asyncio.to_thread(check_disk, s.vault),
    )
    return [agy, claude, *rest, *(check_family(users) if users else [])]


def format_checks(checks: list[Check]) -> str:
    return "\n".join(f"{'✅' if c.ok else '⚠️'} {c.name}: {c.detail}" for c in checks)


if __name__ == "__main__":
    from agent.config import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    print(format_checks(asyncio.run(run_all(Settings.from_env()))))
