"""DM 대화 이어가기와 이전 대화 전달 (2026-10-26 맥락 누락 사례 재현)."""

from datetime import datetime

from agent.cli_runner import Engine
from agent.context import KST
from agent.sessions import Store
from agent.slack_app import Agent, Job, history_block, unseen_turns
from agent.tools import google
from agent.tools.google import Event
from tests.test_slack_app import USER, FakeClient, FakeRunner, settings  # noqa: F401


class TsClient(FakeClient):
    """chat_postMessage 가 ts 를 돌려주는 가짜 Slack 클라이언트."""

    async def chat_postMessage(self, **kw):
        self.posts.append(kw)
        return {"ts": f"9{len(self.posts)}.0"}


def dm(ts, text, thread_ts=None):
    e = {"user": USER, "channel": "D1", "ts": ts, "text": text, "channel_type": "im"}
    if thread_ts:
        e["thread_ts"] = thread_ts
    return e


def test_dm_top_level_messages_continue_within_window(settings, monkeypatch):  # noqa: F811
    agent = Agent(settings, Store(":memory:"), TsClient(), USER, run=FakeRunner())
    a = agent._job_for(dm("100.0", "11월 일정 조사해줘"), "11월 일정 조사해줘")
    b = agent._job_for(dm("101.0", "이 중에서 우리의 일정이 아닌게 무엇이지?"), "…")
    assert a.thread_key == b.thread_key == "D1:100.0"
    assert a.thread_ts is None and b.thread_ts is None  # DM 본문에 답한다

    # 30분이 지나면 새 대화
    now = __import__("time").time()
    monkeypatch.setattr("agent.sessions.time.time", lambda: now + 31 * 60)
    c = agent._job_for(dm("200.0", "안녕"), "안녕")
    assert c.thread_key == "D1:200.0"


async def test_thread_reply_under_bot_answer_joins_conversation(settings):  # noqa: F811
    client = TsClient()
    agent = Agent(settings, Store(":memory:"), client, USER, run=FakeRunner())
    job = agent._job_for(dm("100.0", "안녕"), "안녕")
    await agent.process(job)
    bot_ts = "91.0"  # TsClient 가 첫 답에 돌려준 ts
    reply = agent._job_for(dm("300.0", "이어서", thread_ts=bot_ts), "이어서")
    assert reply.thread_key == "D1:100.0" and reply.thread_ts == bot_ts


def test_unseen_turns_and_history_block():
    h = [("user", "안녕"), ("agy · 2.0s", "안녕하세요"), ("user", "11월 일정"), ("calendar", "8건 …")]
    assert unseen_turns(h, Engine.AGY, has_session=True) == h[2:]
    assert unseen_turns(h, Engine.AGY, has_session=False) == h
    assert unseen_turns(h, Engine.CLAUDE, has_session=True) == h
    block = history_block(h[2:])
    assert "나: 11월 일정" in block and "봇(calendar): 8건" in block


async def test_fall_through_question_gets_calendar_history(settings):  # noqa: F811
    """'이 중에서 우리의 일정이 아닌게 무엇이지?' 가 agy 로 가도 앞의 일정 목록을 볼 수 있어야 한다."""
    store = Store(":memory:")
    runner = FakeRunner()

    class NoWs:
        async def handle(self, key, text, history=None):
            return None

    agent = Agent(settings, store, TsClient(), USER, run=runner, workspace=NoWs())
    store.add_turn("D1:100.0", "user", "11월 일정 조사해줘 --deep 은 문자열일 뿐")
    store.add_turn("D1:100.0", "calendar", "- 종일 KB 신용카드 결제일 `기본`\n- 종일 월세 `우리의 일정`")
    await agent.process(Job("D1", "101.0", None, "이 중에서 우리의 일정이 아닌게 무엇이지?", "100.0", True))
    req = runner.calls[0]
    assert req.engine is Engine.AGY  # 이전 대화의 --deep 은 경로 판단에 쓰지 않는다
    assert "[이전 대화" in req.prompt and "봇(calendar): - 종일 KB 신용카드 결제일 `기본`" in req.prompt
    assert req.prompt.rstrip().endswith("이 중에서 우리의 일정이 아닌게 무엇이지?")


def test_primary_calendar_labeled_when_mixed():
    t = datetime(2026, 11, 13, tzinfo=KST)
    evs = [Event("월세", None, None, "2026-11-13", True, calendar="우리의 일정"),
           Event("KB 신용카드 결제일", None, None, "2026-11-14", True)]
    out = google.format_events(evs, t, t)
    assert "KB 신용카드 결제일 `기본`" in out and "월세 `우리의 일정`" in out
    only_primary = google.format_events(evs[1:], t, t)
    assert "`기본`" not in only_primary
