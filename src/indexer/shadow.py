"""섀도우 마크다운 (_Extracted/Local_Docs/...) 작성."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from urllib.parse import quote

import frontmatter

from indexer.extract import Extracted
from indexer.sources import SourceDoc
from indexer.summarize import Summary

# Vault 비대화 방지: 원문은 이 길이까지만 보관 (원본은 source_file 로 언제든 열 수 있음)
MAX_STORED_CHARS = 200_000


def write_shadow(path: Path, doc: SourceDoc, sha256: str, ex: Extracted, s: Summary) -> None:
    meta = {
        "source_file": str(doc.path),
        "source_url": "file://" + quote(str(doc.path)),
        "file_hash": sha256,
        "file_type": doc.path.suffix.lower().lstrip("."),
        "pages": ex.pages,
        "extracted_date": date.today().isoformat(),
        "project": s.project,
        "doc_type": s.doc_type,
        "tags": s.tags,
        "summary": s.summary,
    }
    if s.tag_candidates:
        meta["tag_candidates"] = s.tag_candidates
    # 엔터티 관계: 기준표 대표 이름 (링크는 _Entities 노트 쪽에서 건다)
    if s.author:
        meta["author"] = s.author
    if s.issuer:
        meta["issuer"] = s.issuer
    if s.mentions:
        meta["mentions"] = s.mentions
    if s.entity_candidates:
        meta["entity_candidates"] = s.entity_candidates
    meta = {k: v for k, v in meta.items() if v is not None}

    text = ex.text
    cut = len(text) > MAX_STORED_CHARS
    body = [
        f"# {doc.path.name}",
        "",
        f"[원본 열기]({meta['source_url']})",
        "",
        "## AI 핵심 요약",
        *(f"- {k}" for k in s.key_points),
        "",
        "---",
        "## 원본 추출 텍스트",
        text[:MAX_STORED_CHARS],
    ]
    if cut:
        body.append(f"\n> (원문 {len(text):,}자 중 앞 {MAX_STORED_CHARS:,}자만 보관)")

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(frontmatter.dumps(frontmatter.Post("\n".join(body), **meta)) + "\n", encoding="utf-8")
    tmp.replace(path)
