"""
========================================
tools/ferrideo/core.py — 观影工具实现
========================================

对 ferrideo 子进程的**门禁端口**做人话包装：

- create_room(title)                  → 开一场，返回房间号 + 进场地址
- list_rooms()                        → 开着的房间（名额满了时用来挑一个关）
- close_room(room_id)                 → 关掉一个（有摘录/笔记会先抢救进 letter）
- get_context(room_id, include_frame)  → 现在放到哪、这一句是什么（最重要的一个）
- send_danmaku(room_id, text, author)  → 往她画面上发一条弹幕
- request_frame(room_id)               → 跟播放器要一帧（异步）
- add_note(room_id, text)              → 记一笔，自动绑当前时间
- get_room(room_id)                    → 房间全貌：笔记、摘录、弹幕
- generate_ticket(room_id, mood, note) → 出票根，并直接存成一条 #观影 记忆

关键行为：
- 地址每次调用现读：env FERRIDEO_API_BASE > bridge.internal_gate_base() > 本地默认。
  **那是门禁端口**，不是播放器端口
- get_context 返回 (文字, 帧的 jpeg 二进制 or None)：帧走 MCP 的 image content
  block（server.py 里组装），不塞进文字——一张 200KB 的图编成 base64 是
  27 万个字符，塞进文字等于把上下文烧光
- 票根入库跳过 dehydrator.analyze（标签我们已经知道了，是 #观影 加片名，
  没有需要 LLM 判断的东西），并且**绝不与任何旧桶合并**：两场电影被判定
  近似然后合成一条，正好毁掉票根存在的理由
- 连接失败时返回排查指引，不抛裸异常给 MCP 层

不做什么（边界）：
- 不调播放器的 /api/rooms/*（结构上也调不到）
- 不自己解析字幕、不自己判断该不该给某句台词
========================================
"""

import os
import json
import logging
import re
from typing import Optional

logger = logging.getLogger("ombre_brain")

# ============================================================
# 调参面板
# ============================================================
_DEFAULT_BASE = "http://127.0.0.1:18006"      # 本地开发默认（门禁端口）
_HTTP_TIMEOUT_SECONDS = 15.0
_ROOM_ID_RE = re.compile(r"^[A-Z0-9]{6}$")    # 与 store.js 的房间号一致
_TICKET_TAGS = ["观影"]                        # v2 §1.5 规定的标签，再加片名
_TICKET_IMPORTANCE = 6                         # 一场电影不是惊天动地，但值得留着


def _base_url() -> str:
    """每次调用现读。优先级：env > 内嵌子进程的门禁地址 > 本地默认。

    注意这里永远是**门禁端口**：播放器的 /api/rooms/*（能取字幕全文）不在
    那个端口上注册，所以本包在结构上就够不到它。
    """
    explicit = (os.environ.get("FERRIDEO_API_BASE") or "").strip()
    if explicit:
        return explicit.rstrip("/")
    try:
        from web import ferrideo_bridge as _bridge
        return _bridge.internal_gate_base().rstrip("/")
    except Exception:
        return _DEFAULT_BASE


def _conn_help(exc: Exception) -> str:
    base = _base_url()
    return (
        f"观影服务连不上（{base}）：{type(exc).__name__}: {exc}\n"
        f"排查：ferrideo 是 ombre 同服务里的 node 子进程——"
        f"① 看 ombre 日志里 [ferrideo] 行（启动/重启/失败原因，崩溃会自动退避重启）；"
        f"② 子进程日志在 <DATA_DIR>/ferrideo-server.log；"
        f"③ 部署与环境变量见 ferrideo/README.md。"
        f"这期间播放器那边照常能放片，只是你看不到进度。"
    )


async def _request(method: str, path: str, *, params: Optional[dict] = None,
                   json_body: Optional[dict] = None) -> tuple[int, dict]:
    """发一次请求，返回 (status_code, dict)。连接类异常向上抛。"""
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


def _check_room_id(room_id: str) -> Optional[str]:
    """房间号校验。非法时返回给模型看的文案，合法返回 None。"""
    rid = (room_id or "").strip().upper()
    if not _ROOM_ID_RE.match(rid):
        return (
            f"房间号不合法：{room_id!r}（应该是 6 位大写字母数字，比如 K7X2M9，"
            f"不含 0 O 1 I L 这几个容易看错的字符）。问她 iPad 上显示的是什么。"
        )
    return None


def _room_404(room_id: str) -> str:
    return (
        f"没有 {room_id} 这个房间——可能她还没开场，或者房间过期了（12 小时无心跳即回收）。"
        f'要开新的一场就用 ferrideo(action="create_room")。'
    )


# ============================================================
# create_room
# ============================================================
async def create_room(title: Optional[str] = "") -> str:
    try:
        status, data = await _request("POST", "/rooms", json_body={"title": (title or "").strip()})
    except Exception as e:  # noqa: BLE001
        return _conn_help(e)
    if status == 409:
        rooms = data.get("rooms") or []
        lines = [f"开不了新房间：{data.get('error', data)}"]
        if rooms:
            lines.append("现在开着的：")
            lines += [_fmt_room_line(r) for r in rooms]
            lines.append(
                '问她哪个不要了，用 ferrideo(action="close", room_id=...) 关掉再开。'
                "那种「空的、很久没动静」的多半是她测试时开出来的；"
                "有摘录或笔记的关掉前会先存进 letter，不会丢。"
            )
        return "\n".join(lines)
    if status not in (200, 201):
        return f"开场失败（HTTP {status}）：{data.get('error', data)}"

    room_id = data.get("roomId", "?")
    lines = [f"开好了。房间号 {room_id}，片名《{data.get('title') or '未命名'}》。"]

    # 进场地址：拼完整 URL 需要外部域名，而 python 侧只能从走过代理的请求里知道它。
    # 她开过一次页面之后就知道了；之前只能给路径。
    try:
        from web import ferrideo_bridge as _bridge
        url = _bridge.public_player_url()
    except Exception:
        url = ""
    if url:
        lines.append(f"让她在 iPad 上打开：{url}")
    else:
        lines.append(
            "她要在 iPad 上打开播放器。完整地址是她 ombre 的域名加上 "
            f"{_player_path()}（这个服务还没被外面访问过，所以我报不出域名；"
            "她把页面存成主屏图标之后就不用再找了）。"
        )
    lines.append(f"进去之后在「加入房间」里输入 {room_id}，选片、选字幕（字幕可选），点开始。")
    return "\n".join(lines)


def _fmt_room_line(r: dict) -> str:
    """一行房间摘要。让人一眼分得出「测试时丢下的空房间」和「真在看的」。"""
    bits = []
    if r.get("finished"):
        bits.append("已散场" + ("、票根已出" if r.get("hasTicket") else ""))
    idle = r.get("idleMinutes")
    if isinstance(idle, int):
        bits.append("正在看" if idle < 1 else f"{idle} 分钟没动静")
    left = [f"{r[k]} {name}" for k, name in (("quoteCount", "句摘录"), ("noteCount", "条笔记"),
                                             ("danmakuCount", "条弹幕")) if r.get(k)]
    bits.append("、".join(left) if left else "什么都没留下")
    return f"· {r.get('roomId')}《{r.get('title')}》 放到 {r.get('position')}，" + "，".join(bits)


# ============================================================
# list_rooms / close_room
# ============================================================
async def list_rooms() -> str:
    try:
        status, data = await _request("GET", "/rooms")
    except Exception as e:  # noqa: BLE001
        return _conn_help(e)
    if status != 200:
        return f"读不到房间列表（HTTP {status}）：{data.get('error', data)}"
    rooms = data.get("rooms") or []
    if not rooms:
        return '现在一个房间都没开。要开一场就用 ferrideo(action="create_room")。'
    return f"开着 {len(rooms)} 个房间：\n" + "\n".join(_fmt_room_line(r) for r in rooms)


async def close_room(room_id: str) -> str:
    err = _check_room_id(room_id)
    if err:
        return err
    rid = room_id.strip().upper()
    try:
        status, data = await _request("POST", f"/rooms/{rid}/close", json_body={})
    except Exception as e:  # noqa: BLE001
        return _conn_help(e)
    if status == 404:
        return f"没有 {rid} 这个房间，可能已经关掉或过期了。"
    if status != 200:
        return f"没关掉（HTTP {status}）：{data.get('error', data)}"
    out = f"关掉了：{rid}《{data.get('title')}》。"
    if data.get("rescued"):
        out += "里面的摘录和笔记已经先存进 letter 了，不会丢——之后记得补成记忆。"
    return out


def _player_path() -> str:
    try:
        from web import ferrideo_bridge as _bridge
        return f"/ferrideo/{_bridge.web_token()}/"
    except Exception:
        return "/ferrideo/<token>/"


# ============================================================
# get_context —— 最重要的一个
# ============================================================
async def get_context(room_id: str, include_frame: Optional[bool] = None) -> tuple[str, Optional[bytes]]:
    """返回 (给模型看的文字, 帧的 jpeg 二进制 or None)。

    帧不进文字：server.py 把它组装成 image content block。
    """
    err = _check_room_id(room_id)
    if err:
        return err, None
    rid = room_id.strip().upper()
    params = {}
    if include_frame is not None:
        params["includeFrame"] = "1" if include_frame else "0"
    try:
        status, data = await _request("GET", f"/rooms/{rid}/context", params=params or None)
    except Exception as e:  # noqa: BLE001
        return _conn_help(e), None
    if status == 404:
        return _room_404(rid), None
    if status != 200:
        return f"读不到现场（HTTP {status}）：{data.get('error', data)}", None

    state_cn = {"playing": "正在放", "paused": "暂停着", "idle": "没在放"}.get(data.get("state"), data.get("state"))
    lines = [f"《{data.get('title')}》 {state_cn}  {data.get('position')}"]
    if data.get("duration"):
        lines[0] += f" / {data['duration']}（{data.get('progressPercent')}%）"

    if data.get("subtitleMode") == "none":
        lines.append("这一场没有字幕轨。")
    else:
        recent = data.get("recentLines") or []
        if recent:
            lines.append("前几句：" + " / ".join(recent))
        if data.get("currentLine"):
            lines.append(f"这一句：{data['currentLine']}")
        else:
            lines.append("这一刻没有台词。")
        if data.get("nextLineExists"):
            lines.append("（后面还有台词，但你拿不到——门禁在服务端，别试了）")
        else:
            lines.append("（这是最后一句了）")

    extras = []
    if data.get("danmakuCount"):
        extras.append(f"弹幕 {data['danmakuCount']} 条")
    if data.get("quoteCount"):
        extras.append(f"她记下了 {data['quoteCount']} 句")
    if data.get("noteCount"):
        extras.append(f"你写了 {data['noteCount']} 条笔记")
    if extras:
        lines.append("　".join(extras))
    if data.get("finished"):
        lines.append("这一场已经散了（她点过退出）。可以出票根了。")

    frame_bytes = None
    frame = data.get("frame")
    if isinstance(frame, str) and frame.startswith("data:image/jpeg;base64,"):
        import base64
        try:
            frame_bytes = base64.b64decode(frame.split(",", 1)[1])
            lines.append(f"下面这张是 {data.get('frameAt')} 的画面"
                         f"（{data.get('frameAgeSeconds', '?')} 秒前抓的）。")
        except Exception as e:  # noqa: BLE001
            lines.append(f"（有一帧但解不开：{e}）")

    for hint in data.get("hints") or []:
        lines.append("· " + hint)
    return "\n".join(lines), frame_bytes


# ============================================================
# 写入类
# ============================================================
async def send_danmaku(room_id: str, text: str, author: Optional[str] = "") -> str:
    err = _check_room_id(room_id)
    if err:
        return err
    if not (text or "").strip():
        return "弹幕不能是空的。"
    rid = room_id.strip().upper()
    try:
        status, data = await _request(
            "POST", f"/rooms/{rid}/danmaku",
            json_body={"text": text.strip(), "author": (author or "").strip() or "克"},
        )
    except Exception as e:  # noqa: BLE001
        return _conn_help(e)
    if status == 404:
        return _room_404(rid)
    if status not in (200, 201):
        return f"弹幕没发出去（HTTP {status}）：{data.get('error', data)}"
    out = f"发了：{data.get('text')}（署名 {data.get('author')}）"
    if data.get("truncated"):
        out += "\n超过 60 字被截断了——弹幕是飘过去的，写短点。"
    out += "\n3 秒内会浮上她的画面。她要是不在前台（切出去了/锁屏了），会等她回来再上屏。"
    return out


async def request_frame(room_id: str) -> str:
    err = _check_room_id(room_id)
    if err:
        return err
    rid = room_id.strip().upper()
    try:
        status, data = await _request("POST", f"/rooms/{rid}/frame-request", json_body={})
    except Exception as e:  # noqa: BLE001
        return _conn_help(e)
    if status == 404:
        return _room_404(rid)
    if status not in (200, 202):
        return f"要帧失败（HTTP {status}）：{data.get('error', data)}"
    return data.get("tip") or '已经跟播放器要了一帧，等 3-5 秒后用 ferrideo(action="context", include_frame=True) 取。'


async def add_note(room_id: str, text: str) -> str:
    err = _check_room_id(room_id)
    if err:
        return err
    if not (text or "").strip():
        return "笔记不能是空的。"
    rid = room_id.strip().upper()
    try:
        status, data = await _request("POST", f"/rooms/{rid}/note", json_body={"text": text.strip()})
    except Exception as e:  # noqa: BLE001
        return _conn_help(e)
    if status == 404:
        return _room_404(rid)
    if status not in (200, 201):
        return f"笔记没记上（HTTP {status}）：{data.get('error', data)}"
    return f"记下了（{data.get('at')}）：{data.get('text')}"


# ============================================================
# get_room
# ============================================================
async def get_room(room_id: str) -> str:
    err = _check_room_id(room_id)
    if err:
        return err
    rid = room_id.strip().upper()
    try:
        status, data = await _request("GET", f"/rooms/{rid}/room")
    except Exception as e:  # noqa: BLE001
        return _conn_help(e)
    if status == 404:
        return _room_404(rid)
    if status != 200:
        return f"读不到房间（HTTP {status}）：{data.get('error', data)}"

    lines = [
        f"《{data.get('title')}》 房间 {data.get('roomId')}",
        f"开场 {data.get('createdAt')}　现在 {data.get('position')}"
        + (f" / {data['duration']}" if data.get("duration") else ""),
    ]
    if data.get("finished"):
        lines.append(f"已散场，实际看了 {data.get('watchedFor') or '不详'}")
    if data.get("subtitleMode") == "none":
        lines.append("这一场没有字幕轨（要知道台词只能抓帧看画面）")

    quotes = data.get("quotes") or []
    if quotes:
        lines.append(f"\n她记下的台词（{len(quotes)} 句）：")
        lines += [f"· [{q.get('at')}] {q.get('text')}" for q in quotes]
    notes = data.get("notes") or []
    if notes:
        lines.append(f"\n笔记（{len(notes)} 条）：")
        lines += [f"· [{n.get('at')}] {n.get('text')}" for n in notes]
    danmaku = data.get("danmaku") or []
    if danmaku:
        lines.append(f"\n弹幕（{len(danmaku)} 条，按时间）：")
        lines += [f"· {d.get('author')}：{d.get('text')}" for d in danmaku]
    if not (quotes or notes or danmaku):
        lines.append("\n这一场还什么都没留下。")
    if data.get("ticket"):
        lines.append("\n票根已经出过了。")
    return "\n".join(lines)


# ============================================================
# generate_ticket —— 出票根，并直接进记忆库
# ============================================================
async def generate_ticket(room_id: str, mood: Optional[str] = "", note: Optional[str] = "") -> str:
    err = _check_room_id(room_id)
    if err:
        return err
    rid = room_id.strip().upper()
    try:
        status, ticket = await _request(
            "POST", f"/rooms/{rid}/ticket",
            json_body={"mood": (mood or "").strip(), "note": (note or "").strip()},
        )
    except Exception as e:  # noqa: BLE001
        return _conn_help(e)
    if status == 404:
        return _room_404(rid)
    if status != 200:
        return f"票根生成失败（HTTP {status}）：{ticket.get('error', ticket)}"

    body = _format_ticket(ticket)
    bucket_id, how = await _persist_ticket(ticket, body)
    if bucket_id:
        return f"{body}\n\n已经存成一条 #观影 记忆（桶 {bucket_id}）。"
    return (
        f"{body}\n\n"
        f"⚠️ 票根**还没进记忆库**（{how}）。已经存进 letter 降级通道了，"
        f"但那只是中转不是终点——回头用 hold 把上面这张票根补成一条记忆，别让它停在信里。"
    )


def _format_ticket(t: dict) -> str:
    """票根排版。给人看也给记忆库存同一份。"""
    lines = [f"《{t.get('title')}》 {t.get('date')}"]
    dur = t.get("duration")
    watched = t.get("watchedFor")
    if dur or watched:
        lines.append(f"片长 {dur or '不详'}，看了 {watched or '不详'}")
    quotes = t.get("quotes") or []
    if quotes:
        lines.append("\n她记下的台词：")
        lines += [f"· {q}" for q in quotes]
    notes = t.get("notes") or []
    if notes:
        lines.append("\n看的时候写的：")
        lines += [f"· {n}" for n in notes]
    if t.get("danmakuCount"):
        lines.append(f"\n一路说了 {t['danmakuCount']} 句话。")
    if t.get("mood"):
        lines.append(f"那一晚的感觉：{t['mood']}")
    if t.get("note"):
        lines.append(f"附言：{t['note']}")
    return "\n".join(lines)


async def _persist_ticket(ticket: dict, body: str) -> tuple[str, str]:
    """把票根存成一条记忆。返回 (bucket_id, 说明)；失败时 bucket_id 为空。

    **跳过 LLM 打标**：标签我们已经知道了（#观影 + 片名），没有需要判断的东西，
    也就不存在「打标 API 限流导致票根丢了」这条失败路径。
    **绝不合并**：直接建桶，不走 hold 的近似合并——两场电影合成一条就毁了票根。
    """
    title = (ticket.get("title") or "未命名").strip()
    content = f"看了《{title}》。\n\n{body}"
    try:
        from .. import _runtime as rt
        bucket_id = await rt.bucket_mgr.create(
            content=content,
            tags=_TICKET_TAGS + [title],
            importance=_TICKET_IMPORTANCE,
            name=f"观影：{title}",
            source_tool="ferrideo",
            why_remembered="一场看完的电影，票根由 ferrideo 生成",
        )
        return bucket_id, ""
    except Exception as e:  # noqa: BLE001
        reason = f"{type(e).__name__}: {e}"
        logger.warning("[ferrideo] 票根入库失败，走 letter 降级：%s", reason)
    # 降级通道：宁可存成一封信，也不能让一场看完的电影无声无息没了
    try:
        from ..plan import core as _t_plan
        await _t_plan.letter_write(
            author="ai",
            content=f"这张票根还没能进记忆库（{reason}），先放在这里。\n\n{body}",
            title=f"未入库的票根：{title}",
        )
        return "", f"{reason}；已存进 letter"
    except Exception as e2:  # noqa: BLE001
        logger.error("[ferrideo] 票根连 letter 都写不进去：%s", e2)
        return "", f"{reason}；letter 也写不进去（{type(e2).__name__}），票根只在上面这段文字里，务必自己留下来"
