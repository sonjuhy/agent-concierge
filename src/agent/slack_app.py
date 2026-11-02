"""Slack 메시지 → 라우터 → agy/claude → 같은 스레드에 답변.

- SLACK_ALLOWED_USER_ID 의 DM·멘션만 처리하고 나머지는 무시한다.
- 엔진별 큐(동시 실행 1개)로 직렬화한다. 긴 claude 작업이 agy 요청을 막지 않는다.
- 진행 표시: ⏳ 리액션 → 끝나면 ✅ / ❌ 로 바꾼다.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from agent import login
from agent.actions import Reply, Workspace
from agent.agy_profile import NotLoggedIn
from agent.chatlog import ChatLog
from agent.cli_runner import CLIRequest, CLIResult, Engine, run_cli
from agent.config import Settings
from agent.context import build_context
from agent.router import build_request, decide, strip_flags
from agent.sessions import Store
from agent.users import OWNER_GOOGLE_TOKEN, User, Users

log = logging.getLogger(__name__)

MENTION_RE = re.compile(r"<@[A-Z0-9]+>\s*")
# Slack markdown 블록 한도(12,000자)보다 여유 있게 자른다
CHUNK = 11_000

Runner = Callable[[CLIRequest, str], Awaitable[CLIResult]]


# DM 에서 스레드 밖에 보낸 메시지는 마지막 활동 후 이 시간 안이면 같은 대화로 잇는다
DM_CONTINUE_SECONDS = 30 * 60
TURN_CHARS = 1500


@dataclass(frozen=True)
class Job:
    channel: str
    ts: str  # 사용자 메시지 ts (리액션 대상)
    thread_ts: str | None  # 답변 위치: 스레드 ts, None 이면 DM 본문(스레드 없이)
    text: str
    conv_ts: str | None = None  # 대화 식별자 (세션·기록·오염 표시의 단위)
    dm: bool = False
    user: str | None = None  # 요청한 사용자 이름 (None = 소유자)

    @property
    def thread_key(self) -> str:
        return f"{self.channel}:{self.conv_ts or self.thread_ts or self.ts}"


def unseen_turns(history: list[tuple[str, str]], engine: Engine, has_session: bool) -> list[tuple[str, str]]:
    """엔진 세션이 모르는 최근 대화. 세션이 있으면 그 엔진의 마지막 답 이후만, 없으면 전부."""
    if not has_session:
        return history
    last = max((i for i, (who, _) in enumerate(history) if who.startswith(engine.value)), default=-1)
    return history[last + 1:]


def history_block(turns: list[tuple[str, str]]) -> str:
    if not turns:
        return ""
    lines = []
    for who, text in turns:
        speaker = "나" if who == "user" else f"봇({who.split(' ·')[0]})"  # "agy · 3.0s" → 봇(agy)
        lines.append(f"{speaker}: {text[:TURN_CHARS]}")
    return "[이전 대화 — 아래 요청은 이 대화에 이어지는 것이다]\n" + "\n".join(lines)


SLACK_LINK_RE = re.compile(r"<(?:mailto:)?([^<>|]+)(?:\|([^<>]+))?>")


def slack_to_plain(text: str) -> str:
    """Slack 이 자동으로 바꾼 링크 표기(<mailto:a@b.com|a@b.com>, <https://x|x>)와 HTML 이스케이프를 되돌린다."""
    text = SLACK_LINK_RE.sub(lambda m: m.group(2) or m.group(1), text)
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").strip()


def chunk_text(text: str, size: int = CHUNK) -> list[str]:
    """줄 단위로 끊어 size 이하 조각으로 나눈다."""
    chunks, cur = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > size:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:size])
            line = line[size:]
        if len(cur) + len(line) > size:
            chunks.append(cur)
            cur = ""
        cur += line
    if cur.strip():
        chunks.append(cur)
    return chunks or [""]


def footer(r: CLIResult) -> str:
    parts = [r.engine.value, f"{r.duration:.1f}s"]
    if r.cost_usd is not None:
        parts.append(f"${r.cost_usd:.4f}")
    return " · ".join(parts)


FAMILY_TAINTED_MSG = (
    "이 대화에는 메일 같은 외부 내용이 들어 있어서 이어서 답할 수 없어요. 새 대화로 다시 물어봐 주세요."
)


def _per_user(value, owner: str) -> dict:
    """하나만 주면 소유자 것으로, dict 면 사용자 이름별로."""
    if value is None:
        return {}
    return value if isinstance(value, dict) else {owner: value}


class Agent:
    """users: 사용자 목록 (Users). 하위 호환으로 소유자 Slack ID 문자열만 줘도 된다.
    chatlog / workspace: 소유자 하나만 주거나 {사용자 이름: 객체} dict 로 준다."""

    def __init__(
        self,
        settings: Settings,
        store: Store,
        client: Any,
        users: Users | str,
        run: Runner | None = None,
        chatlog: ChatLog | dict[str, ChatLog] | None = None,
        workspace: Workspace | dict[str, Workspace] | None = None,
    ):
        self.settings = settings
        self.store = store
        self.client = client
        if isinstance(users, str):
            users = Users((User("owner", users, "owner", settings.vault, settings.agy_home, OWNER_GOOGLE_TOKEN),))
        self.users = users
        self.run = run or (lambda req, binary: run_cli(req, binary=binary))
        self.chatlogs = _per_user(chatlog, users.owner.name)
        self.workspaces = _per_user(workspace, users.owner.name)
        # 사용자·엔진별 대기열: 가족은 계정이 달라 서로 기다릴 필요가 없다
        self.queues: dict[tuple[str, Engine], asyncio.Queue[Job]] = {}
        self.workers: dict[tuple[str, Engine], asyncio.Task] = {}
        self.running = False
        self.google_logins: dict[str, dict] = {}  # 사용자 이름 → 대기 중인 PKCE 값
        self.agy_logins: dict[str, login.AgyLoginSession] = {}

    def user_of(self, job: Job) -> User:
        return self.users.by_name(job.user) or self.users.owner

    def queue_for(self, user: str, engine: Engine) -> asyncio.Queue[Job]:
        key = (user, engine)
        if key not in self.queues:
            self.queues[key] = asyncio.Queue()
            if self.running:
                self.workers[key] = asyncio.create_task(self._worker(key), name=f"worker-{user}-{engine}")
        return self.queues[key]

    def start(self) -> None:
        self.running = True
        for key in self.queues:
            self.workers.setdefault(key, asyncio.create_task(self._worker(key), name=f"worker-{key}"))

    async def stop(self) -> None:
        self.running = False
        for w in self.workers.values():
            w.cancel()
        await asyncio.gather(*self.workers.values(), return_exceptions=True)

    # ---- Slack 이벤트 진입점 ----
    async def on_event(self, event: dict) -> None:
        user = self.users.by_slack(event.get("user"))
        if user is None or event.get("bot_id") or event.get("subtype"):
            return  # 등록되지 않은 사람·봇 메시지·수정 이벤트는 무시
        text = slack_to_plain(MENTION_RE.sub("", event.get("text") or ""))
        if not text:
            return
        if await self._handle_login(event, user, text):
            return  # 로그인 주소·코드는 대화 기록·세션에 남기지 않는다
        job = self._job_for(event, text, user.name)
        engine = decide(text, has_external=False, thread_tainted=self.store.is_tainted(job.thread_key)).engine
        if not user.is_owner:
            engine = Engine.AGY  # 가족은 agy 만 쓴다
        q = self.queue_for(user.name, engine)
        await self._react(job, "hourglass_flowing_sand")
        if q.qsize():
            await self._reply(job, f"앞선 작업 {q.qsize()}개가 끝나면 처리합니다.")
        await q.put(job)

    # ---- DM 로그인 (agent.login) ----
    async def _handle_login(self, event: dict, user: User, text: str) -> bool:
        is_login = (login.GOOGLE_CMD_RE.match(text) or login.AGY_CMD_RE.match(text)
                    or (user.name in self.google_logins and login.REDIRECT_RE.search(text))
                    or (user.name in self.agy_logins and login.looks_like_agy_code(text)))
        if not is_login:
            return False
        channel = event["channel"]

        async def say(msg: str) -> None:
            await self.client.chat_postMessage(channel=channel, text=msg)

        if event.get("channel_type") != "im":
            await say("로그인은 봇과의 DM 에서만 할 수 있어요.")
            return True
        try:
            if login.GOOGLE_CMD_RE.match(text):
                url = login.google_start(self.google_logins, user.name)
                await say(f"아래 주소에서 Google 로그인을 해 주세요 (10분 안에).\n{url}\n\n"
                          "'확인되지 않은 앱' 화면이 나오면 *고급 → 이동*, 권한에 동의하면 마지막에 "
                          "'사이트에 연결할 수 없음'이 떠요. 그때 *주소창의 주소 전체*를 여기에 보내 주세요.")
            elif user.name in self.google_logins and (m := login.REDIRECT_RE.search(text)):
                missing = await asyncio.to_thread(login.google_finish, self.google_logins, user.name, m.group(0),
                                                  user.google_token)
                await say("✅ Google 연결 완료. " + ("모든 권한이 연결됐어요." if not missing
                                                    else "일부 권한이 빠졌어요: " + ", ".join(missing)))
            elif login.AGY_CMD_RE.match(text):
                if user.is_owner:
                    await say("관리자 계정은 서버의 agy 로그인을 그대로 써요. 따로 로그인할 필요가 없어요.")
                    return True
                url = await asyncio.to_thread(login.agy_start, self.agy_logins, user.name, user.agy_home,
                                              self.settings.agy_bin)
                await say(f"아래 주소에서 Gemini(Google AI Pro) 계정으로 로그인해 주세요 (10분 안에).\n{url}\n\n"
                          "로그인하면 *인증 코드*가 보여요. 그 코드만 여기에 보내 주세요.")
            else:
                out = await asyncio.to_thread(login.agy_finish, self.agy_logins, user.name, text)
                ok = "login ok" in out.lower()
                await say("✅ agy 로그인 완료. 이제 대화할 수 있어요." if ok
                          else f"❌ agy 로그인에 실패했어요. 'agy 로그인'부터 다시 해 주세요.\n```{out[-300:]}```")
        except Exception as e:  # 만료·잘못된 주소 등
            log.warning("login failed for %s: %s", user.name, e)
            await say(f"❌ 로그인 처리 실패: {e}")
        return True

    def _job_for(self, event: dict, text: str, user: str | None = None) -> Job:
        """메시지가 어느 대화에 속하고 어디에 답할지 정한다.

        - 스레드 안 답글: 그 스레드(또는 그 스레드 부모가 속한 대화)를 잇고, 스레드에 답한다
        - DM 본문: 마지막 활동 후 30분 안이면 같은 대화를 잇고, 아니면 새 대화. 답도 DM 본문에 한다
        - 채널 멘션: 그 메시지를 새 스레드로 삼는다 (기존 동작)
        """
        channel, ts = event["channel"], event["ts"]
        dm = event.get("channel_type") == "im"
        if event.get("thread_ts"):
            parent = event["thread_ts"]
            conv = self.store.conv_of(channel, parent) or parent
            job = Job(channel, ts, parent, text, conv, dm, user)
        elif dm:
            conv = self.store.active_dm_conv(channel, DM_CONTINUE_SECONDS) or ts
            job = Job(channel, ts, None, text, conv, dm, user)
        else:
            job = Job(channel, ts, ts, text, ts, dm, user)
        self.store.touch_conv(channel, ts, job.conv_ts, dm)
        return job

    # ---- 처리 ----
    async def _worker(self, key: tuple[str, Engine]) -> None:
        q = self.queues[key]
        while True:
            job = await q.get()
            try:
                await self.process(job)
            except Exception:
                log.exception("job failed: %s", job.thread_key)
                await self._finish(job, ok=False, text="내부 오류가 발생했습니다. 로그를 확인해 주세요.")
            finally:
                q.task_done()

    async def process(self, job: Job) -> CLIResult | None:
        user = self.user_of(job)
        settings = user.settings(self.settings, self.users.shared)
        tainted = self.store.is_tainted(job.thread_key)
        if tainted and not user.is_owner:
            # 오염된 대화는 도구 없는 claude 로만 처리하는데, 가족에게는 claude 가 없다 → 처리하지 않는다
            self._log(job.thread_key, job.ts, "나", job.text, tainted=True, user=user.name)
            await self._finish(job, ok=False, text=FAMILY_TAINTED_MSG)
            return None
        # 이어지는 질문("그 날 결혼식 일정 있지 않았어?", "이 중에서 …")을 풀도록 최근 대화를 함께 넘긴다.
        # 오염된 대화의 기록에는 외부 텍스트가 섞여 있어 넘기지 않는다.
        history = [] if tainted else self.store.recent_turns(job.thread_key)
        workspace = self.workspaces.get(user.name)
        if workspace is not None:
            reply = await workspace.handle(job.thread_key, job.text, history)
            if reply is not None:
                await self._workspace_reply(job, reply, tainted or reply.tainted)
                return None
        text = job.text if user.is_owner else strip_flags(job.text)  # 가족에게는 --deep(claude)이 없다
        route = decide(text, has_external=False, thread_tainted=tainted)
        resume = self.store.get_session(job.thread_key, route.engine) if route.trusted else None
        # 일정·메일 답처럼 엔진 세션 밖에서 오간 대화는 세션이 모르므로 프롬프트에 붙인다
        preamble = history_block(unseen_turns(history, route.engine, bool(resume))) if route.trusted else ""
        try:
            _, req = build_request(
                settings,
                f"{build_context()}\n\n{text}",
                thread_tainted=tainted,
                resume=resume,
                preamble=preamble,
            )
        except NotLoggedIn:
            self._log(job.thread_key, job.ts, "나", job.text, tainted=False, user=user.name)
            await self._finish(job, ok=False, text="agy(Gemini) 로그인이 아직 안 돼 있어요. 관리자에게 로그인을 요청해 주세요.")
            return None
        binary = self.settings.agy_bin if req.engine is Engine.AGY else self.settings.claude_bin
        self._log(job.thread_key, job.ts, "나", job.text, tainted=not req.trusted, user=user.name)
        log.info("run %s trusted=%s resume=%s thread=%s", req.engine, req.trusted, bool(resume), job.thread_key)
        r = await self.run(req, binary)
        self.store.log_call(job.thread_key, req.trusted, r)

        if r.ok and r.session_id and req.trusted:
            self.store.set_session(job.thread_key, req.engine, r.session_id)
        if not req.trusted:
            self.store.mark_tainted(job.thread_key, route.reason)

        if r.ok:
            ok, text, meta = True, r.text or "(빈 응답)", footer(r)
        elif r.timed_out:
            ok, text, meta = False, f"⏱ 시간 초과로 중단했습니다 ({req.timeout:.0f}초).", None
        else:
            ok, text, meta = False, f"실행 실패: {(r.error or '').strip()[:500]}", None
        self._log(job.thread_key, str(time.time()), footer(r), text, tainted=not req.trusted, user=user.name)
        await self._finish(job, ok=ok, text=text, meta=meta)
        return r

    async def _workspace_reply(self, job: Job, reply: Reply, tainted: bool) -> None:
        """일정·메일 처리 결과 전송. 메일 내용이 섞인 답이면 스레드를 오염 표시한다."""
        self._log(job.thread_key, job.ts, "나", job.text, tainted=tainted, user=job.user)
        if reply.tainted:
            self.store.mark_tainted(job.thread_key, "email content")
        log.info("workspace reply user=%s ok=%s tainted=%s thread=%s",
                 job.user, reply.ok, reply.tainted, job.thread_key)
        self._log(job.thread_key, str(time.time()), reply.meta or "workspace", reply.text, tainted=tainted,
                  user=job.user)
        if reply.blocks:
            await self._post(job, text=reply.text, blocks=reply.blocks)
            await self._unreact(job, "hourglass_flowing_sand")
            await self._react(job, "raised_hand")  # 승인 대기
        else:
            await self._finish(job, ok=reply.ok, text=reply.text, meta=reply.meta)

    # ---- 승인 버튼 ----
    async def on_action(self, body: dict, approve: bool) -> None:
        user = self.users.by_slack(body.get("user", {}).get("id"))
        workspace = self.workspaces.get(user.name) if user else None
        if workspace is None:
            return
        aid = body["actions"][0]["value"]
        channel, msg_ts = body["channel"]["id"], body["message"]["ts"]
        # 승인·취소는 요청한 본인만 (PendingStore 가 소유자를 확인한다)
        if approve:
            done = await workspace.approve(aid, owner=user.name)
            thread_key, result = done if done else (None, "이미 처리됐거나 만료됐거나, 본인 요청이 아닙니다.")
        else:
            thread_key = workspace.cancel(aid, owner=user.name)
            result = "취소했습니다." if thread_key else "이미 처리됐거나 만료됐거나, 본인 요청이 아닙니다."
        # 버튼을 결과 문구로 바꿔 다시 누를 수 없게 한다
        original = (body["message"].get("blocks") or [])[:1]
        await self.client.chat_update(
            channel=channel, ts=msg_ts, text=result,
            blocks=original + [{"type": "context", "elements": [{"type": "mrkdwn", "text": result}]}],
        )
        if thread_key:
            self._log(thread_key, str(time.time()), "승인" if approve else "취소", result,
                      tainted=self.store.is_tainted(thread_key), user=user.name)

    async def on_todo_done(self, body: dict) -> None:
        """할 일 알림의 '완료' 버튼. 본인 할 일만 (TodoStore 가 owner 로 거른다)."""
        user = self.users.by_slack(body.get("user", {}).get("id"))
        workspace = self.workspaces.get(user.name) if user else None
        if workspace is None:
            return
        value = str(body["actions"][0].get("value") or "")
        t = workspace.complete(int(value)) if value.isdigit() else None
        result = f"✅ 완료: `#{t.id}` {t.title}" if t else "이미 지웠거나 본인 할 일이 아닙니다."
        original = (body["message"].get("blocks") or [])[:1]
        await self.client.chat_update(
            channel=body["channel"]["id"], ts=body["message"]["ts"], text=result,
            blocks=original + [{"type": "context", "elements": [{"type": "mrkdwn", "text": result}]}],
        )

    def _log(self, thread_key: str, ts: str, who: str, text: str, tainted: bool, user: str | None = None) -> None:
        # who: "나" 또는 답한 쪽 (calendar / gmail · … / scheduler / agy · … 등)
        self.store.add_turn(thread_key, "user" if who == "나" else who, text)
        chatlog = self.chatlogs.get(user or self.users.owner.name)  # 대화 기록은 각자 폴더에
        if chatlog is None:
            return
        try:
            chatlog.append(thread_key, ts, who, text, tainted=tainted)
        except Exception:  # 기록 실패가 답변을 막지 않게
            log.exception("chat log append failed: %s", thread_key)

    # ---- Slack 출력 ----
    async def _finish(self, job: Job, ok: bool, text: str, meta: str | None = None) -> None:
        chunks = chunk_text(text)
        for i, c in enumerate(chunks):
            blocks: list[dict] = [{"type": "markdown", "text": c}]
            if meta and i == len(chunks) - 1:
                blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": meta}]})
            await self._post(job, text=c[:3000], blocks=blocks)
        await self._unreact(job, "hourglass_flowing_sand")
        await self._react(job, "white_check_mark" if ok else "x")

    async def _reply(self, job: Job, text: str) -> None:
        await self._post(job, text=text)

    async def _post(self, job: Job, **kw) -> None:
        """답을 보내고, 그 메시지를 대화에 등록한다 (봇 답에 스레드로 이어 답해도 같은 대화가 되도록)."""
        if job.thread_ts:
            kw["thread_ts"] = job.thread_ts
        resp = await self.client.chat_postMessage(channel=job.channel, **kw)
        ts = resp.get("ts") if resp is not None else None
        self.store.touch_conv(job.channel, ts, job.conv_ts or job.thread_ts or job.ts, job.dm)

    async def _react(self, job: Job, name: str) -> None:
        try:
            await self.client.reactions_add(channel=job.channel, timestamp=job.ts, name=name)
        except Exception as e:  # already_reacted 등은 무시
            log.debug("reactions_add %s: %s", name, e)

    async def _unreact(self, job: Job, name: str) -> None:
        try:
            await self.client.reactions_remove(channel=job.channel, timestamp=job.ts, name=name)
        except Exception as e:
            log.debug("reactions_remove %s: %s", name, e)
