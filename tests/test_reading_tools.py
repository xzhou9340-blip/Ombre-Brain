"""
========================================
tests/test_reading_tools.py — 共读（reading_*）工具回归
========================================

用一个进程内的假 read-along 后端（stdlib http.server，行为对齐上游
server.js 的 gate/annotate 语义）验证 tools/reading/core.py：

- URL 拼接与参数传递正确（gate、text?from&to、search?q、annotate、comment）
- 门禁语义不被工具层破坏：未解锁章节标题绝不出现在任何输出里
- 409（引文重复）/ 404（引文不存在或越界）转译成带指引的人话
- 回复批注 author 固定 "ai"
- 连接失败时返回排查指引而不是裸异常

不做什么：不测 read-along 本身（那是上游仓库的职责），
不起真实 node 进程，不依赖网络。
========================================
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import pytest

from tools.reading import core as reading


# ============================================================
# 假 read-along 后端
# ------------------------------------------------------------
# 一本书：3 章共 6 段（seq 0-5），她读到 seq 2（第 3 章未解锁）。
# 段 1 和段 3 都含「重复句。」用来触发 409。段 3 未解锁，
# 用来验证 gate 匹配只扫 pushedRanges。
# ============================================================
PARAS = [
    "第一段，她已经读过了。重复句。",
    "第二段，重复句。以及一些别的话。",
    "第三段，独一无二的一句话。",
    "第四段，重复句。但她还没读到这里。",
    "第五段，秘密剧情。",
    "第六段，大结局。",
]
CHAPTERS = [
    {"idx": 0, "title": "第一章 开端", "baseSeq": 0, "paraCount": 2},
    {"idx": 1, "title": "第二章 发展", "baseSeq": 2, "paraCount": 2},
    {"idx": 2, "title": "第三章 秘密结局", "baseSeq": 4, "paraCount": 2},
]
PUSHED = [[0, 2]]           # 已解锁 seq 0-2
FURTHEST = 2
LOCKED_TITLE = "第三章 秘密结局"


def _chapter_of(seq):
    found = CHAPTERS[0]
    for ch in CHAPTERS:
        if ch["baseSeq"] <= seq:
            found = ch
    return found


def _unlocked(seq):
    return any(a <= seq <= b for a, b in PUSHED)


class FakeReadAlong(BaseHTTPRequestHandler):
    annotations = []  # 类级共享，测试间由 fixture 重置

    def _send(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # 静音
        pass

    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/api/books":
            return self._send(200, {"books": [{
                "bookId": "mybook", "title": "测试之书", "author": "某人",
                "chapterCount": 3, "totalChars": 100, "progressPct": 50.0,
                "annotationCount": len(self.annotations),
                "readingChapter": "第二章 发展",
            }]})
        if u.path == "/api/gate/mybook":
            return self._send(200, {
                "bookId": "mybook", "title": "测试之书", "author": "某人",
                "furthestSeq": FURTHEST, "pushedRanges": PUSHED,
                "progressPct": 50.0, "reading": False,
                "lastOpenedAt": "2026-07-13T00:00:00Z",
                "unlockedChapters": [ch for ch in CHAPTERS if ch["baseSeq"] <= FURTHEST],
                "note": "unlockedChapters 之外的章节标题也是保密的",
            })
        if u.path == "/api/gate/mybook/text":
            start, end = int(q["from"][0]), int(q["to"][0])
            paras, locked = [], 0
            for seq in range(start, end + 1):
                if 0 <= seq < len(PARAS) and _unlocked(seq):
                    ch = _chapter_of(seq)
                    paras.append({"seq": seq, "chapter": ch["idx"],
                                  "chapterTitle": ch["title"], "text": PARAS[seq]})
                else:
                    locked += 1
            out = {"paragraphs": paras}
            if locked:
                out["locked"] = f"{locked}段未解锁"
            return self._send(200, out)
        if u.path == "/api/gate/mybook/search":
            query = q.get("q", [""])[0]
            hits = []
            for a, b in PUSHED:
                for seq in range(a, b + 1):
                    if query in PARAS[seq]:
                        ch = _chapter_of(seq)
                        hits.append({"seq": seq, "chapter": ch["idx"],
                                     "chapterTitle": ch["title"], "text": PARAS[seq]})
            return self._send(200, {"hits": hits, "scope": "仅已解锁范围"})
        if u.path == "/api/annotations/mybook":
            return self._send(200, {"annotations": self.annotations})
        if u.path.startswith(("/api/gate/", "/api/annotations/")):
            return self._send(404, {"error": "unknown book"})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        body = self._read_json()
        if u.path == "/api/annotate":
            quote = str(body.get("quote") or "")
            matches = []
            for a, b in PUSHED:
                for seq in range(a, b + 1):
                    at = PARAS[seq].find(quote)
                    if quote and at >= 0:
                        matches.append({"seq": seq, "startOff": at,
                                        "endOff": at + len(quote),
                                        "preview": PARAS[seq][:40]})
            if not matches:
                return self._send(404, {"error": "quote not found in unlocked text"})
            if len(matches) > 1:
                return self._send(409, {"error": "quote ambiguous, give a longer quote",
                                        "matches": matches[:5]})
            anno = {"id": f"anno{len(self.annotations) + 1}", "seq": matches[0]["seq"],
                    "quote": quote, "createdBy": "ai", "createdAt": "2026-07-13T00:00:00Z",
                    "comments": [{"id": "c1", "author": "ai",
                                  "text": body.get("comment", ""),
                                  "createdAt": "2026-07-13T00:00:00Z"}]}
            self.annotations.append(anno)
            return self._send(200, {"ok": True, "annotation": anno})
        if u.path.startswith("/api/annotations/mybook/") and u.path.endswith("/comment"):
            anno_id = u.path.split("/")[4]
            for anno in self.annotations:
                if anno["id"] == anno_id:
                    anno["comments"].append({"id": "cX", "author": body.get("author"),
                                             "text": body.get("text"),
                                             "createdAt": "2026-07-13T00:00:00Z"})
                    return self._send(200, {"ok": True})
            return self._send(404, {"error": "unknown annotation"})
        return self._send(404, {"error": "not found"})


@pytest.fixture()
def fake_server(monkeypatch):
    FakeReadAlong.annotations = [{
        "id": "anno-human", "seq": 0, "quote": "第一段，她已经读过了。",
        "createdBy": "human", "createdAt": "2026-07-12T00:00:00Z",
        "comments": [{"id": "hc1", "author": "human",
                      "text": "这里写得真好", "createdAt": "2026-07-12T00:00:00Z"}],
    }]
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeReadAlong)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("READING_API_BASE", f"http://127.0.0.1:{server.server_address[1]}")
    yield server
    server.shutdown()
    thread.join(timeout=5)


# ============================================================
# 用例
# ============================================================
@pytest.mark.asyncio
async def test_progress_lists_books_without_book_id(fake_server):
    out = await reading.progress("")
    assert "bookId=mybook" in out
    assert "测试之书" in out


@pytest.mark.asyncio
async def test_progress_gate_view_hides_locked_chapter(fake_server):
    out = await reading.progress("mybook")
    assert "第一章 开端" in out and "第二章 发展" in out
    assert LOCKED_TITLE not in out            # 门禁：未解锁章节连标题都没有
    assert "0-2" in out                        # pushedRanges 摘要


@pytest.mark.asyncio
async def test_text_returns_unlocked_and_flags_locked(fake_server):
    out = await reading.text("mybook", 0, 5)
    assert PARAS[0] in out and PARAS[2] in out
    assert PARAS[4] not in out and PARAS[5] not in out   # 未解锁正文绝不出现
    assert "未解锁" in out


@pytest.mark.asyncio
async def test_search_only_unlocked_scope(fake_server):
    out = await reading.search("mybook", "秘密剧情")
    assert "没有命中" in out
    out2 = await reading.search("mybook", "独一无二")
    assert PARAS[2] in out2


@pytest.mark.asyncio
async def test_annotate_success(fake_server):
    out = await reading.annotate("mybook", "第三段，独一无二的一句话。", "读到这句想起你")
    assert "写好了" in out
    listing = await reading.annotations("mybook")
    assert "读到这句想起你" in listing


@pytest.mark.asyncio
async def test_annotate_409_suggests_longer_quote(fake_server):
    out = await reading.annotate("mybook", "重复句。", "会撞重复")
    assert "更长" in out


@pytest.mark.asyncio
async def test_annotate_404_gives_guidance(fake_server):
    # 段 4 未解锁：即使 quote 与原文逐字一致也必须 404
    out = await reading.annotate("mybook", "第五段，秘密剧情。", "越界批注")
    assert "没找到" in out and "解锁" in out


@pytest.mark.asyncio
async def test_reply_uses_ai_author(fake_server):
    out = await reading.annotations("mybook", reply_to="anno-human", reply_text="我也觉得")
    assert "回复" in out
    listing = await reading.annotations("mybook")
    assert "我也觉得" in listing
    # 假后端原样存了 author 字段，走列表输出应把 ai 显示为「你」
    assert "你: 我也觉得" in listing


@pytest.mark.asyncio
async def test_reply_needs_both_fields(fake_server):
    out = await reading.annotations("mybook", reply_to="anno-human")
    assert "同时" in out


@pytest.mark.asyncio
async def test_unknown_book_and_bad_book_id(fake_server):
    out = await reading.progress("nope")
    assert "没有" in out
    out2 = await reading.text("../etc", 0, 1)
    assert "不合法" in out2


@pytest.mark.asyncio
async def test_connection_refused_returns_help(monkeypatch):
    monkeypatch.setenv("READING_API_BASE", "http://127.0.0.1:1")   # 必然拒绝
    out = await reading.progress("mybook")
    assert "连不上" in out and "pm2" in out
