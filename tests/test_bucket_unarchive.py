# ============================================================
# 归档还原（bucket_mgr.unarchive）
#
# 归档一直是单向的：type 被覆写成 "archived"、文件搬进 archive/，
# 然后没有回头路。可归档并不只是衰减引擎在做——人也会手工归档「滞后的、
# 重复的」内容，手工操作就一定会有手误。
#
# 覆盖：
#   ① 还原后文件真的搬回活跃区，list_all(include_archive=False) 能看见
#   ② type 还原成归档前那个，不是一律 dynamic
#   ③ 历史归档的桶（没有 archived_from_type 字段）按 dynamic 还原，不报错
#   ④ 还原一个本来就不在归档区的桶 → 返回 False，不是假装成功
#   ⑤ 重复归档不会把 "archived" 记成原始类型
# ============================================================

import os

import pytest

from bucket_manager import BucketManager


class _FakeEmbeddingEngine:
    """永远成功的假引擎。embedding 是 create()/update(content=...) 的强制依赖，
    但本文件测的是归档还原时的文件搬移与 type 还原，与向量无关。
    （仓库惯例是各测试文件本地定义，不跨文件 import conftest。）"""

    enabled = True

    async def generate_and_store(self, bucket_id, content):
        return True

    async def delete_embedding(self, bucket_id):
        return True

    async def get_embedding(self, bucket_id):
        return [0.0]

    async def search_similar(self, query, top_k=10):
        return []


@pytest.fixture
def mgr(test_config):
    return BucketManager(test_config, embedding_engine=_FakeEmbeddingEngine())


async def _make(mgr, content="一条记忆"):
    return await mgr.create(content=content)


def _active_ids(buckets):
    return {b["id"] for b in buckets}


@pytest.mark.asyncio
async def test_archived_bucket_comes_back_to_the_active_area(mgr):
    bid = await _make(mgr)
    assert await mgr.archive(bid) is True
    assert bid not in _active_ids(await mgr.list_all(include_archive=False))

    assert await mgr.unarchive(bid) is True

    assert bid in _active_ids(await mgr.list_all(include_archive=False))


@pytest.mark.asyncio
async def test_type_is_restored_to_what_it_was(mgr):
    bid = await _make(mgr)
    await mgr.update(bid, type="permanent")
    await mgr.archive(bid)

    archived = await mgr.get(bid)
    assert archived["metadata"]["type"] == "archived"

    await mgr.unarchive(bid)

    restored = await mgr.get(bid)
    assert restored["metadata"]["type"] == "permanent"
    # 还原后不该留下归档时的记账字段
    assert "archived_from_type" not in restored["metadata"]


@pytest.mark.asyncio
async def test_legacy_archived_bucket_without_the_marker_restores_as_dynamic(mgr, tmp_path):
    """线上已有 33 条归档桶，它们归档时还没有 archived_from_type 这个字段。"""
    import frontmatter

    bid = await _make(mgr)
    await mgr.archive(bid)

    path = mgr._find_bucket_file(bid)
    post = frontmatter.load(path)
    del post["archived_from_type"]          # 模拟历史数据
    with open(path, "w", encoding="utf-8") as f:
        f.write(frontmatter.dumps(post))

    assert await mgr.unarchive(bid) is True
    assert (await mgr.get(bid))["metadata"]["type"] == "dynamic"


@pytest.mark.asyncio
async def test_unarchive_of_an_active_bucket_reports_failure(mgr):
    """不在归档区的桶没什么可还原的，别让调用方以为成功了。"""
    bid = await _make(mgr)

    assert await mgr.unarchive(bid) is False


@pytest.mark.asyncio
async def test_unarchive_of_a_missing_id_is_false(mgr):
    assert await mgr.unarchive("no-such-bucket") is False


@pytest.mark.asyncio
async def test_archiving_twice_keeps_the_original_type(mgr):
    bid = await _make(mgr)
    await mgr.archive(bid)
    # 第二次归档（已在归档区）不该把 archived 记成「原始类型」
    await mgr.archive(bid)

    await mgr.unarchive(bid)

    assert (await mgr.get(bid))["metadata"]["type"] != "archived"


@pytest.mark.asyncio
async def test_file_actually_moves_out_of_the_archive_dir(mgr):
    bid = await _make(mgr)
    await mgr.archive(bid)
    assert os.path.abspath(mgr._find_bucket_file(bid)).startswith(
        os.path.abspath(mgr.archive_dir) + os.sep
    )

    await mgr.unarchive(bid)

    assert not os.path.abspath(mgr._find_bucket_file(bid)).startswith(
        os.path.abspath(mgr.archive_dir) + os.sep
    )
