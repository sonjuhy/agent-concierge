"""색인 대상 원본 문서 탐색.

원본(PDF/PPTX 등)은 ~/Documents 등 제자리에 두고 읽기만 한다. 요약본(섀도우 마크다운)은
<Vault>/_Extracted/Local_Docs/<원본 루트 이름>/<상대 경로>.<확장자>.md 로 만든다.
확장자를 이름에 남겨 같은 이름의 a.pdf / a.pptx 가 충돌하지 않게 한다.

    uv run python -m indexer.sources      # 실제 설정으로 스캔 결과만 출력 (파일 변경 없음)
"""

from __future__ import annotations

import fnmatch
import os
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

DOC_EXTENSIONS = (".pdf", ".pptx", ".docx", ".xlsx", ".hwp")
# 설정과 무관하게 항상 건너뛰는 디렉터리 / 파일 패턴
ALWAYS_EXCLUDED_DIRS = {".git", ".stfolder", ".stversions", ".trash", ".obsidian", "node_modules", ".venv", "venv", "__pycache__"}
TEMP_FILE_PATTERNS = ("~$*", ".~lock*", "*.tmp", ".*")


@dataclass(frozen=True)
class SourceDoc:
    path: Path
    root: Path
    rel: PurePosixPath
    size: int


def is_excluded(rel: PurePosixPath, excludes: Iterable[str]) -> bool:
    """rel(원본 루트 기준) 또는 그 상위 경로가 제외 패턴에 걸리면 True."""
    parts = rel.parts
    if any(p in ALWAYS_EXCLUDED_DIRS for p in parts):
        return True
    prefixes = ["/".join(parts[: i + 1]) for i in range(len(parts))]
    return any(fnmatch.fnmatchcase(pre, pat.strip("/")) for pat in excludes for pre in prefixes)


def iter_documents(
    sources: Iterable[Path],
    excludes: Iterable[str],
    max_bytes: int,
    extensions: tuple[str, ...] = DOC_EXTENSIONS,
    skipped: Counter | None = None,
) -> Iterator[SourceDoc]:
    excludes = tuple(excludes)
    for root in sources:
        if not root.is_dir():
            if skipped is not None:
                skipped["missing source"] += 1
            continue
        for dirpath, dirnames, filenames in os.walk(root):  # 심볼릭 링크는 따라가지 않음
            base = PurePosixPath(Path(dirpath).relative_to(root).as_posix())
            # 제외 폴더는 내려가지 않는다
            # PurePosixPath(".", "x") == PurePosixPath("x") 이므로 루트에서도 그대로 쓸 수 있다
            dirnames[:] = sorted(d for d in dirnames if not is_excluded(PurePosixPath(base, d), excludes))
            for name in sorted(filenames):
                if not name.lower().endswith(extensions):
                    continue
                rel = PurePosixPath(base, name)
                if any(fnmatch.fnmatchcase(name, p) for p in TEMP_FILE_PATTERNS) or is_excluded(rel, excludes):
                    if skipped is not None:
                        skipped["excluded file"] += 1
                    continue
                path = Path(dirpath, name)
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                if size > max_bytes:
                    if skipped is not None:
                        skipped["too large"] += 1
                    continue
                yield SourceDoc(path=path, root=root, rel=rel, size=size)


def shadow_path(doc: SourceDoc, vault: Path) -> Path:
    return vault / "_Extracted" / "Local_Docs" / doc.root.name / f"{doc.rel}.md"


def main() -> None:
    from agent.config import Settings, load_dotenv

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    s = Settings.from_env()
    print("sources :", ", ".join(map(str, s.doc_sources)))
    print("excludes:", ", ".join(s.doc_excludes))
    print(f"max size: {s.doc_max_bytes // 1024 // 1024}MB\n")

    skipped: Counter = Counter()
    docs = list(iter_documents(s.doc_sources, s.doc_excludes, s.doc_max_bytes, skipped=skipped))
    by_folder = Counter(f"{d.root.name}/{d.rel.parts[0] if len(d.rel.parts) > 1 else '.'}" for d in docs)
    by_ext = Counter(d.path.suffix.lower() for d in docs)
    for folder, n in sorted(by_folder.items()):
        print(f"  {n:4d}  {folder}")
    print(f"\n총 {len(docs)}개 ({sum(d.size for d in docs) / 1024 / 1024:.0f}MB)  확장자: {dict(by_ext)}")
    print(f"건너뜀: {dict(skipped)}")
    if docs:
        print(f"예시: {docs[0].path}\n   → {shadow_path(docs[0], s.vault)}")


if __name__ == "__main__":
    main()
