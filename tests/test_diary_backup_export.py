# ============================================================
# diary 异地备份导出测试（交接事项 a）
#
# diary.db 是二进制 SQLite，不在 github_sync 的 .md 收集范围内，此前只有
# 持久盘上一份；embeddings.db 丢了能 backfill 重建，diary 丢了就没了。
# 这里覆盖：
#   ① export_markdown：分天、含写入时间、正文换行折成一行、空/缺库不报错
#   ② 导出不受 7 天窗口约束（备份要全量，不能只备份一个读取窗口）
#   ③ sync() 把 diary.md 挂进文件集，并进 manifest
#   ④ 盘上若存在 diary.md（import 拉回来的），不被当普通桶收走造成旧盖新
#   ⑤ 导出失败不影响桶的同步
# ============================================================

import os
import sqlite3
from datetime import datetime, timedelta

import pytest

from github_sync import GitHubSync, _DIARY_EXPORT_FILENAME
from tools.diary import core as diary


def _make_db(buckets_dir, rows):
    """rows = [(date, created_at, content), ...]，按给定顺序插入。"""
    path = diary.db_path_for(str(buckets_dir))
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS diary (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date DATE NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL
        )
    """)
    conn.executemany(
        "INSERT INTO diary (date, content, created_at) VALUES (?, ?, ?)",
        [(d, c, ts) for d, ts, c in rows],
    )
    conn.commit()
    conn.close()
    return path


# -------------------- export_markdown --------------------

def test_export_groups_by_day_and_keeps_write_time(tmp_path):
    path = _make_db(tmp_path, [
        ("2026-07-20", "2026-07-20T09:00:00", "出差到周五"),
        ("2026-07-20", "2026-07-20T21:30:00", "晚上加班"),
        ("2026-07-21", "2026-07-21T08:15:00", "胃疼两天了"),
    ])

    out = diary.export_markdown(path)

    assert "## 2026-07-20" in out
    assert "## 2026-07-21" in out
    assert "- [2026-07-20T09:00:00] 出差到周五" in out
    assert "- [2026-07-20T21:30:00] 晚上加班" in out
    assert "- [2026-07-21T08:15:00] 胃疼两天了" in out
    # 同一天两条按写入顺序，且日期段只出现一次
    assert out.index("出差到周五") < out.index("晚上加班")
    assert out.count("## 2026-07-20") == 1
    assert "条数：3" in out


def test_export_covers_everything_not_just_the_seven_day_window(tmp_path):
    """备份是全量的：diary_read 只给 7 天，导出必须把更早的也带上。"""
    old = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
    new = datetime.now().strftime("%Y-%m-%d")
    path = _make_db(tmp_path, [
        (old, f"{old}T10:00:00", "三个月前的事"),
        (new, f"{new}T10:00:00", "今天的事"),
    ])

    out = diary.export_markdown(path)

    assert "三个月前的事" in out
    assert "今天的事" in out


def test_export_flattens_multiline_content_to_one_line(tmp_path):
    path = _make_db(tmp_path, [
        ("2026-07-20", "2026-07-20T09:00:00", "第一行\n第二行"),
    ])

    out = diary.export_markdown(path)

    body = [ln for ln in out.splitlines() if ln.startswith("- [")]
    assert body == ["- [2026-07-20T09:00:00] 第一行\\n第二行"]


def test_export_returns_empty_when_db_missing(tmp_path):
    assert diary.export_markdown(str(tmp_path / "nope.db")) == ""


def test_export_returns_empty_when_table_not_created_yet(tmp_path):
    path = str(tmp_path / "diary.db")
    sqlite3.connect(path).close()

    assert diary.export_markdown(path) == ""


def test_export_returns_empty_when_table_has_no_rows(tmp_path):
    path = _make_db(tmp_path, [])

    assert diary.export_markdown(path) == ""


def test_export_opens_the_db_read_only(tmp_path):
    """导出跑在同步循环里，绝不能改动 diary.db。"""
    path = _make_db(tmp_path, [("2026-07-20", "2026-07-20T09:00:00", "x")])
    before = os.path.getsize(path)

    diary.export_markdown(path)

    rows = sqlite3.connect(path).execute("SELECT count(*) FROM diary").fetchone()[0]
    assert rows == 1
    assert os.path.getsize(path) == before


# -------------------- 挂进同步 --------------------

def _sync() -> GitHubSync:
    return GitHubSync(token="t", repo="owner/repo", branch="main", path_prefix="ombre")


def test_sync_file_set_includes_diary_export(tmp_path):
    (tmp_path / "dynamic").mkdir()
    (tmp_path / "dynamic" / "a.md").write_text("alpha", encoding="utf-8")
    _make_db(tmp_path, [("2026-07-20", "2026-07-20T09:00:00", "出差到周五")])

    gh = _sync()
    files = gh._collect_files(str(tmp_path))
    gh._attach_diary_export(files, str(tmp_path))

    assert set(files) == {"dynamic/a.md", _DIARY_EXPORT_FILENAME}
    assert "出差到周五" in files[_DIARY_EXPORT_FILENAME].decode("utf-8")

    manifest = gh._build_backup_manifest(files)
    assert manifest["file_count"] == 2
    assert _DIARY_EXPORT_FILENAME in {f["path"] for f in manifest["files"]}


def test_attach_is_a_noop_without_diary_db(tmp_path):
    gh = _sync()
    files: dict[str, bytes] = {}
    gh._attach_diary_export(files, str(tmp_path))

    assert files == {}


def test_stale_diary_md_on_disk_never_shadows_the_fresh_export(tmp_path):
    """import_from_github 会把 diary.md 拉回盘上。若 _collect_files 把它当普通
    桶收走，字典后写覆盖先写的顺序一变就可能用旧快照盖掉新快照。"""
    (tmp_path / "diary.md").write_text("# 旧快照\n- [2020-01-01T00:00:00] 过期内容", encoding="utf-8")
    _make_db(tmp_path, [("2026-07-20", "2026-07-20T09:00:00", "新内容")])

    gh = _sync()
    files = gh._collect_files(str(tmp_path))
    assert _DIARY_EXPORT_FILENAME not in files

    gh._attach_diary_export(files, str(tmp_path))
    text = files[_DIARY_EXPORT_FILENAME].decode("utf-8")
    assert "新内容" in text
    assert "过期内容" not in text


def test_export_failure_does_not_break_bucket_sync(tmp_path, monkeypatch):
    (tmp_path / "a.md").write_text("alpha", encoding="utf-8")

    def boom(_path):
        raise sqlite3.DatabaseError("file is not a database")

    monkeypatch.setattr(diary, "export_markdown", boom)

    gh = _sync()
    files = gh._collect_files(str(tmp_path))
    gh._attach_diary_export(files, str(tmp_path))

    assert files == {"a.md": b"alpha"}


@pytest.mark.asyncio
async def test_sync_uploads_diary_export_end_to_end(tmp_path, monkeypatch):
    (tmp_path / "a.md").write_text("alpha", encoding="utf-8")
    _make_db(tmp_path, [("2026-07-20", "2026-07-20T09:00:00", "出差到周五")])

    gh = _sync()
    captured: dict[str, bytes] = {}

    async def fake_commit(files):
        captured.update(files)
        return len(files)

    monkeypatch.setattr(gh, "_batch_commit", fake_commit)

    result = await gh.sync(str(tmp_path))

    assert result["ok"] is True
    assert result["uploaded"] == 2
    assert _DIARY_EXPORT_FILENAME in captured
    assert "出差到周五" in captured[_DIARY_EXPORT_FILENAME].decode("utf-8")


@pytest.mark.asyncio
async def test_diary_only_vault_still_syncs(tmp_path, monkeypatch):
    """一个桶都没有、只有 diary 的库不该被当成「无可同步文件」跳过。"""
    _make_db(tmp_path, [("2026-07-20", "2026-07-20T09:00:00", "出差到周五")])

    gh = _sync()

    async def fake_commit(files):
        return len(files)

    monkeypatch.setattr(gh, "_batch_commit", fake_commit)

    result = await gh.sync(str(tmp_path))

    assert result["ok"] is True
    assert result["uploaded"] == 1
