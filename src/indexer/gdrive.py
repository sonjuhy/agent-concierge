"""Gemini Spark 요약(Google Drive/Spark_Sync) → Vault/_Extracted/GDrive.

Spark 는 매일 04:00 에 최근 Google 문서를 요약해 Spark_Sync 폴더에 **Google 문서**로 저장한다 (.md 파일을 만들 수 없음).
여기서 rclone 으로 마크다운으로 내보내 받아 오고, master_index.md 에 실리도록 YAML 머리말을 붙인다.

    uv run python -m indexer.gdrive        # 수동 동기화 (systemd gdrive-sync.timer 가 매일 04:30 실행)

Drive 쪽 파일이 지워지면 Vault 의 사본도 지운다. 내용(해시)이 같으면 다시 쓰지 않는다.
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import frontmatter

from agent.context import KST

log = logging.getLogger("indexer.gdrive")
REMOTE = "gdrive:Spark_Sync"
PROJECT = "Google Drive (Spark)"
# rclone 의 Docs→md 내보내기가 넣는 백슬래시 이스케이프 (\_ \~ 1\. 등)
ESCAPE_RE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!~|>])")


def clean_name(name: str) -> str:
    """'2026-10-22_Google_Docs_Summary.md.md' → '2026-10-22_Google_Docs_Summary.md'"""
    stem = name
    while stem.lower().endswith(".md"):
        stem = stem[:-3]
    return f"{stem}.md"


def summary_line(body: str, limit: int = 200) -> str:
    """제목·빈 줄을 건너뛴 첫 의미 있는 줄들을 요약으로 쓴다."""
    out = []
    for line in body.splitlines():
        s = re.sub(r"[*`>#]+", "", line).strip(" -\t")
        if not s or line.lstrip().startswith("#"):
            continue
        out.append(s)
        if sum(len(x) for x in out) >= limit:
            break
    return " / ".join(out)[:limit]


def to_shadow(raw: str, name: str, synced: datetime) -> str:
    body = ESCAPE_RE.sub(r"\1", raw).strip()
    m = re.search(r"^#\s+(.+)$", body, re.M)
    meta = {
        "source": "gdrive",
        "source_file": f"{REMOTE}/{name}",
        "file_hash": hashlib.sha256(raw.encode()).hexdigest(),
        "project": PROJECT,
        "doc_type": "보고서",
        "tags": [],
        "title": m.group(1).strip() if m else Path(name).stem,
        "summary": summary_line(body),
        "synced_at": f"{synced:%Y-%m-%d %H:%M}",
    }
    return frontmatter.dumps(frontmatter.Post(body, **meta)) + "\n"


def rclone_copy(dest: Path, remote: str = REMOTE) -> None:
    subprocess.run(
        ["rclone", "copy", remote, str(dest), "--drive-export-formats", "md"],
        check=True, capture_output=True, text=True, timeout=600,
    )


def sync(vault: Path, fetch=rclone_copy) -> dict[str, int]:
    out_dir = vault / "_Extracted" / "GDrive"
    out_dir.mkdir(parents=True, exist_ok=True)
    counts = {"new": 0, "updated": 0, "unchanged": 0, "deleted": 0}
    synced = datetime.now(KST)
    with tempfile.TemporaryDirectory(prefix="gdrive-") as tmp:
        staging = Path(tmp)
        fetch(staging)
        keep: set[str] = set()
        for src in sorted(staging.rglob("*.md")):
            name = clean_name(src.name)
            keep.add(name)
            raw = src.read_text(encoding="utf-8", errors="replace")
            dest = out_dir / name
            digest = hashlib.sha256(raw.encode()).hexdigest()
            if dest.exists():
                try:
                    if frontmatter.load(dest).metadata.get("file_hash") == digest:
                        counts["unchanged"] += 1
                        continue
                except Exception:
                    pass
                counts["updated"] += 1
            else:
                counts["new"] += 1
            tmp_out = dest.with_name(dest.name + ".tmp")
            tmp_out.write_text(to_shadow(raw, name, synced), encoding="utf-8")
            tmp_out.replace(dest)
    for old in out_dir.glob("*.md"):
        if old.name not in keep:
            old.unlink()
            counts["deleted"] += 1
    return counts


def main() -> int:
    from agent.config import Settings, load_dotenv
    from indexer import build_index

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if shutil.which("rclone") is None:
        log.error("rclone not found")
        return 1
    settings = Settings.from_env()
    try:
        counts = sync(settings.vault)
    except subprocess.CalledProcessError as e:
        log.error("rclone failed: %s", (e.stderr or "")[-500:])
        return 1
    build_index.write(settings.vault)
    log.info("gdrive sync: %s", counts)
    return 0


if __name__ == "__main__":
    sys.exit(main())
