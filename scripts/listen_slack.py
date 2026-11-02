"""Slack 이벤트 수신 E2E 확인. Socket Mode 로 잠시 접속해 실제 이벤트를 기다린다.

    uv run python scripts/listen_slack.py [--timeout 300]

허용 사용자(SLACK_ALLOWED_USER_ID)가 봇에게 DM 을 보내거나 채널에서 @멘션하면:
  1) ✅ 리액션 (reactions:write)
  2) 스레드에 [확인] 버튼 메시지 (chat:write + Block Kit)
  3) 버튼을 누르면 메시지를 갱신하고 종료 (Interactivity)
다른 사용자의 이벤트는 무시한다. 메시지 본문은 출력하지 않는다.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
from pathlib import Path

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from agent.config import load_dotenv  # noqa: E402

seen: set[str] = set()


def log(msg: str) -> None:
    print(msg, flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=float, default=300)
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")
    allowed = os.environ["SLACK_ALLOWED_USER_ID"]
    app = App(token=os.environ["SLACK_BOT_TOKEN"])
    done = threading.Event()

    def handle(kind: str, event: dict, client) -> None:
        if event.get("user") != allowed or event.get("bot_id"):
            log(f"[skip] {kind} from {event.get('user')} (허용 사용자 아님)")
            return
        seen.add(kind)
        log(f"[event] {kind} 수신 (channel_type={event.get('channel_type', 'channel')}, ts={event['ts']})")
        client.reactions_add(channel=event["channel"], timestamp=event["ts"], name="white_check_mark")
        log("[ok] 리액션 추가")
        client.chat_postMessage(
            channel=event["channel"],
            thread_ts=event["ts"],
            text=f"{kind} 수신 확인. 버튼을 눌러 Interactivity 를 확인하세요.",
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text": f"*{kind}* 수신 확인 ✅\n버튼을 눌러 Interactivity 를 확인하세요."}},
                {"type": "actions", "elements": [
                    {"type": "button", "action_id": "e2e_confirm", "style": "primary", "text": {"type": "plain_text", "text": "확인"}},
                ]},
            ],
        )
        log("[ok] 버튼 메시지 전송 — Slack 에서 [확인] 을 눌러주세요")

    @app.event("message")
    def on_message(event, client):
        if event.get("channel_type") == "im" and not event.get("subtype"):
            handle("DM (message.im)", event, client)

    @app.event("app_mention")
    def on_mention(event, client):
        handle("멘션 (app_mention)", event, client)

    @app.action("e2e_confirm")
    def on_click(ack, body, client):
        ack()
        if body["user"]["id"] != allowed:
            return
        client.chat_update(
            channel=body["channel"]["id"],
            ts=body["message"]["ts"],
            text="Interactivity 확인 완료",
            blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": "Interactivity 확인 완료 ✅"}}],
        )
        seen.add("버튼 (interactivity)")
        log("[event] 버튼 클릭 수신 — Interactivity OK")
        done.set()

    handler = SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    handler.connect()
    log(f"[ready] Socket Mode 연결됨. {args.timeout:.0f}초 동안 DM/멘션을 기다립니다.")
    done.wait(args.timeout)
    handler.close()
    log(f"\n확인된 항목: {', '.join(sorted(seen)) or '없음'}")
    return 0 if "버튼 (interactivity)" in seen else 1


if __name__ == "__main__":
    sys.exit(main())
