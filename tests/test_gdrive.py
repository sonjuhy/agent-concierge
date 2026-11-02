from datetime import datetime

import frontmatter

from agent.context import KST
from indexer import build_index, gdrive

RAW = (
    "# 최근 24시간 수정 Google Docs 요약 (2026-10-22)\n\n"
    "- **조회 기준 일시**: 2026-10-22 16:17:21 KST  \n"
    "- **조회 범위**: 10-21 \\~ 10-22  \n\n"
    "### 1\\. [Multimodal\\_Checker](https://docs.google.com/x)\n"
)


def fake_fetch(files: dict[str, str]):
    def fetch(dest):
        for name, text in files.items():
            (dest / name).write_text(text, encoding="utf-8")
    return fetch


def test_clean_name_and_unescape():
    assert gdrive.clean_name("2026-10-22_Summary.md.md") == "2026-10-22_Summary.md"
    assert gdrive.clean_name("plain.md") == "plain.md"
    out = gdrive.to_shadow(RAW, "a.md", datetime(2026, 10, 23, 4, 30, tzinfo=KST))
    post = frontmatter.loads(out)
    assert post["title"] == "최근 24시간 수정 Google Docs 요약 (2026-10-22)"
    assert post["project"] == gdrive.PROJECT and post["source_file"] == "gdrive:Spark_Sync/a.md"
    assert "10-21 ~ 10-22" in post.content and "Multimodal_Checker" in post.content and "1. [" in post.content
    assert post["summary"].startswith("조회 기준 일시")


def test_sync_new_unchanged_updated_deleted(tmp_path):
    vault = tmp_path / "vault"
    c = gdrive.sync(vault, fake_fetch({"d1.md.md": RAW, "d2.md.md": RAW}))
    assert (c["new"], c["deleted"]) == (2, 0)
    assert (vault / "_Extracted/GDrive/d1.md").exists()

    c = gdrive.sync(vault, fake_fetch({"d1.md.md": RAW, "d2.md.md": RAW + "추가\n"}))
    assert (c["unchanged"], c["updated"]) == (1, 1)

    c = gdrive.sync(vault, fake_fetch({"d2.md.md": RAW}))
    assert c["deleted"] == 1 and not (vault / "_Extracted/GDrive/d1.md").exists()

    index = build_index.build(vault)
    assert f"### {gdrive.PROJECT}" in index and "_Extracted/GDrive/d2.md|d2.md" in index
