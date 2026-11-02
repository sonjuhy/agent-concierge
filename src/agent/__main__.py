"""코어 데몬 실행: uv run python -m agent"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from agent import scheduler
from agent.actions import PendingStore, Workspace
from agent.chatlog import ChatLog
from agent.cli_runner import run_cli
from agent.config import Settings, load_dotenv
from agent.sessions import Store
from agent.slack_app import Agent
from agent.tools import google
from agent.users import Users

ROOT = Path(__file__).resolve().parents[2]


async def main() -> None:
    load_dotenv(ROOT / ".env")
    logging.basicConfig(
        level=os.environ.get("AGENT_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = Settings.from_env()
    store = Store(ROOT / "data" / "agent.db")
    app = AsyncApp(token=os.environ["SLACK_BOT_TOKEN"])
    users = Users.load(settings)  # data/users.yaml 이 없으면 1인 구성 (docs/MULTI_USER.md)
    sched = scheduler.create(settings, app.client, store, users, jobs_db=ROOT / "data" / "jobs.db")
    sched.start()
    run = lambda req, b: run_cli(req, binary=b)  # noqa: E731
    pending = PendingStore(store.db)
    workspaces, chatlogs = {}, {}
    for u in users.users:
        briefing = os.environ.get("AGENT_BRIEFING_TIME") if u.is_owner else None
        scheduler.ensure_briefing(sched, briefing or scheduler.DEFAULT_BRIEFING_TIME, u.name)
        # 소유자: claude 플래너·메일 요약 / 가족: 각자 agy 로 요청 해석, 메일은 목록만 (API 키 없이)
        workspaces[u.name] = Workspace(
            u.settings(settings, users.shared), pending, run=run, sched=sched, owner=u.name,
            api=google if u.is_owner else google.GoogleApi(u.google_token),
            planner="claude" if u.is_owner else "agy", mail_mode="summary" if u.is_owner else "list",
        )
        chatlogs[u.name] = ChatLog(u.vault, store.db)
    agent = Agent(settings, store, app.client, users, run=run, chatlog=chatlogs, workspace=workspaces)

    @app.action("pending_approve")
    async def on_approve(ack, body):
        await ack()
        await agent.on_action(body, approve=True)

    @app.action("pending_cancel")
    async def on_cancel(ack, body):
        await ack()
        await agent.on_action(body, approve=False)

    @app.action("todo_done")
    async def on_todo_done(ack, body):
        await ack()
        await agent.on_todo_done(body)

    @app.event("message")
    async def on_message(event):
        if event.get("channel_type") == "im":
            await agent.on_event(event)

    @app.event("app_mention")
    async def on_mention(event):
        await agent.on_event(event)

    agent.start()
    handler = AsyncSocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    logging.getLogger(__name__).info("agent started (users=%s, bwrap=%s)",
                                     ", ".join(f"{u.name}({u.role})" for u in users.users), settings.agy_bwrap)
    try:
        await handler.start_async()
    finally:
        sched.shutdown(wait=False)
        await agent.stop()


if __name__ == "__main__":
    asyncio.run(main())
