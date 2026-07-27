# ============================================================
# 待处理区测试（任务书 §1.4）
#
# 场景：compress 与 embed 同源（siliconflow），一次 429 两条都挂 →
# bucket_manager.create() 在 _sync_embedding 失败时删文件并抛 →
# grow 的降级兜底也建不出桶。待处理区是这个场景的最后落点。
#
#   ① 表结构：独立 pending.db，字段含原文/来源工具/失败原因/时间戳/状态
#   ② record() 绝不抛异常——调用方正在救火
#   ③ 全程不碰 LLM / 不碰 bucket_mgr / 不建向量
#   ④ grow 端到端：双挂时内容进待处理区，返回值说明去向
#   ⑤ 待处理区也写不进去时，原文原样回吐
#   ⑥ PENDING_MIGRATE 历史标记能被认出来（ea47fc1b4ee5 那条不能漏）
#   ⑦ 只做写入：本模块不提供任何补建桶 / 对账入口
# ============================================================

import os
import sqlite3

import pytest

import pending_store
import tools._runtime as rt
from tools.grow import dispatch as grow_dispatch
from tools.grow.fallback import UNDIGESTED_TAG


class _FakeEmbedding:
    enabled = True

    def __init__(self, fail=False):
        self.fail = fail

    async def generate_and_store(self, bucket_id, content):
        if self.fail:
            raise RuntimeError("embedding 429 Too Many Requests")
        return True

    def delete_embedding(self, bucket_id):
        pass

    def list_all_ids(self):
        return []


class _FailingDehydrator:
    async def digest(self, content):
        raise RuntimeError("429 Too Many Requests")

    async def analyze(self, content):
        raise RuntimeError("429 Too Many Requests")


class _NoopDecay:
    async def ensure_started(self):
        return None


@pytest.fixture
def bd(tmp_path):
    d = str(tmp_path / "buckets")
    os.makedirs(d, exist_ok=True)
    return d


@pytest.fixture
def wired_broken(test_config, tmp_path, monkeypatch):
    """compress 与 embedding 双挂：桶建不出来，只剩待处理区。"""
    import logging
    from bucket_manager import BucketManager

    d = str(tmp_path / "buckets")
    for sub in ["permanent", "dynamic", "archive"]:
        os.makedirs(os.path.join(d, sub), exist_ok=True)
    cfg = test_config | {"buckets_dir": d}

    bm = BucketManager(cfg, embedding_engine=_FakeEmbedding(fail=True))
    monkeypatch.setattr(rt, "config", cfg)
    monkeypatch.setattr(rt, "bucket_mgr", bm)
    monkeypatch.setattr(rt, "decay_engine", _NoopDecay())
    monkeypatch.setattr(rt, "logger", logging.getLogger("test_pending"))
    monkeypatch.setattr(rt, "mark_op", None)
    rt.dehydrator = _FailingDehydrator()
    return bm, cfg, d


# -------------------- ① 表结构 --------------------

def test_schema_has_the_required_columns(bd):
    pending_store.record(bd, "原文", source_tool="grow", reason="429")

    conn = sqlite3.connect(pending_store.db_path(bd))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(pending_writes)")}
    # 任务书要求：原文 / 来源工具 / 失败原因 / 时间戳 / 状态
    assert {"content", "source_tool", "reason", "created_at", "status"} <= cols
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "pending_writes" in tables
    assert "buckets" not in tables and "diary" not in tables  # 独立库，不混用


def test_record_roundtrip(bd):
    pid = pending_store.record(bd, "出差到周五没存进去", source_tool="grow", reason="429 限流")

    assert pid == 1
    rows = pending_store.list_pending(bd)
    assert len(rows) == 1
    assert rows[0]["content"] == "出差到周五没存进去"
    assert rows[0]["source_tool"] == "grow"
    assert "429" in rows[0]["reason"]
    assert rows[0]["status"] == "pending"
    assert rows[0]["created_at"]


def test_records_keep_write_order(bd):
    for i in range(3):
        pending_store.record(bd, f"第{i}条", source_tool="grow")

    assert [r["content"] for r in pending_store.list_pending(bd)] == ["第0条", "第1条", "第2条"]
    assert pending_store.count_pending(bd) == 3


# -------------------- ② record 绝不抛 --------------------

def test_record_never_raises_on_bad_path():
    """路径不可写时返回 0，不抛——调用方此刻正在救火。"""
    assert pending_store.record("/proc/nonexistent/nope", "内容", source_tool="grow") == 0


def test_record_ignores_empty_content(bd):
    assert pending_store.record(bd, "   ", source_tool="grow") == 0
    assert pending_store.count_pending(bd) == 0


def test_list_and_count_never_raise_on_bad_path():
    assert pending_store.list_pending("/proc/nonexistent/nope") == []
    assert pending_store.count_pending("/proc/nonexistent/nope") == 0


# -------------------- ③ 不碰 LLM / 不碰桶 --------------------

def test_module_imports_nothing_from_llm_or_bucket_layer():
    src = open("src/pending_store.py", encoding="utf-8").read()
    for forbidden in ("dehydrator", "embedding_engine", "bucket_manager", "import bucket"):
        assert f"import {forbidden}" not in src, f"pending_store 不该 import {forbidden}"


def test_record_works_with_every_runtime_dependency_exploded(bd, monkeypatch):
    class _Boom:
        def __getattr__(self, name):
            raise AssertionError(f"待处理区不该碰 {name}")

    monkeypatch.setattr(rt, "dehydrator", _Boom())
    monkeypatch.setattr(rt, "embedding_engine", _Boom())
    monkeypatch.setattr(rt, "bucket_mgr", _Boom())

    assert pending_store.record(bd, "什么都挂了也要写进去", source_tool="grow") == 1


# -------------------- ④ grow 端到端 --------------------

@pytest.mark.asyncio
async def test_grow_double_outage_lands_in_pending(wired_broken):
    bm, cfg, d = wired_broken
    text = "今天出差到吉隆坡，客户改了三次规格单，晚上还要赶标书，胃有点不舒服。" * 2

    res = await grow_dispatch(text)

    assert "待处理区" in res
    rows = pending_store.list_pending(d)
    assert len(rows) == 1
    assert rows[0]["content"].strip() == text.strip()
    assert rows[0]["source_tool"] == "grow"
    # 桶确实一个都没建出来
    buckets = await bm.list_all(include_archive=True)
    assert [b for b in buckets if UNDIGESTED_TAG in (b["metadata"].get("tags") or [])] == []


@pytest.mark.asyncio
async def test_grow_double_outage_short_path_lands_in_pending(wired_broken):
    _bm, _cfg, d = wired_broken

    res = await grow_dispatch("胃疼两天了")

    assert "待处理区" in res
    assert pending_store.count_pending(d) == 1


@pytest.mark.asyncio
async def test_pending_is_invisible_to_normal_retrieval(wired_broken):
    """待处理区不是桶：list_all 看不到它，breath/dream 自然也搜不到。"""
    bm, _cfg, d = wired_broken

    await grow_dispatch("这条只在待处理区里" * 5)

    assert pending_store.count_pending(d) == 1
    assert await bm.list_all(include_archive=True) == []


# -------------------- ⑤ 最后一道防线 --------------------

@pytest.mark.asyncio
async def test_original_text_is_returned_when_even_pending_fails(wired_broken, monkeypatch):
    _bm, cfg, _d = wired_broken
    text = "待处理区也写不进去的时候" * 3
    monkeypatch.setattr(pending_store, "record", lambda *a, **k: 0)

    res = await grow_dispatch(text)

    assert "没有存下来" in res
    assert text.strip() in res


# -------------------- ⑥ 历史 PENDING_MIGRATE 标记 --------------------

def test_legacy_marker_recognises_the_known_historical_letter():
    """letter ea47fc1b4ee5 的实际标题（取自线上 pulse），对账时不能漏掉。"""
    title = "2026-07-27 03-22-26 降级存档 PENDING_MIGRATE20260723 小雪的 ombre-brain 用户报告"

    assert pending_store.is_legacy_pending_title(title)


@pytest.mark.parametrize("title", [
    "降级存档 PENDING_MIGRATE_20260723 报告",   # 带下划线的原始写法
    "PENDING_MIGRATE",                          # 只有标记
    "前缀 PENDING_MIGRATE 后缀",                 # 夹在中间
])
def test_legacy_marker_is_substring_not_exact_match(title):
    assert pending_store.is_legacy_pending_title(title)


@pytest.mark.parametrize("title", ["", "普通信件标题", "PENDING", "MIGRATE"])
def test_legacy_marker_does_not_over_match(title):
    assert not pending_store.is_legacy_pending_title(title)


# -------------------- ⑦ 本次只做写入 --------------------

def test_module_exposes_no_reconciliation_entrypoint():
    """对账补建留给阶段二，与 §2.1 的 letter 回迁合并成同一个函数。
    这里如果冒出补建入口，说明写了两套。"""
    # 只看本模块定义的函数：STATUS_MIGRATED 这类常量是给阶段二回填用的，
    # 不是入口，不该被误判。
    funcs = [
        n for n in dir(pending_store)
        if not n.startswith("_")
        and callable(getattr(pending_store, n))
        and getattr(getattr(pending_store, n), "__module__", "") == "pending_store"
    ]
    for banned in ("migrate", "reconcile", "rebuild", "flush", "retry"):
        hits = [n for n in funcs if banned in n.lower()]
        assert not hits, f"不该有 {banned} 类入口: {hits}"
