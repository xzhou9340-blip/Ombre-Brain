# ============================================================
# diary 分区单元测试
# diary 是独立 SQLite 表，不复用 buckets、不碰任何外部 API，
# 所以这里全部跑真实读写（临时目录），零 mock、零网络。
# 重点覆盖：
#   ① 写入：默认今天 / 显式 date / 同一天多条追加不覆盖 / 返回 id + date
#   ② 表结构：独立 diary 表 + date 索引，不写进 buckets 目录
#   ③ 读取：默认 3 天、日期正序、同日按写入顺序、按天分组、空日期跳过
#   ④ 边界：days 上限 7、下限 1、非法值回默认；窗口外的记录不返回
#   ⑤ 无记录返回「最近 N 天没有记录」而不是报错
#   ⑥ 非法日期给可读提示、空内容拒绝
#   ⑦ 依赖隔离：dehydrator / embedding / decay 一律不被调用
# ============================================================

import os
import sqlite3
from datetime import datetime, timedelta

import pytest

import tools._runtime as rt
from tools.diary import core as diary


def _d(offset_days: int) -> str:
    return (datetime.now() + timedelta(days=offset_days)).strftime("%Y-%m-%d")


@pytest.fixture
def diary_dir(tmp_path, monkeypatch):
    """把 tools._runtime.config 指向临时目录，diary.db 随之落在那里。"""
    buckets_dir = tmp_path / "buckets"
    buckets_dir.mkdir()
    monkeypatch.setattr(rt, "config", {"buckets_dir": str(buckets_dir)})
    monkeypatch.setattr(rt, "mark_op", None)
    return buckets_dir


# -------------------- 写入 --------------------

@pytest.mark.asyncio
async def test_write_defaults_to_today_and_returns_id_and_date(diary_dir):
    res = await diary.diary_write(content="出差到周五")

    assert _d(0) in res
    assert "#1" in res

    rows = sqlite3.connect(diary_dir / "diary.db").execute(
        "SELECT id, date, content FROM diary"
    ).fetchall()
    assert rows == [(1, _d(0), "出差到周五")]


@pytest.mark.asyncio
async def test_write_accepts_explicit_date(diary_dir):
    res = await diary.diary_write(content="胃疼两天", date=_d(-2))

    assert _d(-2) in res
    row = sqlite3.connect(diary_dir / "diary.db").execute(
        "SELECT date FROM diary"
    ).fetchone()
    assert row[0] == _d(-2)


@pytest.mark.asyncio
async def test_same_date_appends_instead_of_overwriting(diary_dir):
    await diary.diary_write(content="第一条", date=_d(0))
    await diary.diary_write(content="第二条", date=_d(0))
    await diary.diary_write(content="第三条", date=_d(0))

    rows = sqlite3.connect(diary_dir / "diary.db").execute(
        "SELECT content FROM diary ORDER BY id"
    ).fetchall()
    assert [r[0] for r in rows] == ["第一条", "第二条", "第三条"]


@pytest.mark.asyncio
async def test_write_records_created_at_separately_from_date(diary_dir):
    await diary.diary_write(content="补记昨天", date=_d(-1))

    date, created_at = sqlite3.connect(diary_dir / "diary.db").execute(
        "SELECT date, created_at FROM diary"
    ).fetchone()
    assert date == _d(-1)
    # created_at 是「实际写入时间」，跟归属日期无关，落在今天
    assert created_at.startswith(_d(0))


@pytest.mark.asyncio
async def test_write_rejects_empty_content(diary_dir):
    res = await diary.diary_write(content="   ")

    assert "需要内容" in res
    assert not (diary_dir / "diary.db").exists() or _count(diary_dir) == 0


@pytest.mark.asyncio
async def test_write_rejects_malformed_date(diary_dir):
    res = await diary.diary_write(content="x", date="2026/07/27")

    assert "YYYY-MM-DD" in res
    assert _count(diary_dir) == 0


def _count(diary_dir) -> int:
    db = diary_dir / "diary.db"
    if not db.exists():
        return 0
    return sqlite3.connect(db).execute("SELECT COUNT(*) FROM diary").fetchone()[0]


# -------------------- 表结构 --------------------

@pytest.mark.asyncio
async def test_uses_its_own_table_and_indexes_date(diary_dir):
    await diary.diary_write(content="建表")

    conn = sqlite3.connect(diary_dir / "diary.db")
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "diary" in tables
    assert "buckets" not in tables

    indexes = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='diary'"
    ).fetchall()}
    assert "idx_diary_date" in indexes

    cols = {r[1] for r in conn.execute("PRAGMA table_info(diary)").fetchall()}
    assert cols == {"id", "date", "content", "created_at"}


@pytest.mark.asyncio
async def test_does_not_touch_bucket_directories(diary_dir):
    await diary.diary_write(content="不进桶")

    assert os.listdir(diary_dir) == ["diary.db"]


# -------------------- 读取 --------------------

@pytest.mark.asyncio
async def test_read_groups_by_day_in_chronological_order(diary_dir):
    # 故意乱序写入，读取时必须按日期正序
    await diary.diary_write(content="今天赶工", date=_d(0))
    await diary.diary_write(content="前天出差", date=_d(-2))
    await diary.diary_write(content="今天还没和好", date=_d(0))

    res = await diary.diary_read(days=3)

    assert res.index(_d(-2)) < res.index(_d(0))
    # 同一天内按写入顺序
    assert res.index("今天赶工") < res.index("今天还没和好")
    assert "前天出差" in res


@pytest.mark.asyncio
async def test_read_skips_days_without_records(diary_dir):
    await diary.diary_write(content="只有前天", date=_d(-2))

    res = await diary.diary_read(days=3)

    assert _d(-2) in res
    assert _d(-1) not in res
    assert _d(0) not in res


@pytest.mark.asyncio
async def test_read_defaults_to_three_days(diary_dir):
    await diary.diary_write(content="窗口内", date=_d(-2))
    await diary.diary_write(content="窗口外", date=_d(-3))

    res = await diary.diary_read()

    assert "窗口内" in res
    assert "窗口外" not in res


@pytest.mark.asyncio
async def test_read_caps_days_at_seven(diary_dir):
    await diary.diary_write(content="第七天", date=_d(-6))
    await diary.diary_write(content="第八天", date=_d(-7))

    res = await diary.diary_read(days=30)

    assert "最近 7 天" in res
    assert "第七天" in res
    assert "第八天" not in res


@pytest.mark.asyncio
async def test_read_floors_days_at_one(diary_dir):
    await diary.diary_write(content="今天", date=_d(0))
    await diary.diary_write(content="昨天", date=_d(-1))

    res = await diary.diary_read(days=0)

    assert "今天" in res
    assert "昨天" not in res


@pytest.mark.asyncio
async def test_read_falls_back_to_default_on_garbage_days(diary_dir):
    await diary.diary_write(content="窗口内", date=_d(-1))

    res = await diary.diary_read(days="abc")  # type: ignore[arg-type]

    assert "最近 3 天" in res
    assert "窗口内" in res


@pytest.mark.asyncio
async def test_read_empty_is_not_an_error(diary_dir):
    res = await diary.diary_read(days=5)

    assert res == "最近 5 天没有记录。"


# -------------------- 依赖隔离 --------------------

@pytest.mark.asyncio
async def test_write_and_read_never_touch_llm_or_index(diary_dir, monkeypatch):
    """diary 必须在 siliconflow 429 挂掉时依然可用：
    dehydrator / embedding_engine / decay_engine 一旦被碰就炸。"""
    class _Boom:
        def __getattr__(self, name):
            raise AssertionError(f"diary 不该调用 {name}")

    monkeypatch.setattr(rt, "dehydrator", _Boom())
    monkeypatch.setattr(rt, "embedding_engine", _Boom())
    monkeypatch.setattr(rt, "decay_engine", _Boom())
    monkeypatch.setattr(rt, "bucket_mgr", _Boom())

    await diary.diary_write(content="429 也要能写")
    res = await diary.diary_read(days=1)

    assert "429 也要能写" in res
