"""
========================================
tools/grow/fallback.py — grow 的 LLM 降级兜底
========================================

2026-07-27 实测：siliconflow 返回 429 后 dehydrator.digest() 抛异常，
grow 整条链路 RuntimeError，桶未创建、内容全丢。grow 的两条分支
（core 的 digest / shortpath 的 analyze）都有这个问题。

这里是它们共用的兜底：拆不了就不拆，原文照存。宁可粗糙也不能丢。

关键行为：
- 全程不碰任何 LLM 接口：不调 dehydrator、不调 merge_or_create
  （后者会走 search→可能合并进别的桶，把原文冲散、也让事后重整更难找）
- 直接 bucket_mgr.create 落桶，元数据用固定默认值（未分类 / V0.5 / A0.3 /
  importance 5），因为「猜标签」正是刚刚挂掉的那个能力
- 原文超过单桶上限时按字节机械切分成多条，各自独立落桶，一个字不丢
- 打内部标签 __undigested__（唯一可靠的标记，桶名会被 create 重写），
  桶名里带「未拆桶」只为人眼可读；事后按标签检索出来重新 grow
- 返回值明确说明走了降级、为什么降级、落了哪些桶、下一步该做什么
- 连落桶都失败时（例如 embedding 也挂了，create() 会删文件并抛），
  转存待处理区 pending_store（§1.4，纯本地 SQLite、不要向量、不碰 LLM），
  等接口恢复后由阶段二的对账函数补建成桶
- 待处理区也写不进去时（盘满 / 路径不可写）才把原文原样回吐给调用方——
  最后一道防线是「别静默吞掉」

不做什么（边界）：
- 不重试、不排队、不落地待办：grow 是一次性调用，重试与否由调用方决定
- 不做 plan 闭环判断 / 疑似重复扫描：那两条都要 LLM，此刻正挂着
- 不绕过 bucket_mgr 的 embedding 硬校验：那是 bucket_manager 明确写死的
  设计决定（不允许「文件在但向量缺」），要改得单独决策，不在这里偷偷破例

对外暴露：grow_fallback(content, reason) → str
========================================
"""

import uuid

import pending_store

from .. import _runtime as rt
from .._common import max_bucket_bytes


def _buckets_dir() -> str:
    """待处理区与桶同盘同目录（持久盘）。取不到就回退环境变量。"""
    base = ""
    if rt.config:
        base = str(rt.config.get("buckets_dir") or "")
    if not base:
        import os
        base = os.environ.get("OMBRE_VAULT_DIR") or os.environ.get("OMBRE_BUCKETS_DIR") or ""
    return base


def _to_pending(chunks: list, reason: str) -> list:
    """落桶失败的段落进待处理区，返回写成功的 id 列表。"""
    base = _buckets_dir()
    if not base:
        rt.logger.error("[grow fallback] 找不到 buckets_dir，待处理区无法写入")
        return []
    ids = []
    for chunk in chunks:
        pid = pending_store.record(base, chunk, source_tool="grow", reason=reason)
        if pid:
            ids.append(pid)
    return ids

# 事后靠标签把「当时没拆成的」捞回来重新整理。
#
# 标签是唯一可靠的标记：bucket_mgr.create() 会重写桶名——前置自己的时间戳，
# 再过 sanitize_name（只留 \w / 空白 / 中日韩 / 连字符），方括号、括号、斜杠
# 全部被吃掉。所以名字里只放一个人眼可读的词，分段序号用连字符（能活下来），
# 检索一律走标签。
UNDIGESTED_PREFIX = "未拆桶"
UNDIGESTED_TAG = "__undigested__"

# 命中这些子串就认定是限流而非配置问题。
# 2026-07-27 的证据 #1：原文案写「API key 未配置」，实为 429，属误导。
_RATE_LIMIT_HINTS = ("429", "rate limit", "rate_limit", "too many requests", "quota")


def _looks_like_rate_limit(reason: str) -> bool:
    low = (reason or "").lower()
    return any(h in low for h in _RATE_LIMIT_HINTS)


def _explain(reason: str) -> str:
    """把底层异常翻译成一句不误导人的原因说明。"""
    if _looks_like_rate_limit(reason):
        return "整理接口被限流（429），不是 key 没配"
    return "整理接口调用失败"


def _split_by_bytes(content: str, cap: int) -> list[str]:
    """按 UTF-8 字节上限机械切分，不在多字节字符中间断开。

    cap <= 0 表示不限制（config.limits.max_bucket_bytes 关掉的情况）。"""
    if cap <= 0 or len(content.encode("utf-8")) <= cap:
        return [content]

    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for ch in content:
        n = len(ch.encode("utf-8"))
        if size + n > cap and buf:
            chunks.append("".join(buf))
            buf, size = [], 0
        buf.append(ch)
        size += n
    if buf:
        chunks.append("".join(buf))
    return chunks


async def grow_fallback(content: str, reason: str = "") -> str:
    text = (content or "").strip()
    if not text:
        return "内容为空，无法整理。"

    why = _explain(reason)
    batch_id = f"g_{uuid.uuid4().hex[:12]}"

    chunks = _split_by_bytes(text, max_bucket_bytes())
    multi = len(chunks) > 1

    created: list[str] = []
    failed: list[str] = []
    failed_chunks: list[str] = []
    for idx, chunk in enumerate(chunks, 1):
        # 不自己加时间戳：create() 会前置 "YYYY-MM-DD HH-MM-SS "。
        # 序号用连字符而非 (i/n)，括号与斜杠过不了 sanitize_name。
        name = UNDIGESTED_PREFIX + (f" {idx}-{len(chunks)}" if multi else "")
        try:
            bucket_id = await rt.bucket_mgr.create(
                content=chunk,
                tags=[UNDIGESTED_TAG],
                importance=5,
                domain=["未分类"],
                valence=0.5,
                arousal=0.3,
                name=name,
                source_tool="grow",
                grow_batch_id=batch_id,
            )
            created.append(bucket_id)
        except Exception as e:
            rt.logger.error(f"grow fallback create failed / 降级落桶失败 ({idx}/{len(chunks)}): {e}")
            failed.append(f"{idx}/{len(chunks)}: {e}")
            failed_chunks.append(chunk)

    # 落桶失败的段落进待处理区（§1.4）：最常见原因是 embedding 接口同样挂着，
    # create() 在 _sync_embedding 失败时会删文件并抛。待处理区不要向量、不碰 LLM，
    # 是这种「什么都挂了」场景下唯一还写得进去的地方。
    pending_ids = _to_pending(failed_chunks, f"{why}；落桶失败：{failed[0]}") if failed_chunks else []

    if not created:
        detail = failed[0] if failed else "未知错误"
        if pending_ids:
            return (
                f"⚠️ grow 失败、降级落桶也失败，内容已存入待处理区，没有丢。\n"
                f"降级原因：{why}（{reason}）\n"
                f"落桶失败原因：{detail}\n"
                f"待处理区 id：{','.join(str(i) for i in pending_ids)}\n"
                f"接口恢复后由对账补建成桶；在那之前它只在待处理区里，"
                f"breath/dream 都搜不到。"
            )
        # 连待处理区都写不进去（盘满 / 路径不可写）。最后一道防线：原样回吐。
        return (
            f"⚠️ grow 失败、降级落桶失败、待处理区也没写进去，内容没有存下来。\n"
            f"降级原因：{why}（{reason}）\n"
            f"落桶失败原因：{detail}\n"
            f"下面是原文，请自行重试或改存别处，不要丢掉：\n"
            f"---\n{text}"
        )

    head = (
        f"⚠️ 走了降级路径：{why}，没有拆桶，原文整存。\n"
        f"新建 {len(created)} 条「{UNDIGESTED_PREFIX}」桶 batch:{batch_id}\n"
        + "\n".join(f"📝{bid}" for bid in created)
    )
    if failed:
        head += f"\n⚠️ 另有 {len(failed)} 段落桶失败：{failed[0]}"
        if pending_ids:
            head += f"（已存入待处理区 id {','.join(str(i) for i in pending_ids)}，等对账补建）"
        else:
            head += "（待处理区也没写进去，这几段内容已丢失）"
    head += (
        f"\n接口恢复后可以按标签 {UNDIGESTED_TAG} 找回这些桶，"
        f"用 trace 替换正文或重新 grow 一次做拆分。"
    )
    return head
