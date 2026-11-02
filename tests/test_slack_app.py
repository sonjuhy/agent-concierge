import asyncio
from datetime import datetime
from pathlib import Path

import pytest

from agent import agy_profile
from agent.cli_runner import CLIResult, Engine
from agent.config import Settings
from agent.context import KST, build_context
from agent.sessions import Store
from agent.slack_app import Agent, Job, chunk_text

USER = "U_ME"


class FakeClient:
    def __init__(self):
        self.posts, self.reactions = [], []

    async def chat_postMessage(self, **kw):
        self.posts.append(kw)

    async def reactions_add(self, **kw):
        self.reactions.append(("+", kw["name"]))

    async def reactions_remove(self, **kw):
        self.reactions.append(("-", kw["name"]))


class FakeRunner:
    def __init__(self, results=None):
        self.calls, self.results = [], list(results or [])

    async def __call__(self, req, binary):
        self.calls.append(req)
        if self.results:
            return self.results.pop(0)
        sid = "conv-1" if req.engine is Engine.AGY else "sess-1"
        return CLIResult(req.engine, ok=True, text=f"answer from {req.engine}", session_id=sid, duration=1.0)


@pytest.fixture
def settings(tmp_path, monkeypatch):
    (tmp_path / "vault").mkdir()
    token = tmp_path / "token"
    token.write_text("t")
    monkeypatch.setattr(agy_profile, "GLOBAL_TOKEN", token)
    return Settings(
        agy_bin="agy", claude_bin="claude", agy_model=None, claude_model=None,
        untrusted_model="claude-haiku-5-5", agy_timeout=120, claude_timeout=300,
        vault=tmp_path / "vault", agy_home=tmp_path / "agy-home", agy_bwrap=False,
    )


@pytest.fixture
def agent(settings):
    return Agent(settings, Store(":memory:"), FakeClient(), USER, run=FakeRunner())


def job(text, thread="100.1"):
    return Job("D1", thread, thread, text)


async def test_reply_in_thread_and_session_resumed(agent):
    await agent.process(job("안녕"))
    await agent.process(Job("D1", "100.2", "100.1", "이어서"))
    first, second = agent.run.calls
    assert first.engine is Engine.AGY and first.resume is None
    assert second.resume == "conv-1"
    assert "[현재 시각]" in first.prompt and first.prompt.endswith("안녕")
    post = agent.client.posts[0]
    assert post["thread_ts"] == "100.1"
    assert post["blocks"][0] == {"type": "markdown", "text": "answer from agy"}
    assert agent.client.reactions[-2:] == [("-", "hourglass_flowing_sand"), ("+", "white_check_mark")]


async def test_conversation_is_logged_per_thread(settings):
    from agent.chatlog import ChatLog

    store = Store(":memory:")
    a = Agent(settings, store, FakeClient(), USER, run=FakeRunner(), chatlog=ChatLog(settings.vault, store.db))
    await a.process(Job("D1", "1792643715.1", "1792643715.1", "안녕"))
    a.run.results = [CLIResult(Engine.AGY, ok=False, timed_out=True, error="t")]
    await a.process(Job("D1", "1792643799.1", "1792643715.1", "일정 찾아줘"))
    files = list((settings.vault / "_Chat_Logs").rglob("*.md"))
    assert len(files) == 1
    text = files[0].read_text()
    for part in ("· 나\n\n안녕", "answer from agy", "· 나\n\n일정 찾아줘", "시간 초과"):
        assert part in text


async def test_deep_uses_separate_claude_session(agent):
    await agent.process(job("안녕"))
    await agent.process(job("분석해줘 --deep"))
    await agent.process(job("다시 agy"))
    agy1, claude1, agy2 = agent.run.calls
    assert claude1.engine is Engine.CLAUDE and claude1.resume is None
    assert agy2.engine is Engine.AGY and agy2.resume == "conv-1"


async def test_tainted_thread_goes_untrusted_and_stores_no_session(agent):
    agent.store.mark_tainted("D1:100.1", "test")
    await agent.process(job("요약 다시"))
    req = agent.run.calls[0]
    assert req.engine is Engine.CLAUDE and not req.trusted and req.resume is None
    assert agent.store.get_session("D1:100.1", Engine.CLAUDE) is None


async def test_failure_and_timeout_messages(agent):
    agent.run.results = [
        CLIResult(Engine.AGY, ok=False, error="boom"),
        CLIResult(Engine.AGY, ok=False, timed_out=True, error="timeout"),
    ]
    await agent.process(job("a"))
    await agent.process(job("b", thread="200.1"))
    texts = [p["text"] for p in agent.client.posts]
    assert "실행 실패: boom" in texts[0]
    assert "시간 초과" in texts[1]
    assert ("+", "x") in agent.client.reactions
    assert agent.store.get_session("D1:100.1", Engine.AGY) is None
    assert agent.store.db.execute("SELECT count(*), sum(ok) FROM calls").fetchone() == (2, 0)


async def test_on_event_filters_users_and_enqueues(agent):
    await agent.on_event({"user": "U_OTHER", "channel": "D1", "ts": "1", "text": "hi"})
    await agent.on_event({"user": USER, "channel": "D1", "ts": "2", "text": "hi", "subtype": "message_changed"})
    await agent.on_event({"user": USER, "channel": "C1", "ts": "3", "text": "<@UBOT> "})
    assert all(q.empty() for q in agent.queues.values())

    await agent.on_event({"user": USER, "channel": "C1", "ts": "4", "text": "<@UBOT> 요약 --deep"})
    await agent.on_event({"user": USER, "channel": "D1", "ts": "5", "thread_ts": "4.5", "text": "안녕"})
    claude_job = agent.queue_for("owner", Engine.CLAUDE).get_nowait()
    agy_job = agent.queue_for("owner", Engine.AGY).get_nowait()
    assert claude_job.text == "요약 --deep" and claude_job.thread_ts == "4"
    assert agy_job.thread_ts == "4.5"


async def test_workers_process_queue(agent):
    agent.start()
    try:
        await agent.on_event({"user": USER, "channel": "D1", "ts": "9", "text": "hi"})
        await asyncio.wait_for(agent.queue_for("owner", Engine.AGY).join(), 2)
    finally:
        await agent.stop()
    assert agent.client.posts[-1]["text"] == "answer from agy"


def test_slack_to_plain():
    from agent.slack_app import slack_to_plain

    assert slack_to_plain("<mailto:a@b.com|a@b.com> 에게 초안") == "a@b.com 에게 초안"
    assert slack_to_plain("<https://x.com/a?b=1|x.com> 참고") == "x.com 참고"
    assert slack_to_plain("<https://x.com> &lt;tag&gt; &amp;") == "https://x.com <tag> &"


async def test_on_event_normalizes_slack_links(agent):
    await agent.on_event({"user": USER, "channel": "D1", "ts": "1", "text": "<mailto:a@b.com|a@b.com> 에게 메일"})
    assert agent.queue_for("owner", Engine.AGY).get_nowait().text == "a@b.com 에게 메일"


def test_chunk_text():
    assert chunk_text("a\nb\n", size=10) == ["a\nb\n"]
    parts = chunk_text("x" * 25, size=10)
    assert parts == ["x" * 10, "x" * 10, "x" * 5]
    parts = chunk_text("aaaa\nbbbb\ncccc\n", size=10)
    assert parts == ["aaaa\nbbbb\n", "cccc\n"]
    assert all(len(p) <= 10 for p in chunk_text("ab\n" * 50, size=10))


def test_build_context():
    ctx = build_context(datetime(2026, 10, 22, 14, 5, tzinfo=KST))
    assert "[현재 시각] 2026-10-22 14:05 (목) KST" in ctx
    assert "[이번 주] 10/19(월)" in ctx and "10/25(일)" in ctx
    assert "[다음 주] 10/26(월)" in ctx


def test_store_sessions_upsert(tmp_path):
    s = Store(tmp_path / "db" / "a.db")
    s.set_session("t", Engine.AGY, "1")
    s.set_session("t", Engine.AGY, "2")
    assert s.get_session("t", Engine.AGY) == "2"
    assert s.get_session("t", Engine.CLAUDE) is None
    assert not s.is_tainted("t")
    s.mark_tainted("t", "x")
    s.mark_tainted("t", "x")
    assert s.is_tainted("t")
