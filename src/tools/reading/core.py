"""
========================================
tools/reading/core.py — 共读工具实现
========================================

对 read-along 后端（本机 HTTP 服务）的五个只读/批注操作做人话包装：

- progress(book_id)          → 不传=列书架；传=该书门禁视图（进度/已解锁章节/可回看段号）
- text(book_id, from, to)    → 按段号回看已解锁正文（GET /api/gate/{id}/text）
- search(book_id, q)         → 只在已解锁范围内全文检索（GET /api/gate/{id}/search）
- annotate(book_id, quote, comment) → 划线写批注（POST /api/annotate，author 固定 ai）
- annotations(book_id, reply_to, reply_text) → 查批注列表 / 在某条批注下回复

关键行为：
- 后端地址每次调用读 READING_API_BASE（默认 http://127.0.0.1:18004），
  与 server.py 的 _fire_webhook 同款「不缓存 env」策略，改配置即时生效
- 防剧透门禁在 read-along 服务端：这里只调 gate/annotate 端点，
  404/409 原样转译成给模型看得懂的指引，绝不尝试其它端点绕过
- 连接失败时返回排查指引（pm2 进程 / READING_API_BASE / Docker 网络），
  不抛裸异常给 MCP 层

不做什么（边界）：
- 不调 /api/book/*、/api/chapter/* 等无门禁端点（那是给人类阅读器用的）
- 不写 read-along 的 data/，不动 Ombre 的记忆桶

对外暴露：progress / text / search / annotate / annotations → str
========================================
"""

import os
import re
import json
import logging
from typing import Optional

logger = logging.getLogger("ombre_brain")

# ============================================================
# 调参面板
# ============================================================
_DEFAULT_BASE = "http://127.0.0.1:18004"   # read-along 默认监听地址（服务器本机）
_HTTP_TIMEOUT_SECONDS = 10.0               # 本机回环，10s 足够
_QUOTE_PREVIEW_CHARS = 80                  # 批注列表里引文的截断长度
_TEXT_RANGE_MAX = 200                      # gate/text 单次段数上限（与服务端一致）

_BOOK_ID_RE = re.compile(r"^[\w-]+$")      # 与 read-along 路由的 ([\w-]+) 一致


def _base_url() -> str:
    """每次调用现读 env，便于 dashboard/env 改动即时生效。"""
    return (os.environ.get("READING_API_BASE") or _DEFAULT_BASE).strip().rstrip("/")


def _conn_help(exc: Exception) -> str:
    base = _base_url()
    return (
        f"共读服务连不上（{base}）：{type(exc).__name__}: {exc}\n"
        f"排查：① 服务器上 `pm2 status reading` 确认 read-along 在跑；"
        f"② `curl -s {base}/health` 应返回 {{\"ok\":true}}；"
        f"③ 若 Ombre 跑在 Docker 容器里，容器内 127.0.0.1 不是宿主机，"
        f"需按 deploy/read-along/README.md 配置 READING_API_BASE。"
    )


async def _request(method: str, path: str, *, params: Optional[dict] = None,
                   json_body: Optional[dict] = None) -> tuple[int, dict]:
    """发一次请求，返回 (status_code, 解析后的 json dict)。连接类异常向上抛。"""
    import httpx
    url = _base_url() + path
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
        resp = await client.request(method, url, params=params, json=json_body)
    try:
        data = resp.json()
        if not isinstance(data, dict):
            data = {"error": str(data)[:200]}
    except (json.JSONDecodeError, ValueError):
        data = {"error": (resp.text or "")[:200] or f"HTTP {resp.status_code}（非 JSON 响应）"}
    return resp.status_code, data


def _check_book_id(book_id: str) -> Optional[str]:
    """bookId 合法性校验。非法时返回错误文案，合法返回 None。"""
    if not book_id or not _BOOK_ID_RE.match(book_id):
        return (
            f"bookId 不合法：{book_id!r}（只允许字母/数字/下划线/连字符）。"
            f"先用 reading_progress 不带参数列出书架，拿到正确的 bookId。"
        )
    return None


def _fmt_para(p: dict) -> str:
    """gate/text、gate/search 返回的段落统一排版：#段号 [章节] 正文。"""
    ch = p.get("chapterTitle") or f"章节{p.get('chapter', '?')}"
    return f"#{p.get('seq', '?')} [{ch}] {p.get('text', '')}"


# ============================================================
# progress — 书架 / 单书门禁视图
# ============================================================
async def progress(book_id: Optional[str] = "") -> str:
    book_id = (book_id or "").strip()
    try:
        if not book_id:
            return await _list_books()
        err = _check_book_id(book_id)
        if err:
            return err
        status, data = await _request("GET", f"/api/gate/{book_id}")
    except Exception as e:
        return _conn_help(e)
    if status == 404:
        return f"没有 bookId={book_id} 这本书。用 reading_progress 不带参数看看书架上有什么。"
    if status != 200:
        return f"查询进度失败（HTTP {status}）：{data.get('error', data)}"

    lines = [
        f"《{data.get('title', book_id)}》 {data.get('author') or ''}".rstrip(),
        f"bookId: {book_id}",
        f"进度: {data.get('progressPct', 0)}%（furthestSeq={data.get('furthestSeq', -1)}）"
        f"{'，她现在正翻开这本书' if data.get('reading') else ''}",
    ]
    if data.get("lastOpenedAt"):
        lines.append(f"最近打开: {data['lastOpenedAt']}")
    ranges = data.get("pushedRanges") or []
    if ranges:
        rng = "、".join(f"{r[0]}-{r[1]}" for r in ranges if isinstance(r, (list, tuple)) and len(r) == 2)
        lines.append(f"可回看的段号区间（已解锁）: {rng}")
    chapters = data.get("unlockedChapters") or []
    if chapters:
        lines.append(f"已解锁章节（{len(chapters)} 章）:")
        for ch in chapters:
            lines.append(
                f"  [{ch.get('idx')}] {ch.get('title')}（baseSeq {ch.get('baseSeq')}，{ch.get('paraCount')} 段）"
            )
    else:
        lines.append("还没有解锁任何章节——她还没开始读，或停留没到推送阈值。")
    lines.append("※ 未解锁章节连标题都是保密的（服务端防剧透门禁），别绕、也别去网上搜后续情节。")
    return "\n".join(lines)


async def _list_books() -> str:
    status, data = await _request("GET", "/api/books")
    if status != 200:
        return f"查询书架失败（HTTP {status}）：{data.get('error', data)}"
    books = data.get("books") or []
    if not books:
        return "书架还是空的。她可以在手机阅读器上传 epub/txt，或在服务器上用 import-book.js 导入。"
    lines = [f"=== 书架（{len(books)} 本）==="]
    for b in books:
        cur = f"，正读到「{b['readingChapter']}」" if b.get("readingChapter") else ""
        lines.append(
            f"· bookId={b.get('bookId')} 《{b.get('title')}》 {b.get('author') or ''}".rstrip()
            + f" — 进度 {b.get('progressPct', 0)}%{cur}，"
            f"{b.get('chapterCount', '?')} 章 / {b.get('totalChars', '?')} 字，"
            f"批注 {b.get('annotationCount', 0)} 条"
        )
    lines.append("用 reading_progress(book_id=...) 看某本书的已解锁章节与可回看段号。")
    return "\n".join(lines)


# ============================================================
# text — 回看已解锁正文
# ============================================================
async def text(book_id: str, from_seq: int, to_seq: int) -> str:
    err = _check_book_id((book_id or "").strip())
    if err:
        return err
    book_id = book_id.strip()
    try:
        from_seq = int(from_seq)
        to_seq = int(to_seq)
    except (TypeError, ValueError):
        return "from_seq/to_seq 需要是整数段号（见 reading_progress 返回的段号区间）。"
    if to_seq < from_seq:
        return "to_seq 不能小于 from_seq。"
    if to_seq - from_seq > _TEXT_RANGE_MAX:
        return f"单次最多回看 {_TEXT_RANGE_MAX} 段，把范围拆小一点。"
    try:
        status, data = await _request(
            "GET", f"/api/gate/{book_id}/text", params={"from": from_seq, "to": to_seq}
        )
    except Exception as e:
        return _conn_help(e)
    if status == 404:
        return f"没有 bookId={book_id} 这本书。"
    if status != 200:
        return f"回看正文失败（HTTP {status}）：{data.get('error', data)}"
    paras = data.get("paragraphs") or []
    lines = []
    if paras:
        lines.append(f"《…》第 {from_seq}-{to_seq} 段（已解锁 {len(paras)} 段）：")
        lines.extend(_fmt_para(p) for p in paras)
    else:
        lines.append(f"第 {from_seq}-{to_seq} 段里没有已解锁的内容。")
    if data.get("locked"):
        lines.append(f"※ 该范围内有{data['locked']}（她还没读到，等她读过才解锁）。")
    return "\n".join(lines)


# ============================================================
# search — 只搜已解锁范围
# ============================================================
async def search(book_id: str, q: str) -> str:
    err = _check_book_id((book_id or "").strip())
    if err:
        return err
    book_id = book_id.strip()
    q = (q or "").strip()
    if not q:
        return "q 不能为空：给一个想找的关键词。"
    try:
        status, data = await _request("GET", f"/api/gate/{book_id}/search", params={"q": q})
    except Exception as e:
        return _conn_help(e)
    if status == 404:
        return f"没有 bookId={book_id} 这本书。"
    if status != 200:
        return f"检索失败（HTTP {status}）：{data.get('error', data)}"
    hits = data.get("hits") or []
    if not hits:
        return (
            f"「{q}」在已解锁范围内没有命中。"
            f"注意检索范围仅限她已读过的部分——搜不到可能只是还没读到，不是没有。"
        )
    lines = [f"「{q}」命中 {len(hits)} 段（仅已解锁范围）："]
    lines.extend(_fmt_para(p) for p in hits)
    return "\n".join(lines)


# ============================================================
# annotate — 划线写批注
# ============================================================
async def annotate(book_id: str, quote: str, comment: str) -> str:
    err = _check_book_id((book_id or "").strip())
    if err:
        return err
    book_id = book_id.strip()
    quote = (quote or "").strip()
    comment = (comment or "").strip()
    if not quote:
        return "quote 不能为空：先用 reading_text 找到想划的那句原文，逐字复制（含标点）。"
    if not comment:
        return "comment 不能为空：写点你想对她说的话。"
    try:
        status, data = await _request(
            "POST", "/api/annotate",
            json_body={"bookId": book_id, "quote": quote, "comment": comment},
        )
    except Exception as e:
        return _conn_help(e)
    if status == 200:
        anno = data.get("annotation") or {}
        return (
            f"批注写好了（id={anno.get('id')}，第 {anno.get('seq')} 段）。"
            f"她的阅读器里，这句「{quote[:_QUOTE_PREVIEW_CHARS]}」下面会出现你的划线和留言。"
        )
    if status == 409:
        matches = data.get("matches") or []
        preview = "\n".join(
            f"  · #段{m.get('seq')} 开头：{m.get('preview', '')}…" for m in matches
        )
        return (
            f"这句引文在已解锁文本里出现了不止一次（{data.get('error', '')}）。\n"
            + (preview + "\n" if preview else "")
            + "换一句更长的、独一无二的原文再试（往前后多带几个字就行）。"
        )
    if status == 404:
        return (
            f"没找到这句引文：{data.get('error', '')}\n"
            f"两个常见原因：① quote 与原文不是逐字一致（全角/半角标点最容易错）——"
            f"先用 reading_text 回看原文，复制原文再批；"
            f"② 想批的内容她还没读到（未解锁的内容不能批，这是设计，不是故障）。"
        )
    return f"批注失败（HTTP {status}）：{data.get('error', data)}"


# ============================================================
# annotations — 查批注 / 回复批注
# ============================================================
def _fmt_annotation(a: dict) -> list[str]:
    quote = str(a.get("quote") or "")
    if len(quote) > _QUOTE_PREVIEW_CHARS:
        quote = quote[:_QUOTE_PREVIEW_CHARS] + "…"
    who = {"human": "她", "ai": "你"}.get(str(a.get("createdBy")), str(a.get("createdBy")))
    lines = [
        f"· id={a.get('id')} 第 {a.get('seq')} 段，{who}划的「{quote}」（{a.get('createdAt', '')}）"
    ]
    for c in a.get("comments") or []:
        c_who = {"human": "她", "ai": "你"}.get(str(c.get("author")), str(c.get("author")))
        lines.append(f"    {c_who}: {c.get('text', '')}")
    return lines


async def annotations(book_id: str, reply_to: Optional[str] = "",
                      reply_text: Optional[str] = "") -> str:
    err = _check_book_id((book_id or "").strip())
    if err:
        return err
    book_id = book_id.strip()
    reply_to = (reply_to or "").strip()
    reply_text = (reply_text or "").strip()

    # --- 回复模式：reply_to + reply_text 都给了才算 ---
    if reply_to or reply_text:
        if not (reply_to and reply_text):
            return "回复需要同时给 reply_to（批注 id）和 reply_text（回复内容）；只查列表就两个都别传。"
        if not _BOOK_ID_RE.match(reply_to):
            return f"reply_to 不像一个批注 id：{reply_to!r}。先查列表拿到正确的 id。"
        try:
            status, data = await _request(
                "POST", f"/api/annotations/{book_id}/{reply_to}/comment",
                json_body={"author": "ai", "text": reply_text},
            )
        except Exception as e:
            return _conn_help(e)
        if status == 200:
            return f"回复已经挂在那条批注下面了，她翻到那页就能看到。"
        if status == 404:
            return f"没找到这条批注（id={reply_to}）：{data.get('error', '')}。先查列表核对 id。"
        return f"回复失败（HTTP {status}）：{data.get('error', data)}"

    # --- 列表模式 ---
    try:
        status, data = await _request("GET", f"/api/annotations/{book_id}")
    except Exception as e:
        return _conn_help(e)
    if status != 200:
        return f"查询批注失败（HTTP {status}）：{data.get('error', data)}"
    annos = data.get("annotations") or []
    if not annos:
        return "这本书还没有任何批注。读到有感触的地方，用 reading_annotate 划一句。"
    lines = [f"=== 批注（{len(annos)} 条）==="]
    for a in sorted(annos, key=lambda x: x.get("seq") or 0):
        lines.extend(_fmt_annotation(a))
    lines.append("回复某条：reading_annotations(book_id, reply_to=<id>, reply_text=<你的话>)。")
    return "\n".join(lines)
