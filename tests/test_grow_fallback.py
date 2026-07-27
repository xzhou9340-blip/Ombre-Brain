# ============================================================
# grow 兜底测试（任务书 §1.2）
#
# 2026-07-27 实测：siliconflow 429 → dehydrator 抛异常 → grow 整条
# RuntimeError，桶未创建、内容全丢。这里钉住修复后的行为：
#   ① 长内容 digest 挂掉 → 原文整存，不拆桶，返回值说明降级
#   ② 短内容 analyze 挂掉 → 同样兜底（文档只提了 core，shortpath 也有这个洞）
#   ③ 降级路径全程不碰 LLM：dehydrator 被碰就断言失败
#   ④ 429 与「key 没配」文案区分（证据 #1：原文案误导）
#   ⑤ 超过单桶上限的原文按字节机械切分，一个字不丢
#   ⑥ 落桶也失败时（embedding 挂）把原文原样回吐，不静默吞
#   ⑦ 正常路径不受影响
# 全部真实落盘到 tmp_path，LLM 与 embedding 用替身，不发网络请求。
# ============================================================

import os
import re

import pytest

import tools._runtime as rt
from tools.grow import dispatch as grow_dispatch
from tools.grow.fallback import UNDIGESTED_PREFIX, UNDIGESTED_TAG, _split_by_bytes


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


class _BoomDehydrator:
    """任何属性访问都炸——用来证明降级路径没碰 LLM。"""

    def __getattr__(self, name):
        raise AssertionError(f"降级路径不该调用 dehydrator.{name}")


class _FailingDehydrator:
    """digest / analyze 按指定错误抛，模拟限流或 key 失效。"""

    def __init__(self, err):
        self.err = err

    async def digest(self, content):
        raise RuntimeError(self.err)

    async def analyze(self, content):
        raise RuntimeError(self.err)


class _OkDehydrator:
    async def digest(self, content):
        return [{
            "name": "正常拆出来的一条", "content": content,
            "domain": ["日常"], "valence": 0.5, "arousal": 0.3,
            "tags": ["测试"], "importance": 5,
        }]

    async def analyze(self, content):
        return {
            "domain": ["日常"], "valence": 0.5, "arousal": 0.3,
            "tags": ["测试"], "importance": 5, "suggested_name": "短条",
        }


class _NoopDecay:
    async def ensure_started(self):
        return None


@pytest.fixture
def wired(test_config, tmp_path, monkeypatch):
    """把 tools._runtime 接到真实 BucketManager + 假 embedding 上。"""
    import logging
    from bucket_manager import BucketManager

    bd = str(tmp_path / "buckets")
    for d in ["permanent", "dynamic", "archive"]:
        os.makedirs(os.path.join(bd, d), exist_ok=True)
    cfg = test_config | {"buckets_dir": bd}

    emb = _FakeEmbedding()
    bm = BucketManager(cfg, embedding_engine=emb)

    monkeypatch.setattr(rt, "config", cfg)
    monkeypatch.setattr(rt, "bucket_mgr", bm)
    monkeypatch.setattr(rt, "embedding_engine", emb)
    monkeypatch.setattr(rt, "decay_engine", _NoopDecay())
    monkeypatch.setattr(rt, "logger", logging.getLogger("test_grow_fallback"))
    monkeypatch.setattr(rt, "mark_op", None)
    return bm, emb, cfg


_LONG = "今天出差到吉隆坡，客户改了三次规格单，晚上还要赶标书。胃有点不舒服，明天继续。" * 2


async def _undigested(bm):
    buckets = await bm.list_all(include_archive=True)
    return [b for b in buckets if UNDIGESTED_TAG in (b["metadata"].get("tags") or [])]


# -------------------- ① 长内容降级 --------------------

@pytest.mark.asyncio
async def test_long_content_falls_back_instead_of_losing_everything(wired):
    bm, _emb, _cfg = wired
    rt.dehydrator = _FailingDehydrator("429 Too Many Requests")

    res = await grow_dispatch(_LONG)

    assert "降级" in res
    kept = await _undigested(bm)
    assert len(kept) == 1
    # 原文一字不改地存下来了
    assert kept[0]["content"].strip() == _LONG.strip()
    # create() 会前置自己的时间戳并 sanitize，所以只能断言「名字里带得上这个词」
    assert UNDIGESTED_PREFIX in kept[0]["metadata"]["name"]


# -------------------- ② 短内容也要兜住 --------------------

@pytest.mark.asyncio
async def test_short_content_path_also_falls_back(wired):
    bm, _emb, _cfg = wired
    rt.dehydrator = _FailingDehydrator("429 rate limit exceeded")

    short = "胃疼两天了"  # < 30 字 → 走 shortpath
    res = await grow_dispatch(short)

    assert "降级" in res
    kept = await _undigested(bm)
    assert len(kept) == 1
    assert kept[0]["content"].strip() == short


# -------------------- ③ 降级路径不碰 LLM --------------------

@pytest.mark.asyncio
async def test_fallback_never_touches_the_llm(wired):
    """digest 挂掉之后，后续步骤一次都不许再调 dehydrator。"""
    bm, _emb, _cfg = wired

    class _OneShotBoom:
        def __init__(self):
            self.used = False

        async def digest(self, content):
            self.used = True
            raise RuntimeError("429")

        def __getattr__(self, name):
            raise AssertionError(f"降级路径不该调用 dehydrator.{name}")

    rt.dehydrator = _OneShotBoom()
    res = await grow_dispatch(_LONG)

    assert "降级" in res
    assert len(await _undigested(bm)) == 1


@pytest.mark.asyncio
async def test_fallback_module_is_llm_free(wired):
    """直接调 grow_fallback 时 dehydrator 整个是炸弹，依然能落桶。"""
    from tools.grow.fallback import grow_fallback

    bm, _emb, _cfg = wired
    rt.dehydrator = _BoomDehydrator()

    res = await grow_fallback("接口全挂时也要存下来", reason="429")

    assert "降级" in res
    assert len(await _undigested(bm)) == 1


# -------------------- ④ 429 与 key 未配置的文案区分 --------------------

@pytest.mark.asyncio
async def test_rate_limit_is_not_reported_as_missing_key(wired):
    """证据 #1：原文案写「API key 未配置」，实为限流，属误导。"""
    _bm, _emb, _cfg = wired
    rt.dehydrator = _FailingDehydrator("Error code: 429 - Too Many Requests")

    res = await grow_dispatch(_LONG)

    assert "限流" in res and "429" in res
    assert "key 没配" not in res.replace("不是 key 没配", "")


@pytest.mark.asyncio
async def test_non_rate_limit_error_keeps_generic_wording(wired):
    _bm, _emb, _cfg = wired
    rt.dehydrator = _FailingDehydrator("Connection reset by peer")

    res = await grow_dispatch(_LONG)

    assert "调用失败" in res
    assert "限流" not in res


# -------------------- ⑤ 超上限机械切分 --------------------

def test_split_by_bytes_never_breaks_a_character():
    text = "中文字符" * 100
    chunks = _split_by_bytes(text, 30)

    assert len(chunks) > 1
    assert "".join(chunks) == text          # 一个字不丢
    for c in chunks:
        assert len(c.encode("utf-8")) <= 30  # 每段都在上限内


def test_split_by_bytes_passes_through_when_under_cap():
    assert _split_by_bytes("短", 1024) == ["短"]
    assert _split_by_bytes("不限制", 0) == ["不限制"]


@pytest.mark.asyncio
async def test_oversized_content_is_split_into_multiple_buckets(wired):
    bm, _emb, cfg = wired
    cfg["limits"] = {"max_bucket_bytes": 120}
    rt.dehydrator = _FailingDehydrator("429")

    big = "出差赶工胃疼没和好" * 60
    res = await grow_dispatch(big)

    kept = await _undigested(bm)
    assert len(kept) > 1
    # 按返回值里 📝 的顺序拼回来（那就是写入顺序），必须与原文逐字一致
    order = re.findall(r"📝(\S+)", res)
    assert len(order) == len(kept)
    by_id = {b["id"]: b["content"] for b in kept}
    assert "".join(by_id[bid] for bid in order).strip() == big.strip()
    assert f"新建 {len(kept)} 条" in res


# -------------------- ⑥ 落桶也失败时不静默吞 --------------------

@pytest.mark.asyncio
async def test_when_even_the_bucket_write_fails_the_original_text_comes_back(wired, monkeypatch):
    """embedding 也挂了 → create() 删文件并抛 → 至少把原文还给调用方。"""
    from bucket_manager import BucketManager

    bm, _emb, cfg = wired
    broken = BucketManager(cfg, embedding_engine=_FakeEmbedding(fail=True))
    monkeypatch.setattr(rt, "bucket_mgr", broken)
    rt.dehydrator = _FailingDehydrator("429")

    res = await grow_dispatch(_LONG)

    assert "没有存下来" in res
    assert _LONG.strip() in res           # 原文原样回吐
    assert len(await _undigested(bm)) == 0


# -------------------- ⑦ 正常路径不受影响 --------------------

@pytest.mark.asyncio
async def test_healthy_path_is_untouched(wired):
    bm, _emb, _cfg = wired
    rt.dehydrator = _OkDehydrator()

    res = await grow_dispatch(_LONG)

    assert "降级" not in res
    assert len(await _undigested(bm)) == 0


@pytest.mark.asyncio
async def test_healthy_short_path_is_untouched(wired):
    bm, _emb, _cfg = wired
    rt.dehydrator = _OkDehydrator()

    res = await grow_dispatch("短内容正常走打标")

    assert "降级" not in res
    assert len(await _undigested(bm)) == 0
