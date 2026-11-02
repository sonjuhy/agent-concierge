"""멀티 사용자(가족) — docs/MULTI_USER.md 의 격리·정책 검증."""

import sqlite3
from datetime import datetime, timedelta

import pytest

from agent import agy_profile, scheduler
from agent.actions import PendingStore, Workspace
from agent.agy_profile import NotLoggedIn
from agent.cli_runner import Engine
from agent.context import KST
from agent.sessions import Store
from agent.slack_app import FAMILY_TAINTED_MSG, Agent, Job
from agent.users import Users
from tests.test_actions import FakeApi, PlanRunner
from tests.test_slack_app import FakeClient, FakeRunner, settings  # noqa: F401

OWNER_ID, FAM_ID = "U_OWNER", "U_FAM"


@pytest.fixture
def users(settings, tmp_path):  # noqa: F811
    yaml = tmp_path / "users.yaml"
    yaml.write_text(
        f"shared: {tmp_path}/공유\n"
        f"users:\n"
        f"  - {{name: 민수, slack_id: {OWNER_ID}, role: owner}}\n"
        f"  - {{name: 엄마, slack_id: {FAM_ID}, role: family, vault: {tmp_path}/fam}}\n",
        encoding="utf-8",
    )
    return Users.load(settings, yaml, data_dir=tmp_path / "data")  # 실제 data/ 를 건드리지 않게


def test_users_load_and_validation(users, settings, tmp_path):  # noqa: F811
    owner, fam = users.owner, users.by_slack(FAM_ID)
    assert owner.name == "민수" and owner.vault == settings.vault and owner.is_owner
    assert fam.name == "엄마" and not fam.is_owner and (tmp_path / "fam").is_dir()
    assert fam.agy_home.name == "agy-home" and fam.google_token.name == "google_token.json"
    assert users.shared == tmp_path / "공유"
    s = fam.settings(settings, users.shared)
    assert s.vault == tmp_path / "fam" and s.agy_own_token and s.shared == users.shared
    assert not owner.settings(settings).agy_own_token

    bad = tmp_path / "bad.yaml"
    bad.write_text("users:\n  - {name: a, slack_id: U1, role: family}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        Users.load(settings, bad)  # owner 없음
    # users.yaml 이 없으면 기존 1인 구성
    single = Users.load(settings, tmp_path / "none.yaml")
    assert len(single.users) == 1 and single.owner.vault == settings.vault


def test_family_agy_profile_never_copies_owner_token(tmp_path, monkeypatch):
    vault, home, shared = tmp_path / "v", tmp_path / "h", tmp_path / "s"
    vault.mkdir()
    token = tmp_path / "owner-token"
    token.write_text("owner")
    monkeypatch.setattr(agy_profile, "GLOBAL_TOKEN", token)
    with pytest.raises(NotLoggedIn):
        agy_profile.prepare(home, vault, "agy", use_bwrap=False, own_token=True, shared=shared)
    assert not (home / ".gemini/antigravity-cli/antigravity-oauth-token").exists()
    # 본인이 로그인해 토큰이 생기면 동작하고, 공용 폴더가 쓰기 허용·bwrap 바인드에 들어간다
    (home / ".gemini/antigravity-cli").mkdir(parents=True, exist_ok=True)
    (home / ".gemini/antigravity-cli/antigravity-oauth-token").write_text("family")
    wrapper, _ = agy_profile.prepare(home, vault, "/bin/true", use_bwrap=True, own_token=True, shared=shared)
    assert ("--bind", str(shared), str(shared)) in list(zip(wrapper, wrapper[1:], wrapper[2:]))
    allow = agy_profile.render_settings(home, vault, shared)["permissions"]["allow"]
    assert f"write_file({shared})" in allow


def agent_for(settings, users, run=None, workspaces=None):  # noqa: F811
    return Agent(settings, Store(":memory:"), FakeClient(), users, run=run or FakeRunner(), workspace=workspaces)


async def test_routing_by_user_and_family_policies(settings, users, monkeypatch):  # noqa: F811
    a = agent_for(settings, users)
    await a.on_event({"user": "U_STRANGER", "channel": "D9", "ts": "1", "text": "hi", "channel_type": "im"})
    assert a.queues == {}  # 모르는 사람은 무시

    await a.on_event({"user": FAM_ID, "channel": "D2", "ts": "2", "text": "분석해줘 --deep", "channel_type": "im"})
    job = a.queue_for("엄마", Engine.AGY).get_nowait()  # 가족은 --deep 이어도 agy 대기열
    assert job.user == "엄마"

    # 가족 요청은 자기 폴더·자기 agy 프로필로, --deep 없이 agy 로 실행된다
    seen = {}

    def fake_build(s, text, **kw):
        seen["settings"], seen["text"] = s, text
        raise NotLoggedIn("no token")

    monkeypatch.setattr("agent.slack_app.build_request", fake_build)
    await a.process(job)
    assert seen["settings"].vault == users.by_name("엄마").vault and seen["settings"].agy_own_token
    assert "--deep" not in seen["text"]
    assert "로그인" in a.client.posts[-1]["text"]  # agy 미로그인 안내


async def test_family_tainted_conversation_is_refused(settings, users):  # noqa: F811
    run = FakeRunner()
    a = agent_for(settings, users, run=run)
    a.store.mark_tainted("D2:5.0", "email content")
    await a.process(Job("D2", "6.0", None, "그 메일 요약해줘", "5.0", True, "엄마"))
    assert run.calls == [] and a.client.posts[-1]["text"] == FAMILY_TAINTED_MSG


async def test_family_workspace_uses_agy_planner_and_mail_list(settings, users):  # noqa: F811
    fam = users.by_name("엄마")
    runner = PlanRunner({"intent": "email_list", "email_query": "is:unread"})
    api = FakeApi()
    ws = Workspace(fam.settings(settings, users.shared), PendingStore(sqlite3.connect(":memory:")), runner,
                   api=api, owner="엄마", planner="agy", mail_mode="list")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("agent.actions.build_request", lambda s, text, **kw: (None, _agy_req(text)))
        r = await ws.handle("t", "안 읽은 메일 있어?")
    plan_req = runner.calls[0]
    assert plan_req.engine is Engine.AGY and plan_req.model == "gemini-3.8-flash-low"
    assert "--json-schema" in plan_req.extra_args
    assert len(runner.calls) == 1  # 메일은 LLM 에 넣지 않는다
    assert r.tainted and "boss@x.com — 보고서 요청" in r.text and "IGNORE" not in r.text


def _agy_req(text):
    from agent.cli_runner import CLIRequest
    return CLIRequest(Engine.AGY, text)


async def test_pending_approval_only_by_requester(settings, users):  # noqa: F811
    store = Store(":memory:")
    pending = PendingStore(store.db)
    fam_ws = Workspace(settings, pending, PlanRunner({"intent": "calendar_create", "event_summary": "병원",
                                                      "event_start": "2026-11-02T10:00:00+09:00"}),
                       api=FakeApi(), owner="엄마")
    r = await fam_ws.handle("D2:1", "월요일 10시 병원 일정 추가")
    aid = r.blocks[1]["elements"][0]["value"]
    owner_ws = Workspace(settings, pending, PlanRunner({}), api=FakeApi(), owner="민수")
    assert await owner_ws.approve(aid, owner="민수") is None  # 다른 사람은 승인 불가
    assert fam_ws.api.created == []
    _, msg = await fam_ws.approve(aid, owner="엄마")
    assert "병원" in msg and len(fam_ws.api.created) == 1


class DMClient:
    def __init__(self):
        self.posts, self.opened = [], []

    async def conversations_open(self, users):
        self.opened.append(users)
        return {"channel": {"id": f"DM-{users}"}}

    async def chat_postMessage(self, **kw):
        self.posts.append(kw)
        return {"ts": f"{len(self.posts)}.0"}


@pytest.fixture
async def sched_env(settings, users, monkeypatch):  # noqa: F811
    client = DMClient()
    sched = scheduler.create(settings, client, Store(":memory:"), users)
    sched.start(paused=True)
    scheduler.RUNTIME.apis = {"민수": FakeApi(), "엄마": FakeApi()}
    yield sched, client
    sched.shutdown(wait=False)


async def test_scheduler_jobs_are_per_user(sched_env):
    sched, client = sched_env
    when = datetime.now(KST) + timedelta(hours=1)
    mine = scheduler.add_reminder(sched, when, "약", user="민수")
    hers = scheduler.add_reminder(sched, when, "장보기", user="엄마")
    assert "장보기" not in scheduler.list_jobs(sched, "민수") and "장보기" in scheduler.list_jobs(sched, "엄마")
    assert not scheduler.remove(sched, hers, "민수")  # 남의 예약은 못 지운다
    assert scheduler.remove(sched, mine, "민수")
    await scheduler.job_reminder("장보기", user="엄마")
    assert client.opened[-1] == FAM_ID and client.posts[-1]["channel"] == f"DM-{FAM_ID}"


async def test_family_briefing_has_no_health_and_no_llm(sched_env, monkeypatch):
    sched, client = sched_env

    async def no_health(s, users=None):
        raise AssertionError("가족 브리핑에서 헬스체크를 돌리면 안 된다")

    async def no_llm(req, binary):
        raise AssertionError("가족 브리핑에서 메일을 LLM 에 넣으면 안 된다")

    monkeypatch.setattr(scheduler.health, "run_all", no_health)
    monkeypatch.setattr(scheduler, "run_cli", no_llm)
    scheduler.ensure_briefing(sched, "08:00", user="엄마")
    job = sched.get_job("daily_briefing:엄마")
    assert job.kwargs == {"user": "엄마"}
    await scheduler.job_briefing(user="엄마")
    text = client.posts[-1]["text"]
    assert "팀 회의" in text and "boss@x.com — 보고서 요청" in text and "시스템" not in text


async def test_legacy_jobs_belong_to_owner(sched_env):
    sched, _ = sched_env
    # 멀티 사용자 이전에 만든 잡(kwargs 없음)은 소유자 것
    sched.add_job(scheduler.job_reminder, "date", run_date=datetime.now(KST) + timedelta(hours=1),
                  args=["옛 알림"], id="reminder:old")
    assert "reminder:old" in scheduler.list_jobs(sched, "민수")
    assert "reminder:old" not in scheduler.list_jobs(sched, "엄마")


def test_family_mail_watch_id_is_namespaced(sched_env):
    sched, _ = sched_env
    assert scheduler.add_mail_watch(sched, "from:x", 10, "x", user="엄마") == "mailwatch:엄마:from:x"
    assert scheduler.add_mail_watch(sched, "from:x", 10, "x", user="민수") == "mailwatch:from:x"

