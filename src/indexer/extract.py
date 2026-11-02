"""원본 문서 → 마크다운 텍스트.

- PDF: pymupdf(2.x 에 포함된 pymupdf4llm). 페이지당 글자 수가 기준 미만이면 스캔본으로 보고 needs_ocr.
  (OCR 폴백은 Gemini API 키가 생기면 추가. agy 는 이미지 입력을 지원하지 않음)
- DOCX / PPTX / XLSX: MarkItDown
- HWP: 미지원 (unsupported)
"""

from __future__ import annotations

import contextlib
import io
from dataclasses import dataclass
from pathlib import Path

SCANNED_CHARS_PER_PAGE = 50


@dataclass
class Extracted:
    status: str  # ok | needs_ocr | unsupported
    text: str = ""
    pages: int | None = None
    note: str | None = None


def extract(path: Path) -> Extracted:
    ext = path.suffix.lower()
    if ext == ".pdf":
        return _pdf(path)
    if ext in (".docx", ".pptx", ".xlsx"):
        return _markitdown(path)
    return Extracted("unsupported", note=f"{ext} 추출 미지원")


def _pdf(path: Path) -> Extracted:
    import pymupdf
    from pymupdf import pymupdf4llm

    with pymupdf.open(path) as doc:
        pages = doc.page_count
        plain = sum(len(p.get_text().strip()) for p in doc)
        if pages and plain / pages < SCANNED_CHARS_PER_PAGE:
            return Extracted("needs_ocr", pages=pages, note=f"페이지당 {plain / pages:.0f}자 — 스캔본으로 판단")
        # 스캔본은 위에서 걸렀으므로 OCR 은 끈다 (켜 두면 PDF 마다 RapidOCR 경고가 찍힘).
        # pymupdf4llm 이 stdout 으로 진행 메시지를 찍으므로 삼킨다
        with contextlib.redirect_stdout(io.StringIO()):
            text = pymupdf4llm.to_markdown(doc, **_no_ocr())
    return Extracted("ok", text=text, pages=pages)


def _no_ocr() -> dict:
    try:
        from pymupdf4llm.helpers.document_layout import OCRMode
    except ImportError:  # 레이아웃 엔진이 없는 설치에서는 OCR 경로 자체가 없다
        return {}
    return {"use_ocr": OCRMode.NEVER}


_md = None


def _markitdown(path: Path) -> Extracted:
    global _md
    from markitdown import MarkItDown

    _md = _md or MarkItDown()
    text = _md.convert(str(path)).text_content or ""
    if not text.strip():
        return Extracted("needs_ocr", note="추출된 텍스트 없음 (이미지 위주 문서)")
    return Extracted("ok", text=text)
