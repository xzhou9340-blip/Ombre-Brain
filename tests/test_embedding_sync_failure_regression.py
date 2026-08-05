"""
========================================
test_embedding_sync_failure_regression.py — 「存得下却搜不出来」回归
========================================

背景：generate_and_store 的失败约定是「返回 False」而不是抛异常。
bucket_manager._sync_embedding 早期丢掉了这个返回值，于是向量化限流/超时时：

- create() 里写好的 markdown 不会被回滚，桶带着「无向量」状态永久存在；
- update(content=...) 的向量停留在旧正文上；
- decay 的自愈补向量无条件计数，日志照报「自愈 N 条」，缺失数一轮不降。

结果就是桶在 pulse 列表里看得见、breath(query=...) 语义召回里永远搜不到。
本文件锁死修复后的行为。
========================================
"""

import logging
import os

import pytest


class FailingEmbeddingEngine:
    """向量化服务挂掉的替身：generate_and_store 永远返回 False（不抛）。"""

    enabled = True

    def __init__(self):
        self.calls: list[str] = []

    async def generate_and_store(self, bucket_id: str, content: str) -> bool:
        self.calls.append(bucket_id)
        return False

    def delete_embedding(self, bucket_id: str) -> None:
        return None

    async def get_embedding(self, bucket_id: str) -> list[float] | None:
        return None

    async def search_similar(self, query: str, top_k: int = 10):
        return []

    def list_all_ids(self) -> list[str]:
        return []


@pytest.mark.asyncio
async def test_create_rejects_and_rolls_back_when_embedding_cannot_be_stored(test_config):
    """向量存不下 → create 抛 EmbeddingSyncError，且不留下无向量的 markdown。"""
    from bucket_manager import BucketManager, EmbeddingSyncError

    engine = FailingEmbeddingEngine()
    mgr = BucketManager(test_config, embedding_engine=engine)

    with pytest.raises(EmbeddingSyncError):
        await mgr.create("这条记忆的向量算不出来，不该被落盘")

    assert engine.calls, "应该真的尝试过写向量"

    # 整库扫一遍：不能有任何残留的桶文件
    leftovers = []
    for root, _dirs, files in os.walk(test_config["buckets_dir"]):
        leftovers += [f for f in files if f.endswith(".md")]
    assert leftovers == [], f"失败的 create 不该留下孤儿桶文件: {leftovers}"


@pytest.mark.asyncio
async def test_create_succeeds_normally_when_embedding_works(bucket_mgr):
    """对照组：向量正常时 create 照旧成功，修复没有误伤主路径。"""
    bucket_id = await bucket_mgr.create("正常记忆")
    assert bucket_id
    assert await bucket_mgr.get(bucket_id) is not None


@pytest.mark.asyncio
async def test_update_content_surfaces_embedding_failure(test_config, fake_embedding_engine):
    """update(content=...) 向量刷新失败必须抛，不能静默留下旧向量。"""
    from bucket_manager import BucketManager, EmbeddingSyncError

    mgr = BucketManager(test_config, embedding_engine=fake_embedding_engine)
    bucket_id = await mgr.create("原始正文")

    failing = FailingEmbeddingEngine()
    mgr.embedding_engine = failing

    with pytest.raises(EmbeddingSyncError):
        await mgr.update(bucket_id, content="替换后的正文")

    # 正文已落盘（update 不回滚），异常只是让调用方知道向量是旧的
    bucket = await mgr.get(bucket_id)
    assert bucket is not None
    assert "替换后的正文" in bucket["content"]


@pytest.mark.asyncio
async def test_trace_degrades_with_visible_warning_instead_of_raising(
    test_config, fake_embedding_engine, monkeypatch
):
    """trace 把 EmbeddingSyncError 翻译成可见的降级提示，而不是抛给 MCP 客户端。"""
    from bucket_manager import BucketManager
    from tools import _runtime as rt
    from tools.trace.core import trace_core

    mgr = BucketManager(test_config, embedding_engine=fake_embedding_engine)
    bucket_id = await mgr.create("原始正文")

    failing = FailingEmbeddingEngine()
    mgr.embedding_engine = failing

    monkeypatch.setattr(rt, "config", test_config, raising=False)
    monkeypatch.setattr(rt, "bucket_mgr", mgr, raising=False)
    monkeypatch.setattr(rt, "embedding_engine", failing, raising=False)
    monkeypatch.setattr(rt, "logger", logging.getLogger("test.trace"), raising=False)
    monkeypatch.setattr(rt, "mark_op", None, raising=False)

    result = await trace_core(bucket_id, content="替换后的正文")

    assert "embedding 重建失败" in result
    assert "修改失败" not in result


@pytest.mark.asyncio
async def test_generate_and_store_retries_transient_failures(tmp_path, monkeypatch):
    """写路径现在是 fail-fast 的，一次限流不该直接把 hold 顶掉：允许有限重试。"""
    import embedding_engine as ee_mod
    from embedding_engine import EmbeddingEngine

    monkeypatch.setattr(ee_mod, "_STORE_RETRY_BASE_DELAY", 0.0)

    buckets_dir = tmp_path / "buckets"
    os.makedirs(buckets_dir, exist_ok=True)
    engine = EmbeddingEngine(
        {
            "buckets_dir": str(buckets_dir),
            "embedding": {
                "enabled": True,
                "api_key": "test-key",
                "api_format": "openai_compat",
                "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
                "model": "gemini-embedding-001",
            },
        }
    )

    attempts = {"n": 0}

    async def flaky(_text: str) -> list[float]:
        attempts["n"] += 1
        # 前两次模拟限流（后端约定：失败返回空列表），第三次才拿到向量
        return [0.1, 0.2, 0.3] if attempts["n"] >= 3 else []

    monkeypatch.setattr(engine, "_generate_async", flaky)

    assert await engine.generate_and_store("bucketretry01", "内容") is True
    assert attempts["n"] == 3
    assert await engine.get_embedding("bucketretry01") == [0.1, 0.2, 0.3]


@pytest.mark.asyncio
async def test_generate_and_store_gives_up_after_retry_budget(tmp_path, monkeypatch):
    """重试用尽仍拿不到向量 → 返回 False，由调用方决定拒绝还是降级。"""
    import embedding_engine as ee_mod
    from embedding_engine import EmbeddingEngine

    monkeypatch.setattr(ee_mod, "_STORE_RETRY_BASE_DELAY", 0.0)

    buckets_dir = tmp_path / "buckets"
    os.makedirs(buckets_dir, exist_ok=True)
    engine = EmbeddingEngine(
        {
            "buckets_dir": str(buckets_dir),
            "embedding": {
                "enabled": True,
                "api_key": "test-key",
                "api_format": "openai_compat",
                "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
                "model": "gemini-embedding-001",
            },
        }
    )

    attempts = {"n": 0}

    async def always_empty(_text: str) -> list[float]:
        attempts["n"] += 1
        return []

    monkeypatch.setattr(engine, "_generate_async", always_empty)

    assert await engine.generate_and_store("bucketretry02", "内容") is False
    assert attempts["n"] == ee_mod._STORE_RETRY_ATTEMPTS


@pytest.mark.asyncio
async def test_self_heal_counts_only_real_successes(test_config):
    """自愈补向量：generate_and_store 返回 False 不该被算成「补上了」。"""
    from bucket_manager import BucketManager
    from decay_engine import DecayEngine

    failing = FailingEmbeddingEngine()
    mgr = BucketManager(test_config, embedding_engine=failing)
    engine = DecayEngine(test_config, mgr)

    buckets = [{"id": "aaaaaaaaaaaa", "content": "缺向量的桶"}]
    healed = await engine._self_heal_embeddings(buckets)

    assert healed == 0, "向量化全挂时不能报告自愈成功，否则告警信号被盖住"
