# ============================================================
# 工具调用频次统计测试
#
# 这东西存在的唯一理由：砍工具之前得有真数据。所以重点不在功能多全，
# 而在两条硬约束：
#   ① record() 永不抛 —— 它挂在每次工具调用的路径上
#   ② 计数落盘 —— 部署一次进程就没了，内存计数攒不出「一周的数据」
# 另外钉住它只记名字和次数：这是「决定砍谁」的依据，不是行为审计。
# ============================================================

import sqlite3

import pytest

import tool_stats


@pytest.fixture
def stats_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(tool_stats, "_buckets_dir_override", str(tmp_path))
    return tmp_path


# -------------------- 计数 --------------------

def test_record_accumulates_per_tool_name(stats_dir):
    for _ in range(3):
        tool_stats.record("breath")
    tool_stats.record("diary_write")

    counts = {row["name"]: row["count"] for row in tool_stats.snapshot()}
    assert counts == {"breath": 3, "diary_write": 1}


def test_snapshot_is_sorted_by_count_desc(stats_dir):
    tool_stats.record("peek")
    for _ in range(5):
        tool_stats.record("breath")
    for _ in range(2):
        tool_stats.record("hold")

    assert [r["name"] for r in tool_stats.snapshot()] == ["breath", "hold", "peek"]


def test_first_seen_is_kept_while_last_seen_moves(stats_dir):
    tool_stats.record("breath")
    first = tool_stats.snapshot()[0]

    tool_stats.record("breath")
    second = tool_stats.snapshot()[0]

    assert second["first_seen"] == first["first_seen"]
    assert second["last_seen"] >= first["last_seen"]
    assert second["count"] == 2


def test_counts_survive_a_process_restart(stats_dir):
    """部署重启是常态。内存计数攒不出一周的数据，所以必须落盘。"""
    tool_stats.record("breath")
    tool_stats.record("breath")

    # 模拟新进程：模块状态与连接全部重来，只有盘上的库还在
    rows = sqlite3.connect(str(stats_dir / "tool_stats.db")).execute(
        "SELECT name, count FROM tool_calls"
    ).fetchall()
    assert rows == [("breath", 2)]


def test_snapshot_respects_limit(stats_dir):
    for name in ("a", "b", "c"):
        tool_stats.record(name)

    assert len(tool_stats.snapshot(limit=2)) == 2


# -------------------- 永不抛 --------------------

def test_record_never_raises_when_the_db_is_unusable(tmp_path, monkeypatch):
    """统计挂了绝不能让工具本身挂掉。"""
    monkeypatch.setattr(tool_stats, "_buckets_dir_override", str(tmp_path))
    broken = tmp_path / "tool_stats.db"
    broken.write_text("this is not a database", encoding="utf-8")

    assert tool_stats.record("breath") is False
    assert tool_stats.snapshot() == []


def test_record_never_raises_without_a_buckets_dir(monkeypatch):
    monkeypatch.setattr(tool_stats, "_buckets_dir_override", "")
    monkeypatch.delenv("OMBRE_VAULT_DIR", raising=False)
    monkeypatch.delenv("OMBRE_BUCKETS_DIR", raising=False)

    assert tool_stats.record("breath") is False


def test_empty_name_is_ignored(stats_dir):
    assert tool_stats.record("") is False
    assert tool_stats.record("   ") is False
    assert tool_stats.snapshot() == []


def test_overlong_name_is_truncated_not_rejected(stats_dir):
    tool_stats.record("x" * 500)

    rows = tool_stats.snapshot()
    assert len(rows) == 1
    assert len(rows[0]["name"]) == tool_stats._MAX_NAME_LEN


# -------------------- 边界：只记名字和次数 --------------------

def test_table_stores_nothing_but_name_and_counters(stats_dir):
    """不记参数、不记内容——这是「决定砍谁」的依据，不是行为审计。"""
    tool_stats.record("breath")

    cols = {
        row[1]
        for row in sqlite3.connect(str(stats_dir / "tool_stats.db")).execute(
            "PRAGMA table_info(tool_calls)"
        )
    }
    assert cols == {"name", "count", "first_seen", "last_seen"}


def test_mark_op_feeds_the_counter(stats_dir, monkeypatch):
    """埋点早就铺好了：每个工具都把名字传进 _mark_op，此前被丢掉。"""
    from web import _shared as sh

    sh._mark_op("breath")
    sh._mark_op("breath")
    sh._mark_op("hold")

    counts = {row["name"]: row["count"] for row in tool_stats.snapshot()}
    assert counts == {"breath": 2, "hold": 1}


def test_mark_op_still_updates_the_heartbeat_timestamp(stats_dir):
    """统计是顺带的，不能挤掉它原本的职责。"""
    from web import _shared as sh

    before = sh._LAST_OP_TS
    sh._mark_op("breath")

    assert sh._LAST_OP_TS >= before
