"""Slack 연결 상태 점검. 토큰 값은 출력하지 않는다.

    uv run python scripts/check_slack.py

- 봇 토큰(xoxb): auth.test, 부여된 scope(x-oauth-scopes 헤더) 확인
- 앱 토큰(xapp): apps.connections.open 으로 Socket Mode 사용 가능 여부 확인
- 허용 사용자: users.info 로 존재 여부 확인
- 봇이 들어가 있는 채널 목록
"""

from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from agent.config import load_dotenv  # noqa: E402

REQUIRED_SCOPES = {
    "app_mentions:read", "chat:write", "channels:history", "groups:history",
    "im:history", "im:read", "im:write", "reactions:write",
}


def call(method: str, token: str, params: dict | None = None) -> tuple[dict, dict]:
    data = urllib.parse.urlencode(params or {}).encode()
    req = urllib.request.Request(
        f"https://slack.com/api/{method}", data=data, headers={"Authorization": f"Bearer {token}"}
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp), dict(resp.headers)


def report(ok: bool | None, name: str, detail: str) -> bool:
    mark = {True: "OK  ", False: "FAIL", None: "WARN"}[ok]
    print(f"[{mark}] {name}: {detail}")
    return ok is not False


def main() -> int:
    load_dotenv(ROOT / ".env")
    bot = os.environ.get("SLACK_BOT_TOKEN", "")
    app = os.environ.get("SLACK_APP_TOKEN", "")
    user = os.environ.get("SLACK_ALLOWED_USER_ID", "")
    ok = True

    if not bot.startswith("xoxb-"):
        return 0 if report(False, "SLACK_BOT_TOKEN", "xoxb- 로 시작해야 함") else 1
    r, headers = call("auth.test", bot)
    if not r.get("ok"):
        report(False, "bot token", f"auth.test 실패: {r.get('error')}")
        return 1
    ok &= report(True, "bot token", f"workspace={r['team']} bot=@{r['user']} (user_id={r['user_id']}, bot_id={r.get('bot_id')})")
    bot_user_id = r["user_id"]

    scopes = set(filter(None, headers.get("x-oauth-scopes", "").split(",")))
    missing = REQUIRED_SCOPES - scopes
    print(f"       현재 scope: {', '.join(sorted(scopes)) or '(없음)'}")
    ok &= report(not missing, "bot scopes", "모두 있음" if not missing else f"없음: {', '.join(sorted(missing))} → 추가 후 Reinstall to Workspace")

    if not app.startswith("xapp-"):
        ok &= report(False, "SLACK_APP_TOKEN", "xapp- 로 시작해야 함")
    else:
        r, _ = call("apps.connections.open", app)
        detail = "Socket Mode 연결 URL 발급됨" if r.get("ok") else f"실패: {r.get('error')}"
        if r.get("error") == "not_allowed_token_type":
            detail += " (connections:write 권한의 앱 레벨 토큰이어야 함)"
        ok &= report(bool(r.get("ok")), "app token / Socket Mode", detail)

    if user:
        r, _ = call("users.info", bot, {"user": user})
        if r.get("ok"):
            u = r["user"]
            ok &= report(True, "allowed user", f"{u.get('real_name') or u.get('name')} ({user}), deleted={u.get('deleted')}")
        elif r.get("error") == "missing_scope":
            # 데몬은 이벤트의 user ID 만 비교하므로 users:read 는 필수가 아니다
            report(None, "allowed user", f"{user} — users:read 권한이 없어 존재 여부는 확인 못 함 (필수 아님)")
        else:
            ok &= report(False, "allowed user", f"users.info 실패: {r.get('error')}")
    else:
        ok &= report(False, "SLACK_ALLOWED_USER_ID", "비어 있음")

    types = ",".join(t for t, s in (("public_channel", "channels:read"), ("private_channel", "groups:read")) if s in scopes)
    r, _ = call("users.conversations", bot, {"user": bot_user_id, "types": types or "public_channel", "limit": 200})
    if r.get("ok"):
        names = [f"#{c['name']}" for c in r.get("channels", [])]
        report(True if names else None, "joined channels", ", ".join(names) or "없음 — DM 으로만 사용하거나 채널에서 /invite 필요")
    else:
        report(None, "joined channels", f"조회 실패: {r.get('error')} (channels:read/groups:read 없으면 정상)")

    print("\n이벤트 구독(app_mention, message.im)·Interactivity 는 API 로 확인할 수 없어 데몬 연결 후 실제 메시지로 확인")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
