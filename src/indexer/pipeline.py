"""색인 파이프라인: 스캔 → 변경 감지 → 추출 → 요약 → 섀도우 md → master_index.md.

    uv run python -m indexer                 # 바뀐 문서만 처리
    uv run python -m indexer --limit 5       # 이번 실행에서 최대 5개만 요약
    uv run python -m indexer --dry-run       # 처리 대상만 출력
    uv run python -m indexer --reindex       # 전부 다시 요약 (온톨로지 변경 후)
    uv run python -m indexer --reindex --only Documents/자격증   # 이 경로 아래 문서만 다시 요약
    uv run python -m indexer --index-only    # master_index.md 만 다시 생성

주기 실행은 systemd 타이머(indexer.timer)가 맡는다. inotify(watchdog) 대신 주기 스캔을 쓰는 이유:
Syncthing 이 임시 파일로 받다가 이름을 바꾸는 과정, 서버 재부팅 중 생긴 변경 등을 놓치지 않기 위해서.
크기·mtime 이 같으면 해시도 계산하지 않으므로 스캔 비용은 작다.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import logging
import sys
import time
from collections import Counter
from pathlib import Path

from agent.config import Settings, load_dotenv
from indexer import build_index
from indexer.extract import extract
from indexer.hashdb import HashDB, sha256_file
from indexer.ontology import Ontology
from indexer.shadow import write_shadow
from indexer.sources import SourceDoc, iter_documents, shadow_path
from indexer.summarize import SummarizeError, summarize

log = logging.getLogger("indexer")
ROOT = Path(__file__).resolve().parents[2]
# Syncthing 이 아직 받는 중일 수 있으므로 최근에 바뀐 파일은 다음 실행으로 미룬다
SETTLE_SECONDS = 60


async def process(settings: Settings, onto: Ontology, db: HashDB, doc: SourceDoc, sha: str, mtime: float) -> str:
    out = shadow_path(doc, settings.vault)
    ex = await asyncio.to_thread(extract, doc.path)
    if ex.status != "ok":
        out.unlink(missing_ok=True)  # 예전 버전의 요약본이 남지 않게
        db.upsert(doc.path, sha, doc.size, mtime, ex.status, error=ex.note)
        return ex.status
    fixed = onto.project_for(doc.root.name, doc.rel)
    try:
        s = await summarize(settings, onto, ex.text, f"{doc.root.name}/{doc.rel}", f"file:{doc.root.name}/{doc.rel}", fixed)
    except SummarizeError as e:
        db.upsert(doc.path, sha, doc.size, mtime, "error", error=str(e)[:500])
        return "error"
    write_shadow(out, doc, sha, ex, s)
    db.upsert(doc.path, sha, doc.size, mtime, "ok", shadow=out, cost_usd=s.cost_usd)
    return "ok"


def remove_deleted(db: HashDB, seen: set[str]) -> int:
    n = 0
    for path in db.paths() - seen:
        row = db.get(Path(path))
        if row and row.shadow:
            Path(row.shadow).unlink(missing_ok=True)
        db.delete(path)
        n += 1
    return n


def in_scope(doc: SourceDoc, only: str | None) -> bool:
    """--only 경로 접두사("<원본 루트 이름>/<상대 경로>") 안의 문서인지."""
    if not only:
        return True
    full, prefix = f"{doc.root.name}/{doc.rel}", only.strip("/")
    return full == prefix or full.startswith(prefix + "/")


async def run(settings: Settings, limit: int | None, dry_run: bool, reindex: bool, only: str | None = None) -> Counter:
    onto = Ontology.load(settings.vault / "_Indexes" / "ontology_schema.yaml")
    db = HashDB(ROOT / "data" / "index.db")
    counts: Counter = Counter()
    seen: set[str] = set()
    todo: list[tuple[SourceDoc, str, float]] = []

    for doc in iter_documents(settings.doc_sources, settings.doc_excludes, settings.doc_max_bytes):
        seen.add(str(doc.path))  # 범위 밖 문서도 '존재함'으로 기록해야 삭제 처리되지 않는다
        if not in_scope(doc, only):
            counts["out_of_scope"] += 1
            continue
        st = doc.path.stat()
        if time.time() - st.st_mtime < SETTLE_SECONDS:
            counts["settling"] += 1
            continue
        row = db.get(doc.path)
        if not reindex and row and row.size == st.st_size and row.mtime == st.st_mtime:
            counts["unchanged"] += 1
            continue
        sha = sha256_file(doc.path)
        if not reindex and row and row.sha256 == sha:
            db.touch(doc.path, st.st_size, st.st_mtime)
            counts["unchanged"] += 1
            continue
        todo.append((doc, sha, st.st_mtime))

    log.info("scan: %d docs, %d to process, %s", len(seen), len(todo), dict(counts))
    if dry_run:
        for doc, _, _ in todo[:limit]:
            print(f"  {doc.root.name}/{doc.rel}")
        return counts

    for i, (doc, sha, mtime) in enumerate(todo[:limit] if limit else todo, 1):
        started = time.monotonic()
        status = await process(settings, onto, db, doc, sha, mtime)
        counts[status] += 1
        log.info("[%d/%d] %s %.1fs %s/%s", i, len(todo), status, time.monotonic() - started, doc.root.name, doc.rel)

    if not limit or len(todo) <= limit:
        counts["deleted"] = remove_deleted(db, seen)
    build_index.write(settings.vault)
    log.info("done: %s, db=%s", dict(counts), db.stats())
    return counts


def main() -> int:
    ap = argparse.ArgumentParser(prog="python -m indexer")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reindex", action="store_true")
    ap.add_argument("--index-only", action="store_true")
    ap.add_argument("--only", metavar="PATH", help="이 경로 아래 문서만 처리 (예: Documents/자격증)")
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    if args.index_only:
        print(build_index.write(settings.vault))
        return 0

    lock = (ROOT / "data").joinpath("indexer.lock")
    lock.parent.mkdir(exist_ok=True)
    with lock.open("w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.info("another indexer run is in progress; exiting")
            return 0
        counts = asyncio.run(run(settings, args.limit, args.dry_run, args.reindex, args.only))
    return 1 if counts.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
