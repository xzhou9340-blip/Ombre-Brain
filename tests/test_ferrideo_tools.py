"""
========================================
tests/test_ferrideo_tools.py — 观影（ferrideo_*）工具回归
========================================

用一个进程内的假门禁后端（stdlib http.server，行为对齐 ferrideo/gate.js）
验证 tools/ferrideo/core.py：

- URL 拼接正确，且**所有请求都落在门禁前缀下**——工具层不去碰播放器的
  /api/rooms/*（那套能取字幕全文）。这条是防剧透的最后一道：node 侧那个
  端口上根本没有播放器路由，但工具层也不许往那个方向拼路径
- 房间号校验与归一化（小写照收、非法给指引）
- 404 / 409 转译成带指引的人话，而不是裸状态码
- get_context 把帧**单独返回二进制**，不塞进文字（一张图的 base64 是
  二十多万字符，塞进文字等于把上下文烧光）
- 票根入库：成功回桶号；入库失败走 letter 降级且明说「还没进记忆库」；
  连 letter 都失败时明说「务必自己留下来」——降级可以，丢失不行
- 连接失败时返回排查指引

另外钉住连接器说明里的工具数是**运行时算出来的**，不是写死的数字。

不做什么：不起真实 node 子进程（那是 test_ferrideo_bridge 的事），不依赖网络。
========================================
"""

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from tools.ferrideo import core as fer


# ============================================================
# 假门禁后端
# ------------------------------------------------------------
# 一场《花样年华》，100 句台词，播放头在第 40 句。
# 后端只认门禁前缀下的路径；任何别的路径返回 404 并记进 SEEN，
# 测试用它断言工具层没往播放器那边拼路径。
# ============================================================
GATE_PREFIX = "/tk/api/gate"
SEEN: list = []
STATE = {
    "frame": None,          # bytes or None
    "ticket_fail": False,
    "full": False,          # True＝建房时名额已满
}
ROOMS_LIST = [
    {"roomId": "AAA222", "title": "测试一", "position": "00:00:12", "idleMinutes": 37,
     "finished": False, "hasTicket": False, "quoteCount": 0, "noteCount": 0, "danmakuCount": 0},
    {"roomId": "K7X2M9", "title": "花样年华", "position": "00:30:34", "idleMinutes": 0,
     "finished": False, "hasTicket": False, "quoteCount": 1, "noteCount": 0, "danmakuCount": 3},
]


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except ValueError:
            return {}

    def do_GET(self):
        SEEN.append(("GET", self.path))
        path = self.path.split("?")[0]
        if path == f"{GATE_PREFIX}/rooms":
            return self._send(200, {"rooms": ROOMS_LIST})
        if path == f"{GATE_PREFIX}/rooms/K7X2M9/context":
            out = {
                "roomId": "K7X2M9", "title": "花样年华", "state": "playing",
                "position": "00:30:34", "duration": "02:01:00", "progressPercent": 25,
                "subtitleMode": "track",
                "currentLine": "如果我多一张船票",
                "recentLines": ["前三句", "前两句", "前一句"],
                "nextLineExists": True,
                "staleSeconds": 2, "danmakuCount": 3, "quoteCount": 1, "noteCount": 0,
                "finished": False, "frameMode": "ok", "hints": [],
            }
            if STATE["frame"] is not None and "includeFrame=1" in self.path:
                import base64
                out["frame"] = "data:image/jpeg;base64," + base64.b64encode(STATE["frame"]).decode()
                out["frameAt"] = "00:30:34"
                out["frameAgeSeconds"] = 4
            return self._send(200, out)
        if path == f"{GATE_PREFIX}/rooms/NOROOM/context" or path.endswith("/NOROOM/room"):
            return self._send(404, {"error": "房间不存在，或者已经过期了"})
        if path == f"{GATE_PREFIX}/rooms/K7X2M9/room":
            return self._send(200, {
                "roomId": "K7X2M9", "title": "花样年华", "createdAt": "2026-08-26T12:00:00Z",
                "state": "paused", "position": "01:58:12", "duration": "02:01:00",
                "finished": True, "watchedFor": "01:58:12", "subtitleMode": "track",
                "quotes": [{"text": "如果我多一张船票", "at": "00:30:34"}],
                "notes": [{"text": "她一直没回头", "at": "01:40:00", "source": "ai"}],
                "danmaku": [{"text": "这段配乐", "author": "克", "delivered": True}],
                "frameCount": 2, "frameMode": "ok", "ticket": None,
            })
        return self._send(404, {"error": "没有这条路由"})

    def do_POST(self):
        SEEN.append(("POST", self.path))
        path = self.path.split("?")[0]
        body = self._body()
        if path == f"{GATE_PREFIX}/rooms" and STATE["full"]:
            return self._send(409, {"error": "同时最多 5 个房间，现在都还开着。关掉一个不用的再开新的",
                                    "rooms": ROOMS_LIST})
        if path == f"{GATE_PREFIX}/rooms/K7X2M9/close":
            return self._send(200, {"ok": True, "roomId": "K7X2M9", "title": "花样年华", "rescued": True})
        if path == f"{GATE_PREFIX}/rooms/AAA222/close":
            return self._send(200, {"ok": True, "roomId": "AAA222", "title": "测试一", "rescued": False})
        if path == f"{GATE_PREFIX}/rooms":
            return self._send(201, {"roomId": "K7X2M9", "title": body.get("title") or "未命名",
                                    "tip": "房间号 K7X2M9，在 iPad 上输入这个号"})
        if path == f"{GATE_PREFIX}/rooms/K7X2M9/danmaku":
            text = (body.get("text") or "")[:60]
            return self._send(201, {"ok": True, "text": text, "author": body.get("author"),
                                    "truncated": len(body.get("text") or "") > 60})
        if path == f"{GATE_PREFIX}/rooms/K7X2M9/frame-request":
            return self._send(202, {"ok": True, "requestId": "abc",
                                    "tip": '已经跟播放器要了一帧，等 3-5 秒后用 ferrideo(action="context", include_frame=True) 取。'})
        if path == f"{GATE_PREFIX}/rooms/K7X2M9/note":
            return self._send(201, {"ok": True, "at": "00:30:34", "text": body.get("text")})
        if path == f"{GATE_PREFIX}/rooms/K7X2M9/ticket":
            return self._send(200, {
                "roomId": "K7X2M9", "title": "花样年华", "date": "2026-08-26",
                "duration": "02:01:00", "watchedFor": "01:58:12",
                "quotes": ["如果我多一张船票"], "notes": ["她一直没回头"],
                "danmakuCount": 47, "mood": body.get("mood") or None, "note": body.get("note") or None,
            })
        if path.endswith("/NOROOM/danmaku") or "/NOROOM/" in path:
            return self._send(404, {"error": "房间不存在，或者已经过期了"})
        return self._send(404, {"error": "没有这条路由"})


@pytest.fixture()
def gate(monkeypatch):
    SEEN.clear()
    STATE["frame"] = None
    STATE["full"] = False
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}{GATE_PREFIX}"
    monkeypatch.setenv("FERRIDEO_API_BASE", base)
    yield base
    srv.shutdown()


# ============================================================
# 地址与房间号
# ============================================================
@pytest.mark.asyncio
async def test_create_room_gives_room_id_and_address(gate, monkeypatch):
    from web import ferrideo_bridge
    monkeypatch.setattr(ferrideo_bridge, "public_player_url", lambda: "https://ombre.example.com/ferrideo/tk/")
    out = await fer.create_room("花样年华")
    assert "K7X2M9" in out
    assert "https://ombre.example.com/ferrideo/tk/" in out, "要给她一个能点开的地址"
    assert "加入房间" in out


@pytest.mark.asyncio
async def test_create_room_without_known_domain_still_usable(gate, monkeypatch):
    """还没人从外面访问过时报不出域名——那就说清楚，不要编一个。"""
    from web import ferrideo_bridge
    monkeypatch.setattr(ferrideo_bridge, "public_player_url", lambda: "")
    out = await fer.create_room("")
    assert "K7X2M9" in out
    assert "域名" in out and "/ferrideo/" in out
    assert "http://127.0.0.1" not in out, "绝不能把内部环回地址报给她"


@pytest.mark.asyncio
async def test_room_id_is_validated_and_normalised(gate):
    out = await fer.get_context("k7x2m9")           # 小写照收
    assert "花样年华" in out[0]
    bad = await fer.send_danmaku("K7X2", "测试")
    assert "不合法" in bad and "K7X2M9" in bad      # 给个例子让模型照着问


@pytest.mark.asyncio
async def test_missing_room_is_explained_not_coded(gate):
    out = await fer.get_context("NOROOM")
    assert "没有 NOROOM 这个房间" in out[0]
    assert 'ferrideo(action="create_room")' in out[0], "指引里的工具名要跟合并入口一致"
    assert "404" not in out[0]


# ============================================================
# 名额满了：列出来、能关
# ------------------------------------------------------------
# 真实事故：测试时开了 5 个房间都没退出，克只能回一句「你开了五个房间」，
# 她既不知道是哪五个，也没有办法关。
# ============================================================
@pytest.mark.asyncio
async def test_full_create_lists_rooms_and_says_how_to_close(gate):
    STATE["full"] = True
    out = await fer.create_room("新的一部")
    assert "开不了新房间" in out
    assert "AAA222" in out and "测试一" in out and "K7X2M9" in out
    assert "37 分钟没动静" in out and "什么都没留下" in out, "要让人一眼认出测试时丢下的空房间"
    assert "正在看" in out
    assert 'ferrideo(action="close"' in out


@pytest.mark.asyncio
async def test_list_rooms(gate):
    out = await fer.list_rooms()
    assert "开着 2 个房间" in out
    assert "1 句摘录" in out and "3 条弹幕" in out


@pytest.mark.asyncio
async def test_close_room_says_whether_it_rescued(gate):
    out = await fer.close_room("k7x2m9")
    assert "关掉了" in out and "letter" in out, "有摘录的关掉时要说已经存进 letter"
    out2 = await fer.close_room("AAA222")
    assert "关掉了" in out2 and "letter" not in out2


# ============================================================
# get_context
# ============================================================
@pytest.mark.asyncio
async def test_context_is_human_readable(gate):
    text, frame = await fer.get_context("K7X2M9")
    assert frame is None
    assert "00:30:34" in text and "02:01:00" in text
    assert "如果我多一张船票" in text
    assert "前一句" in text
    assert "正在放" in text
    assert "拿不到" in text, "要明说播放头之后的台词拿不到"
    assert "1834000" not in text, "不许给毫秒数"


@pytest.mark.asyncio
async def test_frame_comes_back_as_bytes_not_text(gate):
    """帧走 image content block。塞进文字的话，一张图就是二十多万字符。"""
    STATE["frame"] = b"\xff\xd8\xff\xe0" + b"A" * 2000
    text, frame = await fer.get_context("K7X2M9", include_frame=True)
    assert frame == STATE["frame"]
    assert "base64" not in text
    assert "AAAA" not in text
    assert "00:30:34" in text


# ============================================================
# 工具层不许往播放器那边拼路径
# ============================================================
@pytest.mark.asyncio
async def test_tools_only_touch_the_gate_prefix(gate):
    STATE["frame"] = b"\xff\xd8\xff\xe0"
    await fer.get_context("K7X2M9", include_frame=True)
    await fer.get_room("K7X2M9")
    await fer.send_danmaku("K7X2M9", "这段配乐")
    await fer.request_frame("K7X2M9")
    await fer.add_note("K7X2M9", "她一直没回头")
    assert SEEN, "应该有请求记录"
    for method, path in SEEN:
        assert path.startswith(GATE_PREFIX), f"{method} {path} 跑到门禁前缀外面去了"
        assert "/api/rooms/" not in path.replace(GATE_PREFIX, ""), f"{path} 在往播放器那套路由上拼"
        assert "subtitle" not in path and "cues" not in path, f"{path} 在试字幕全文"


# ============================================================
# 写入类
# ============================================================
@pytest.mark.asyncio
async def test_danmaku_reports_truncation_and_author(gate):
    out = await fer.send_danmaku("K7X2M9", "啊" * 100)
    assert "截断" in out and "写短点" in out
    out2 = await fer.send_danmaku("K7X2M9", "这段配乐")
    assert "署名 克" in out2, "author 默认是克"
    assert "3 秒" in out2


@pytest.mark.asyncio
async def test_request_frame_says_it_is_async(gate):
    out = await fer.request_frame("K7X2M9")
    assert "3-5 秒" in out and 'ferrideo(action="context"' in out


@pytest.mark.asyncio
async def test_add_note_binds_time(gate):
    out = await fer.add_note("K7X2M9", "她一直没回头")
    assert "00:30:34" in out and "她一直没回头" in out
    assert "不能是空的" in await fer.add_note("K7X2M9", "   ")


@pytest.mark.asyncio
async def test_get_room_lists_everything_but_no_subtitles(gate):
    out = await fer.get_room("K7X2M9")
    assert "如果我多一张船票" in out
    assert "她一直没回头" in out
    assert "这段配乐" in out
    assert "已散场" in out and "01:58:12" in out
    assert "cues" not in out


# ============================================================
# 票根入库
# ============================================================
class _FakeBucketMgr:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    async def create(self, **kw):
        self.calls.append(kw)
        if self.fail:
            raise RuntimeError("embedding 不可用")
        return "bkt_0001"


@pytest.mark.asyncio
async def test_ticket_goes_straight_into_memory(gate, monkeypatch):
    from tools import _runtime as rt
    mgr = _FakeBucketMgr()
    monkeypatch.setattr(rt, "bucket_mgr", mgr)
    out = await fer.generate_ticket("K7X2M9", mood="安静", note="看完没说话")

    assert "bkt_0001" in out and "#观影" in out
    assert "如果我多一张船票" in out and "安静" in out
    kw = mgr.calls[0]
    assert "观影" in kw["tags"] and "花样年华" in kw["tags"]
    assert "花样年华" in kw["content"]
    # 不打标、不合并：直接建桶，没有走 hold 那条会与近似老桶合并的路径
    assert kw.get("pinned") in (None, False)
    assert "source_bucket" not in kw


@pytest.mark.asyncio
async def test_ticket_falls_back_to_letter_and_says_so(gate, monkeypatch):
    """一场电影看完了，不该因为写库出问题就把它弄丢。"""
    from tools import _runtime as rt
    from tools.plan import core as plan_core
    monkeypatch.setattr(rt, "bucket_mgr", _FakeBucketMgr(fail=True))
    letters = []

    async def fake_letter_write(**kw):
        letters.append(kw)
        return "ok"

    monkeypatch.setattr(plan_core, "letter_write", fake_letter_write)
    out = await fer.generate_ticket("K7X2M9")

    assert "如果我多一张船票" in out, "票根本身必须照常返回"
    assert "还没进记忆库" in out
    assert "letter" in out and "中转不是终点" in out
    assert len(letters) == 1
    assert "如果我多一张船票" in letters[0]["content"]


@pytest.mark.asyncio
async def test_ticket_when_even_letter_fails(gate, monkeypatch):
    from tools import _runtime as rt
    from tools.plan import core as plan_core
    monkeypatch.setattr(rt, "bucket_mgr", _FakeBucketMgr(fail=True))

    async def boom(**kw):
        raise RuntimeError("盘满了")

    monkeypatch.setattr(plan_core, "letter_write", boom)
    out = await fer.generate_ticket("K7X2M9")
    assert "如果我多一张船票" in out
    assert "务必自己留下来" in out


# ============================================================
# 连不上
# ============================================================
@pytest.mark.asyncio
async def test_connection_failure_gives_guidance(monkeypatch):
    monkeypatch.setenv("FERRIDEO_API_BASE", "http://127.0.0.1:1")   # 肯定连不上
    text, frame = await fer.get_context("K7X2M9")
    assert frame is None
    assert "连不上" in text and "[ferrideo]" in text
    assert "照常能放片" in text, "要说明这不影响她看片"
    assert "Traceback" not in text


# ============================================================
# 连接器说明里的工具数必须是算出来的
# ============================================================
def test_connector_instructions_has_no_hardcoded_tool_count():
    import server
    raw = server.OMBRE_CONNECTOR_INSTRUCTIONS
    assert "__TOOL_COUNT__" in raw and "__MAX_RESULTS__" in raw
    assert not re.search(r"这里有 \d+ 个", raw), "工具数不许写死——加个工具就没人记得回来改"
    assert not re.search(r"max_results=\d+", raw), "max_results 不许写死"


def test_apply_connector_instructions_fills_real_numbers():
    """数的是两个实例的并集减去 _DISABLED_TOOLS：
    - 取并集 → 在「回灌之前」调也得到完整数字（只数主实例会得到 5）
    - 减瘦身名单 → 不把已经不对外暴露的工具算进去（瘦身只从 mcp 里 del，
      它们还留在 mcp_extra 里）
    """
    import server
    n = server.apply_connector_instructions()
    union = set(server.mcp._tool_manager._tools) | set(server.mcp_extra._tool_manager._tools)
    expected = len(union - set(server._DISABLED_TOOLS))
    assert n == expected and n >= 15, f"工具数看着不对：{n}"
    assert n < len(union), "被摘除的工具不该算进数字里"

    ins = server.mcp.instructions
    assert f"这里有 {n} 个" in ins
    assert f"max_results={n + 10}" in ins
    assert "__TOOL_COUNT__" not in ins and "__MAX_RESULTS__" not in ins
    # 观影入口在场；七条 action 的路由由 test_tool_merge_routing 钉
    for name in ("ferrideo", "breath"):
        assert name in union, f"{name} 没注册上"
