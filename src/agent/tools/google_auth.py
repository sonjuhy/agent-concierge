"""Google OAuth (데스크톱 앱 클라이언트) 로그인과 자격 증명 로딩.

    uv run python -m agent.tools.google_auth      # 최초 1회 로그인 (브라우저)

서버에는 화면이 없으므로 로그인 URL 을 출력하고 localhost:8765 에서 리디렉션을 기다린다.
다른 PC 의 브라우저로 로그인하면, 그 PC 에서 먼저 SSH 포트 포워딩을 열어 둔다:
    ssh -L 8765:localhost:8765 <user>@<server>
토큰은 data/google_token.json (chmod 600) 에 저장되고 이후 자동 갱신된다.
동의 화면이 '테스트' 상태면 갱신 토큰이 7일 뒤 만료되므로 '프로덕션'으로 전환해 둔다.
"""

from __future__ import annotations

import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

ROOT = Path(__file__).resolve().parents[3]
CLIENT_FILE = ROOT / "data" / "google_client.json"
TOKEN_FILE = ROOT / "data" / "google_token.json"
PORT = 8765

# 필요한 최소 권한: 캘린더 목록 보기(기본 외 캘린더 조회), 일정 읽기·쓰기, 메일 읽기, 메일 초안 작성(발송은 Slack 승인 후에만)
SCOPES = [
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
]


class NotAuthorized(Exception):
    pass


def missing_scopes(token_file: Path = TOKEN_FILE) -> list[str]:
    """저장된 토큰에 없는 권한 (권한을 추가한 뒤 재로그인 전이면 비어 있지 않다)."""
    import json

    if not token_file.exists():
        return SCOPES
    granted = set(json.loads(token_file.read_text(encoding="utf-8")).get("scopes") or [])
    return [s for s in SCOPES if s not in granted]


def _save(creds: Credentials, token_file: Path = TOKEN_FILE) -> None:
    token_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = token_file.with_suffix(".tmp")
    tmp.write_text(creds.to_json(), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(token_file)


def load_credentials(token_file: Path = TOKEN_FILE) -> Credentials:
    """저장된 토큰을 읽고, 만료됐으면 갱신한다. 로그인이 필요하면 NotAuthorized.

    token_file: 사용자별 토큰 (가족은 data/users/<이름>/google_token.json). 기본은 소유자 토큰.
    """
    if not token_file.exists():
        raise NotAuthorized("Google 로그인이 필요합니다. 봇에게 'Google 로그인'이라고 보내 주세요.")
    creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as e:  # 갱신 토큰 만료(테스트 모드 7일) 또는 권한 취소
            raise NotAuthorized(f"Google 토큰 갱신 실패, 다시 로그인 필요: {e}") from e
        _save(creds, token_file)
        return creds
    raise NotAuthorized("Google 토큰이 유효하지 않습니다. 다시 로그인하세요.")


def login() -> Credentials:
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_FILE), SCOPES)
    creds = flow.run_local_server(
        host="localhost",
        port=PORT,
        open_browser=False,
        authorization_prompt_message="아래 URL 을 브라우저에서 열어 로그인하세요:\n{url}\n",
        success_message="로그인 완료. 이 창은 닫아도 됩니다.",
        access_type="offline",
        prompt="consent",  # 갱신 토큰을 확실히 받기 위해
    )
    _save(creds)
    return creds


if __name__ == "__main__":
    c = login()
    print("saved:", TOKEN_FILE, "| scopes:", ", ".join(s.rsplit("/", 1)[-1] for s in c.scopes or []))
