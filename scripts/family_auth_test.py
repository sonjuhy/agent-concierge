"""가족 계정 Google 로그인 시험 (멀티 사용자 확정 전 검증용). 내 토큰(data/google_token.json)은 건드리지 않는다.

    uv run python scripts/family_auth_test.py <이름>            # SSH 터널(8765)로 브라우저 로그인
    uv run python scripts/family_auth_test.py <이름> --paste    # 휴대폰 등 1단계: 로그인 주소 출력
    uv run python scripts/family_auth_test.py <이름> --finish '<주소창 주소>'   # 2단계: 로그인 후 주소로 마무리

확인하는 것:
1. "확인되지 않은 앱" 화면을 넘어 로그인이 끝나는지 (Gmail 권한은 제한 범위라 막힐 수 있음)
2. 요청한 권한이 모두 부여됐는지
3. 캘린더 목록·안 읽은 메일 '개수'만 읽어 실제 접근이 되는지 (내용은 출력하지 않음)
토큰은 data/family/<이름>_google_token.json (600) 에 저장된다. 시험 후 지우려면 그 파일을 삭제하면 된다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from agent.tools.google_auth import CLIENT_FILE, PORT, SCOPES  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("name")
    ap.add_argument("--paste", action="store_true", help="1단계: 로그인 주소만 출력 (PKCE 값은 파일에 보관)")
    ap.add_argument("--finish", metavar="URL", help="2단계: 로그인 후 주소창의 http://localhost:8765/?… 주소")
    args = ap.parse_args()
    out = ROOT / "data" / "family" / f"{args.name}_google_token.json"
    pending = out.with_name(f"{args.name}_pending.json")
    out.parent.mkdir(parents=True, exist_ok=True)

    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_FILE), SCOPES)
    flow.redirect_uri = f"http://localhost:{PORT}/"
    if args.paste:
        url, state = flow.authorization_url(access_type="offline", prompt="consent")
        # 2단계는 별도 프로세스라 state 와 PKCE code_verifier 를 넘겨줘야 한다 (10분 안에 쓰는 일회용 값)
        pending.write_text(json.dumps({"state": state, "code_verifier": flow.code_verifier}), encoding="utf-8")
        os.chmod(pending, 0o600)
        print("아래 주소를 가족분 브라우저(휴대폰 가능)에서 열어 로그인하세요:\n" + url)
        print("\n마지막에 '사이트에 연결할 수 없음'이 떠도 정상입니다. 주소창의 주소 전체를 복사해 서버에서:")
        print(f"  uv run python scripts/family_auth_test.py {args.name} --finish '<복사한 주소>'")
        return 0
    if args.finish:
        saved = json.loads(pending.read_text(encoding="utf-8"))
        flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_FILE), SCOPES, state=saved["state"],
                                                         code_verifier=saved["code_verifier"])
        flow.redirect_uri = f"http://localhost:{PORT}/"
        # oauthlib 은 http 리디렉션을 거부하므로 localhost 주소를 https 로 바꿔 코드만 읽게 한다
        flow.fetch_token(authorization_response=args.finish.strip().replace("http://", "https://", 1))
        pending.unlink(missing_ok=True)
        creds = flow.credentials
    else:
        creds = flow.run_local_server(host="localhost", port=PORT, open_browser=False,
                                      authorization_prompt_message="아래 주소를 열어 로그인하세요:\n{url}\n",
                                      success_message="로그인 완료. 창을 닫아도 됩니다.",
                                      access_type="offline", prompt="consent")
    out.write_text(creds.to_json(), encoding="utf-8")
    os.chmod(out, 0o600)

    granted = set(creds.scopes or [])
    missing = [s.rsplit("/", 1)[-1] for s in SCOPES if s not in granted]
    print(f"\n[1] 로그인 완료, 갱신 토큰: {bool(creds.refresh_token)}")
    print(f"[2] 권한: {'모두 부여됨' if not missing else '빠짐 → ' + ', '.join(missing)}")
    cal = build("calendar", "v3", credentials=creds, cache_discovery=False)
    print(f"[3] 캘린더 {len(cal.calendarList().list().execute().get('items', []))}개 접근 가능")
    gm = build("gmail", "v1", credentials=creds, cache_discovery=False)
    unread = gm.users().messages().list(userId="me", q="is:unread newer_than:7d", maxResults=50).execute()
    print(f"    Gmail 최근 7일 안 읽은 메일 약 {unread.get('resultSizeEstimate', 0)}통 (내용은 읽지 않음)")
    print(f"토큰: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
