"""DM 으로 하는 로그인 (Google 캘린더·메일, agy). 가족이 서버 터미널 없이 휴대폰만으로 연결할 수 있게 한다.

Google: "구글 로그인" → 봇이 로그인 주소를 보낸다 → 가족이 로그인 후 주소창의 http://localhost:8765/?…code=… 를
        DM 으로 보낸다 → 서버가 PKCE 값으로 토큰을 받아 data/users/<이름>/google_token.json 에 저장.
agy:    "agy 로그인" → 서버가 가상 터미널(pty)에서 그 사람의 agy 프로필로 agy 를 띄워 로그인 주소를 받는다 →
        가족이 antigravity.google 페이지에 나온 인증 코드를 DM 으로 보내면 터미널에 입력해 마무리.

로그인 주소·코드가 담긴 메시지는 대화 기록·세션에 남기지 않는다(호출하는 쪽 책임). 대기 상태는 메모리에만 두고 10분 뒤 만료.
"""

from __future__ import annotations

import os
import pty
import re
import select
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

from agent.tools.google_auth import CLIENT_FILE, PORT, SCOPES

LOGIN_TTL = 600
GOOGLE_CMD_RE = re.compile(r"^\s*(구글|google)\s*(로그인|연결|login)\s*$", re.I)
AGY_CMD_RE = re.compile(r"^\s*(agy|gemini|제미나이)\s*(로그인|연결|login)\s*$", re.I)
REDIRECT_RE = re.compile(r"https?://localhost:\d+/\?\S*code=\S+")
URL_RE = re.compile(r"https://accounts\.google\.com/\S+")


def google_start(state_store: dict, user: str) -> str:
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_FILE), SCOPES)
    flow.redirect_uri = f"http://localhost:{PORT}/"
    url, state = flow.authorization_url(access_type="offline", prompt="consent")
    state_store[user] = {"state": state, "code_verifier": flow.code_verifier, "at": time.time()}
    return url


def google_finish(state_store: dict, user: str, redirect: str, token_file: Path) -> list[str]:
    """리디렉션 주소로 토큰을 받아 저장한다. 부여되지 않은 권한 목록을 돌려준다."""
    saved = state_store.pop(user, None)
    if not saved or time.time() - saved["at"] > LOGIN_TTL:
        raise ValueError("로그인 요청이 없거나 10분이 지났어요. '구글 로그인'부터 다시 해 주세요.")
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_FILE), SCOPES, state=saved["state"],
                                                     code_verifier=saved["code_verifier"])
    flow.redirect_uri = f"http://localhost:{PORT}/"
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")  # Google 이 권한 순서를 바꿔 돌려줘도 허용
    # oauthlib 은 http 리디렉션을 거부하므로 localhost 주소를 https 로 바꿔 코드만 읽게 한다
    flow.fetch_token(authorization_response=redirect.strip().replace("http://", "https://", 1))
    creds = flow.credentials
    token_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = token_file.with_suffix(".tmp")
    tmp.write_text(creds.to_json(), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(token_file)
    return [s.rsplit("/", 1)[-1] for s in SCOPES if s not in set(creds.scopes or [])]


@dataclass
class AgyLoginSession:
    """agy 를 가상 터미널에서 띄워 두고 인증 코드를 기다린다."""

    proc: subprocess.Popen
    fd: int  # 가상 터미널 master
    started: float = field(default_factory=time.time)
    output: bytes = b""

    def read(self, seconds: float, until: re.Pattern | None = None) -> str:
        """출력을 모은다. 프로세스가 끝나거나 until 이 보이면 바로 멈춘다."""
        end = time.time() + seconds
        while time.time() < end:
            r, _, _ = select.select([self.fd], [], [], 0.3)
            if r:
                try:
                    chunk = os.read(self.fd, 4096)
                except OSError:  # 자식이 끝나 터미널이 닫힘 (EIO)
                    break
                if not chunk:
                    break
                self.output += chunk
                if until and until.search(_strip_ansi(self.output)):
                    break
            elif self.proc.poll() is not None:
                break
        return _strip_ansi(self.output)

    def close(self) -> None:
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self.proc.wait()
        try:
            os.close(self.fd)
        except OSError:
            pass


def _strip_ansi(b: bytes) -> str:
    return re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\r", "", b.decode("utf-8", "replace"))


def agy_spawn(agy_home: Path, agy_binary: str) -> AgyLoginSession:
    """그 사람의 agy 프로필(HOME)로 agy 를 띄운다. print 모드라도 제어 터미널이 있으면 인증 코드를 기다린다.

    스레드에서 fork(pty.fork)하면 교착 위험이 있어, subprocess + `setsid --ctty`로 가상 터미널을 제어 터미널로 붙인다.
    """
    agy_home.mkdir(parents=True, exist_ok=True)
    os.chmod(agy_home, 0o700)
    master, slave = pty.openpty()
    proc = subprocess.Popen(
        ["setsid", "--ctty", agy_binary, "--print", "Reply with exactly: login ok"],
        stdin=slave, stdout=slave, stderr=slave, cwd=agy_home, close_fds=True,
        env={**os.environ, "HOME": str(agy_home)},
    )
    os.close(slave)
    return AgyLoginSession(proc, master)


def agy_start(sessions: dict, user: str, agy_home: Path, agy_binary: str) -> str:
    old = sessions.pop(user, None)
    if old:
        old.close()
    s = agy_spawn(agy_home, agy_binary)
    text = s.read(20, until=URL_RE)
    m = URL_RE.search(text)
    if not m:
        s.close()
        if "login ok" in text.lower():
            raise RuntimeError("이미 agy 로그인이 되어 있어요.")
        raise RuntimeError(f"agy 로그인 주소를 받지 못했어요: {text[-300:]}")
    sessions[user] = s
    return m.group(0)


def agy_finish(sessions: dict, user: str, code: str) -> str:
    s = sessions.pop(user, None)
    if s is None or time.time() - s.started > LOGIN_TTL:
        if s:
            s.close()
        raise ValueError("agy 로그인 요청이 없거나 10분이 지났어요. 'agy 로그인'부터 다시 해 주세요.")
    try:
        os.write(s.fd, code.strip().encode() + b"\n")
        text = s.read(60, until=re.compile(r"login ok", re.I))
    finally:
        s.close()
    return text


def looks_like_agy_code(text: str) -> bool:
    """agy 인증 코드처럼 보이는 한 줄 (공백 없는 긴 토큰)."""
    return bool(re.fullmatch(r"[A-Za-z0-9/_\-.~]{20,}", text.strip()))
