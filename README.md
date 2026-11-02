# agent-concierge

개인 AI 에이전트 코어 데몬. Slack ↔ Ubuntu 서버 ↔ Obsidian Vault 를 잇고,
Tier 1 은 `agy` (Gemini 3.8 Flash), Tier 2 는 `claude -p` 로 처리한다.

## 구조

```
src/agent/
  config.py       환경 변수 설정 (.env / systemd EnvironmentFile)
  cli_runner.py   agy / claude -p 서브프로세스 래퍼 (타임아웃·프로세스 그룹 kill·JSON 정규화)
  tools/          Gmail / Calendar 네이티브 함수 (4주차)
src/indexer/      문서 전처리·인덱서 (2주차)
systemd/          서비스 유닛 (4주차)
scripts/
  spike_phase0.py 0단계 검증 스파이크
tests/
```

## Vault (`~/Obsidian`)

사용자 노트는 루트에 자유 구조로 두고, 에이전트용 폴더만 `_` 접두사로 추가한다.
Syncthing 으로 Windows PC 와 양방향 동기화된다.

```
~/Obsidian/
├── _Indexes/              ontology_schema.yaml, master_index.md (Python 이 생성)
├── _Entities/{프로젝트,조직,인물}/  엔터티 노트 (Python 이 생성, 관계·관련 문서 링크)
├── _Extracted/
│   ├── Local_Docs/        _Assets 의 PDF/PPTX 에서 추출한 섀도우 마크다운
│   └── GDrive/            Google Drive 문서 요약
├── _Agent_Inbox/          에이전트가 쓸 수 있는 유일한 폴더
├── _Assets/{pdf,pptx}/    (선택) Vault 안에 두고 싶은 원본만 넣는 곳
└── (기존 노트들)
```

`~/Documents/SecondBrain` 은 추후 읽기 전용 소스로 추가 예정.

## 일정·메일 (Google Calendar / Gmail)

최초 1회 로그인: `uv run python -m agent.tools.google_auth` (다른 PC 브라우저면 먼저 `ssh -L 8765:localhost:8765 …`).
클라이언트는 `data/google_client.json`(데스크톱 앱), 토큰은 `data/google_token.json`(600). 동의 화면은 프로덕션 상태로 둔다.

| 요청 예 | 처리 | 승인 |
|---|---|---|
| "다음 주 일정 보여줘" | Python 이 일정 목록을 직접 만든다 (LLM 미사용) | 불필요 |
| "안 읽은 메일 중 중요한 거" | 발신자·제목·미리보기를 **도구 없는 claude** 가 요약, 스레드는 오염 표시 | 불필요 |
| "내일 3시 치과 일정 추가해줘" | 내용 확인 후 Slack [승인] 을 눌러야 생성 | 필요 |
| "a@b.com 에게 … 메일 초안 써줘" | [승인] 시 Gmail 임시보관함에 초안만 생성 (발송 안 함) | 필요 |

- 일정·메일 단어가 있는 메시지만 플래너(claude Haiku, 도구 없음, `--json-schema`)로 의도·날짜를 뽑는다. 노트·문서 찾기는 기존 agy 경로로 간다
- 승인 대기 작업은 `pending_actions` 테이블에 24시간 보관, 한 번만 실행된다. 허용 사용자만 승인 가능
- 아직 없는 것: 메일 발송, 일정 수정·삭제, Google Drive

## 예약 작업·아침 브리핑·헬스체크 (`scheduler.py`, `health.py`)

APScheduler 가 데몬 안에서 돌고, 예약은 `data/jobs.db` 에 저장돼 재시작 후에도 유지된다.
주기적 감시는 Python 이 하고, AI(플래너)는 Slack 요청에서 아래 템플릿의 파라미터만 뽑는다 (CLI 반복 호출 없음).

| 템플릿 | Slack 요청 예 | 동작 |
|---|---|---|
| 아침 브리핑 | 기본 매일 07:30 (`AGENT_BRIEFING_TIME`), "브리핑 8시로 바꿔줘" | 헬스체크 경고 + 오늘 일정 + 지난 하루 안 읽은 메일 요약 |
| 알림 | "오늘 3시에 약 먹으라고 알려줘" | 지정 시각에 DM |
| 반복 알림 | "매일 7시 반에 …", "평일 6시마다 …", "매주 월요일 9시 …", "매달 25일 10시 …" | 매일·평일·주말·매주 요일·매월 날짜 |
| 이름으로 취소 | "분리수거 알림 취소해줘" | 플래너가 [등록된 예약] 목록에서 골라 취소 |
| 메일 감시 | "boss@x.com 메일 30분마다 확인해줘" | 새 메일의 발신자·제목만 DM (LLM 미사용, 최소 5분) |
| 목록·취소 | "예약 작업 보여줘", "mailwatch:… 취소해줘" | |

- 메일 내용이 들어간 브리핑·메일 감시 메시지의 스레드는 오염 표시 (이어지는 대화는 도구 없는 claude)
- 헬스체크 `uv run python -m agent.health`: agy·claude 응답, Google 토큰, Syncthing(기기 연결·폴더 오류), 색인 마지막 결과, 디스크

## 할 일 (`todos.py`)

예약(리마인드)은 '알려 주기'만 하고, 할 일은 **끝냈는지**를 기록한다. `data/agent.db` 의 `todos` 테이블, 사용자별로 분리.

| 요청 예 | 동작 |
|---|---|
| "금요일까지 발표자료 만들기 할 일 추가하고 목요일 2시에 알려줘" | 기한과 함께 추가하고 알림 예약 (알림에 **완료** 버튼) |
| "할 일 뭐 남았어?" / "완료한 것도 보여줘" | 기한 순 목록, `#번호`, 지난 기한은 ⚠️ |
| "#3 다음주 월요일로 바꿔줘" / "#3 내일 9시에 알려줘" | 내용·기한 수정, 알림을 새로 걸면 이전 알림은 취소 |
| "세탁소 다녀왔어" / "#3 했어" / "#3 다시 열어줘" / "#3 지워줘" | 완료(알림 취소) · 되돌리기 · 삭제 |

- 플래너에 `[할 일]` 목록(번호·내용·기한)을 함께 줘서 "세탁소 다녀왔어" 처럼 번호 없이 말해도 고른다
- 알림 시각에 이미 끝냈거나 지운 할 일이면 보내지 않는다
- 아침 브리핑에 '할 일' 섹션: 기한이 지났거나 오늘인 것, 나머지는 개수만

## Slack 대화 기록 (`_Chat_Logs/`)

Slack 무료 플랜은 90일 지난 메시지를 숨기고 1년 지나면 삭제하므로, 데몬이 모든 질문·답변을 Vault 에 백업한다.

- 스레드 하나 = 파일 하나: `_Chat_Logs/YYYY/MM/YYYY-MM-DD_HHMM_<첫 질문>.md` (실패·시간 초과도 기록)
- `_Agent_Inbox` 가 아닌 별도 폴더: agy 는 쓰기 금지, Python 데몬만 기록한다
- 외부 텍스트가 섞인(오염된) 스레드는 `_Chat_Logs/_untrusted/` 로 옮기고 agy 의 읽기를 금지한다 (Obsidian 에서만 열람)
- `master_index.md` 의 '대화 기록' 섹션에 월별 목록이 실려, "지난달 대화" 같은 요청은 인덱스 → 해당 파일 순으로 찾는다
- Windows 동기화와 자정 git 스냅샷에 함께 포함된다. 비밀번호 등 민감 정보는 봇에게 보내지 않는다

## 문서 색인 (원본은 제자리)

원본 PDF/PPTX/DOCX/XLSX/HWP 는 `~/Documents` 에 그대로 두고(이미 Syncthing 동기화 중),
Python 색인기가 읽기만 해서 요약본을 Vault 에 만든다. AI 는 원본에 접근하지 않는다
(agy 는 bwrap 으로 `~/Documents` 자체가 보이지 않음).

```
~/Documents/채용/공고.pdf
  → ~/Obsidian/_Extracted/Local_Docs/Documents/채용/공고.pdf.md   (source_file: 원본 절대 경로)
```

- 설정: `AGENT_DOC_SOURCES`(기본 `~/Documents`, `<Vault>/_Assets`), `AGENT_DOC_EXCLUDES`, `AGENT_DOC_MAX_MB`(50)
- 기본 제외 없음: 민감한 폴더(가족·계약·이력서 등)는 `.env` 의 `AGENT_DOC_EXCLUDES` 에 꼭 적는다.
  요약본은 Vault 를 통해 동기화되고 AI 가 읽는다.
- 항상 제외: `.git`, `.stversions`, `node_modules` 등, `~$*` 같은 Office 임시 파일
- 요약은 **도구 없는 claude(untrusted 경로)** 로 한다. 내려받은 PDF 에는 남이 쓴 글이 섞여 있기 때문.
- 원본이 Vault 밖이라 `[[위키링크]]` 대신 원본 경로(`file://`)로 연결한다.
- 미리보기: `uv run python -m indexer.sources` (파일 변경 없음). 2026-10-22 기준 113개, 105MB.

### 파이프라인 (`python -m indexer`, `indexer.timer` 15분 주기)

1. 스캔: 크기·mtime 이 같으면 건너뛰고, 다르면 SHA-256 비교 (`data/index.db`). 최근 60초 내 바뀐 파일은 Syncthing 수신 중일 수 있어 다음 실행으로 미룸
2. 추출: PDF → pymupdf(4llm), DOCX/PPTX/XLSX → MarkItDown. 페이지당 50자 미만 PDF 는 `needs_ocr`(OCR 미구현), HWP 는 `unsupported`
3. 요약: 앞 30,000자를 **도구 없는 claude(Haiku)** 에 `--json-schema` 로 보내 summary·key_points·doc_type·tags·project 를 받음.
   프로젝트는 `ontology_schema.yaml` 의 경로 접두사로 먼저 고정하고, 태그·유형은 기준표에 있는 값만 남김. 문서당 약 $0.003
4. 저장: `_Extracted/Local_Docs/<루트>/<경로>.<확장자>.md` (YAML 헤더 + 핵심 요약 + 원문 최대 20만 자)
5. 원본이 지워지면 요약본도 지우고, 마지막에 `_Entities/` 노트와 `_Indexes/master_index.md` 재생성 (토큰 0)

- 기준표: `~/Obsidian/_Indexes/ontology_schema.yaml` (직접 수정, 처음엔 `docs/ontology_schema.example.yaml` 복사). 태그·유형을 바꾼 뒤엔 `python -m indexer --reindex`,
  엔터티·관계만 바꿨으면 다음 15분 주기 또는 `python -m indexer --index-only` 로 충분

### 엔터티·관계

| 관계 | 출처 |
|---|---|
| 문서 → 프로젝트 (`속함`) | 경로 규칙 + 요약 AI |
| 프로젝트 → 프로젝트 (`parent`) / → 조직 (`org`, 비우면 상위에서 상속) | 기준표에 직접 |
| 인물 → 조직 (`소속`, 기간) | 기준표에 직접 |
| 문서 → 인물 (`author`: 기준표 인물 / 타인 / 불명) | 요약 AI (기준표 인물 중에서만) |
| 문서 → 조직 (`issuer`, 발급·발행) | 요약 AI (기준표 조직 중에서만) |
| 문서 → 조직·인물 (`mentions`, 최대 5) | 요약 AI + 요약·핵심 요약의 이름·별칭 문자열 일치 |

- 기준표에 없는 이름을 가리키는 관계는 무시하고 경고만 남긴다. 인물은 제3자 개인정보가 쌓이지 않게 필요한 사람만 적는다
- AI 가 별칭으로 답해도 대표 이름으로 바꾼다. 기준표에 없는 **조직**은 `entity_candidates` 로 받아 `_Entities/_후보.md` 에 모은다 (개인 이름은 받지 않음)
- 조직·인물을 기준표에 추가한 뒤 작성·발급 관계까지 반영하려면 `--reindex` (문서당 약 $0.003). 언급(문자열 일치)은 다음 색인부터 바로 반영
- `_Entities/` 는 매번 다시 만들고(`generated: true` 노트만 지움), Vault git 스냅샷에서는 제외. Syncthing 으로 Windows Obsidian 에 동기화되어 백링크·그래프에 보인다
- agy 는 프로젝트·조직·인물 질문에 master_index 의 '엔터티' 섹션 → 해당 노트 순서로 연다
- 미구현: 후보 알림(브리핑), 일정 참석자·대화 기록과의 연결

### Google Drive (Gemini Spark → `_Extracted/GDrive/`)

Spark 예약 작업이 매일 04:00 에 최근 Google 문서를 요약해 Drive `Spark_Sync/` 에 **Google 문서**로 저장한다.
`gdrive-sync.timer` 가 04:30 에 `rclone copy gdrive:Spark_Sync --drive-export-formats md` 로 받아,
rclone 이스케이프(`\_`, `\~`)를 되돌리고 YAML 머리말(title, summary, project=`Google Drive (Spark)`)을 붙여 저장한 뒤 인덱스를 다시 만든다.
Drive 에서 지운 문서는 Vault 에서도 지운다. 수동 실행: `uv run python -m indexer.gdrive`

- Spark 는 실험 기능이고 Drive 파일 이름 변경·삭제 권한도 있으므로, 작업 지시문에 "Spark_Sync 에 새 문서를 만드는 것 외에는 수정·삭제 금지"를 넣어 둔다
- 남이 공유한 문서의 요약도 들어올 수 있다 (로컬 문서 요약본과 같은 수준으로 취급)
- 옵션: `--limit N`, `--dry-run`, `--index-only`. 로그: `journalctl --user -u indexer`
- agy 지침: 노트·문서 검색 시 `master_index.md` 를 먼저 읽고 고른 파일만 연다

## 라우팅·격리 결정 (2026-10-22)

| 요청 | 엔진 | 도구 | 세션 재개 |
|---|---|---|---|
| 일반 | `agy` | 있음 | O |
| `--deep` / `--claude` | `claude -p` | 있음 | O |
| 외부 텍스트 포함 (메일/웹) 또는 오염된 스레드 | `claude -p --tools "" --strict-mcp-config` | 없음 | X (1회성) |

- `agy -p` 는 파일 쓰기를 승인 없이 실행하고 `--sandbox` 로도 막히지 않는다 → untrusted 금지.
- bwrap 만으로는 agy 의 `search_web`/`send_message`/`schedule` 경로 유출을 막지 못한다 → untrusted 용으로는 채택하지 않음.
- untrusted 출력이 들어간 스레드는 tainted 로 표시하고 이후 요청도 untrusted 경로로 보낸다.

### 데몬 전용 agy 프로필 (`agy_profile.py`)

데몬의 agy 는 전역 `~/.gemini` 설정을 쓰지 않는다. `HOME=data/agy-home` 으로 바꿔
전역 allow 목록·trustedWorkspaces 가 섞이지 않게 하고, 매 실행 전에 settings.json 을 다시 쓴다
(agy 가 스스로 덮어쓰기 때문). OAuth 토큰은 처음 한 번 전역에서 복사한다(chmod 600).

| 계층 | 막는 것 | 실측 |
|---|---|---|
| deny 규칙 | 셸 명령(`command(*)`), URL, MCP, 프로필 홈(토큰·설정) 읽기/쓰기, `_Agent_Inbox` 를 뺀 Vault 루트의 모든 항목 쓰기 (매 실행마다 다시 계산) | 강제됨 |
| allow 규칙 | `_Agent_Inbox` 쓰기만 명시 허용. 그 밖의 쓰기는 print 모드에서 확인 불가로 자동 거부(soft-deny) | 강제됨 |
| bwrap | 실제 홈 숨김(tmpfs), Vault 읽기 전용, `_Agent_Inbox`·프로필 홈만 쓰기 | 강제됨 |
| `allowNonWorkspaceAccess: false` | (워크스페이스 밖 접근) | **강제 안 됨** — 의도 표시용 |
| `disabledTools` | (도구 비활성화) | **무시됨** — 사용 안 함 |
| `unsandboxed(...)` 규칙 | — | **유효하지 않은 규칙** (agy 경고) — 사용 안 함 |

허용되는 것: Vault 읽기, `_Agent_Inbox/` 쓰기, `search_web` 등 끌 수 없는 내장 도구.

## 설정·비밀값 (저장소에 올리지 않는 것)

비밀값과 개인 데이터는 모두 `.env` 와 `data/` 에만 두며, 둘 다 `.gitignore` 에 있다. 저장소에는 예시 파일만 올린다.

| 파일 | 내용 | 만드는 법 |
|---|---|---|
| `.env` | Slack 토큰(`SLACK_APP_TOKEN`·`SLACK_BOT_TOKEN`), 허용 사용자 ID, 모델·경로·제외 폴더 | `cp .env.example .env && chmod 600 .env` |
| `data/google_client.json` | Google Cloud OAuth 클라이언트 (데스크톱 앱) | Cloud 콘솔에서 내려받기 |
| `data/google_token.json` | Google 로그인 토큰 (자동 갱신) | `uv run python -m agent.tools.google_auth` |
| `data/users.yaml` | 멀티 사용자 목록 (선택) | [`docs/users.example.yaml`](docs/users.example.yaml) 복사 |
| `data/agy-home/` | 데몬 전용 agy 로그인 프로필 | 아래 "agy 프로필" 참고 |
| `data/*.db` | 예약·할 일·대화 상태 | 자동 생성 |
| `<Vault>/_Indexes/ontology_schema.yaml` | 프로젝트·조직·인물 기준표 (실제 소속 정보) | [`docs/ontology_schema.example.yaml`](docs/ontology_schema.example.yaml) 복사 |

커밋 전 확인 (출력이 없어야 한다): `git grep -nIE 'xox[abp]-[0-9]|xapp-[0-9]-|GOCSPX-|AIza[0-9A-Za-z_-]{30}' -- ':!README.md'`

## 운영 (systemd 사용자 서비스)

```bash
ln -sf ~/agent/systemd/*.service ~/agent/systemd/*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now agent.service vault-snapshot.timer indexer.timer gdrive-sync.timer
sudo loginctl enable-linger $USER     # 로그인하지 않아도(재부팅 후에도) 서비스 실행
```

- `agent.service`: Slack 데몬. 실패 시 10초 후 재시작. 로그: `journalctl --user -u agent -f`
- `vault-snapshot.timer`: 매일 자정 `~/Obsidian` 로컬 git 커밋(원격 없음). 제외 규칙은 Vault 에 파일을
  만들지 않도록 `.git/info/exclude` 에 있다(`_Assets`, `_Extracted`, 캐시, 플러그인 코드 등).
  `.git` 은 서버 `.stignore` 로 Windows 동기화에서 제외. 복구: `git -C ~/Obsidian log -- <파일>` → `git checkout <커밋> -- <파일>`
- 코드 변경 후: `systemctl --user restart agent`

## 쇼핑 (지원 안 함)

쇼핑 검색·가격 알림 요청에는 "지원하지 않는다"고 답한다.
- 네이버 쇼핑 검색 API: 2026-07-31 대체 없이 종료 (남은 검색 API 는 NAVER API HUB 로 이관, 2026-09-07 부터 AI 입력 금지 약관)
- 쿠팡 파트너스 Open API: 판매 실적 기반 최종 승인 후 키 발급, 검색 시간당 약 10회·결과 10개·가격 필터 없음
- 사이트 크롤링·자동 구매는 약관·봇 차단 문제로 하지 않는다

## 멀티 사용자(가족)

`data/users.yaml`([예시](docs/users.example.yaml))을 두면 가족이 각자 봇과의 DM 으로 쓴다. 없으면 1인 구성.
가족은 각자 agy(Gemini) 계정으로 대화·요청 해석, 메일은 목록만(API 키 없음), `--deep`·문서 색인 없음.
폴더·agy 프로필·Google 토큰·대기열·예약·승인이 사람별로 나뉘고, 로그인은 DM 으로(`agy 로그인`, `구글 로그인`).
자세한 내용: [docs/MULTI_USER.md](docs/MULTI_USER.md)

## 개발

```bash
uv sync
uv run python -m agent              # Slack 데몬 (DM/멘션 → 라우터 → agy/claude → 스레드 답변)
uv run python scripts/check_slack.py
uv run pytest
uv run python scripts/spike_phase0.py
```

## 라이선스

[Apache License 2.0](LICENSE)
