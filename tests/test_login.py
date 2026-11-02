"""DM 로그인 (agent.login) — 가짜 agy 로 가상 터미널 흐름까지 검증."""

import os
import stat
import time
from dataclasses import replace

import pytest

from agent import health, login
from agent.cli_runner import Engine
from agent.sessions import Store
from agent.slack_app import Agent
from agent.users import Users
from tests.test_multi_user import FAM_ID, OWNER_ID, users  # noqa: F401
from tests.test_slack_app import FakeClient, FakeRunner, settings  # noqa: F401

FAKE_AGY = """#!/usr/bin/env python3
import sys
print("Authentication required. Please visit the URL to log in:")
print("  https://accounts.google.com/o/oauth2/auth?client_id=fake&state=xyz")
print("Enter the authorization code: ", end="", flush=True)
code = input().strip()
print("login ok" if code == "4/GOOD-CODE-1234567890abcdef" else "invalid code")
"""


@pytest.fixture
def fake_agy(tmp_path):
    p = tmp_path / "fake_agy"
    p.write_text(FAKE_AGY)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


def test_agy_login_through_pty(tmp_path, fake_agy):
    sessions = {}
    url = login.agy_start(sessions, "엄마", tmp_path / "home", fake_agy)
    assert url.startswith("https://accounts.google.com/") and "엄마" in sessions
    assert oct((tmp_path / "home").stat().st_mode & 0o777) == "0o700"
    out = login.agy_finish(sessions, "엄마", "4/GOOD-CODE-1234567890abcdef")
    assert "login ok" in out and "엄마" not in sessions

    login.agy_start(sessions, "엄마", tmp_path / "home", fake_agy)
    assert "invalid code" in login.agy_finish(sessions, "엄마", "4/BAD-CODE-000000000000000000")
    with pytest.raises(ValueError):
        login.agy_finish(sessions, "엄마", "4/GOOD-CODE-1234567890abcdef")  # 대기 중인 로그인 없음


def test_google_finish_requires_fresh_request(tmp_path):
    with pytest.raises(ValueError):
        login.google_finish({}, "엄마", "http://localhost:8765/?state=a&code=b", tmp_path / "t.json")
    old = {"엄마": {"state": "s", "code_verifier": "v", "at": time.time() - 3600}}
    with pytest.raises(ValueError):
        login.google_finish(old, "엄마", "http://localhost:8765/?state=s&code=c", tmp_path / "t.json")


def test_login_message_patterns():
    assert login.GOOGLE_CMD_RE.match("구글 로그인") and login.GOOGLE_CMD_RE.match("Google 연결")
    assert login.AGY_CMD_RE.match("agy 로그인") and login.AGY_CMD_RE.match("제미나이 연결")
    assert not login.GOOGLE_CMD_RE.match("구글 로그인 방법 알려줘")
    assert login.REDIRECT_RE.search("http://localhost:8765/?state=x&iss=y&code=4/abc&scope=z")
    assert login.looks_like_agy_code("4/0AUn6Hd9zZ0-KiEaI0TMJpFQYPPnjrAK1sh9")
    assert not login.looks_like_agy_code("오늘 일정 알려줘")


async def test_dm_login_flow_is_not_logged(settings, users, monkeypatch, tmp_path, fake_agy):  # noqa: F811
    store = Store(":memory:")
    client = FakeClient()
    a = Agent(replace(settings, agy_bin=fake_agy), store, client, users, run=FakeRunner())

    calls = {}

    def fake_start(state, user):
        state[user] = {"at": time.time()}
        return "https://accounts.google.com/o/oauth2/auth?fake"

    def fake_finish(state, user, redirect, token_file):
        calls["finish"] = (user, redirect, token_file)
        state.pop(user)
        return []

    monkeypatch.setattr(login, "google_start", fake_start)
    monkeypatch.setattr(login, "google_finish", fake_finish)
    dm = {"user": FAM_ID, "channel": "D2", "channel_type": "im"}
    await a.on_event({**dm, "ts": "1", "text": "구글 로그인"})
    assert "accounts.google.com" in client.posts[-1]["text"]
    redirect = "<http://localhost:8765/?state=s&amp;code=4/abc&amp;scope=x>"
    await a.on_event({**dm, "ts": "2", "text": redirect})
    user, url, token = calls["finish"]
    assert user == "엄마" and url == "http://localhost:8765/?state=s&code=4/abc&scope=x"
    assert token == users.by_name("엄마").google_token
    assert "✅ Google 연결 완료" in client.posts[-1]["text"]

    await a.on_event({**dm, "ts": "3", "text": "agy 로그인"})
    assert "인증 코드" in client.posts[-1]["text"]
    await a.on_event({**dm, "ts": "4", "text": "4/GOOD-CODE-1234567890abcdef"})
    assert "✅ agy 로그인 완료" in client.posts[-1]["text"]

    # 로그인 메시지는 대기열·대화 기록·세션 어디에도 남지 않는다
    assert a.queues == {} and store.db.execute("select count(*) from turns").fetchone()[0] == 0

    # 채널에서는 거절, 로그인 대기 중이 아니면 코드처럼 보이는 글도 일반 메시지로 처리
    await a.on_event({"user": FAM_ID, "channel": "C1", "ts": "5", "text": "구글 로그인"})
    assert "DM 에서만" in client.posts[-1]["text"]
    await a.on_event({**dm, "ts": "6", "text": "4/GOOD-CODE-1234567890abcdef"})
    assert not a.queue_for("엄마", Engine.AGY).empty()


async def test_owner_agy_login_is_not_needed(settings, users):  # noqa: F811
    client = FakeClient()
    a = Agent(settings, Store(":memory:"), client, users, run=FakeRunner())
    await a.on_event({"user": OWNER_ID, "channel": "D1", "channel_type": "im", "ts": "1", "text": "agy 로그인"})
    assert "따로 로그인할 필요가 없어요" in client.posts[-1]["text"]


def test_health_reports_family_login_state(users):  # noqa: F811
    fam = users.by_name("엄마")
    (c,) = health.check_family(users)
    assert not c.ok and "agy 로그인 안 됨" in c.detail and "Google 연결 안 됨" in c.detail
    token = fam.agy_home / ".gemini/antigravity-cli/antigravity-oauth-token"
    token.parent.mkdir(parents=True)
    token.write_text("t")
    fam.google_token.parent.mkdir(parents=True, exist_ok=True)
    fam.google_token.write_text('{"scopes": []}')
    (c,) = health.check_family(users)
    assert not c.ok and "권한 부족" in c.detail and "agy" not in c.detail
    os.remove(fam.google_token)


def test_users_without_yaml_has_no_family(settings, tmp_path):  # noqa: F811
    assert health.check_family(Users.load(settings, tmp_path / "none.yaml")) == []
