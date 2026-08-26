"""
========================================
web/ferrideo_bridge.py — ferrideo 观影后端：子进程托管 + 反向代理
========================================

把仓库内 ferrideo/（Node 服务）作为**子进程**跑在 ombre-brain 同一个
Render 服务里，共用同一持久盘。形状与 web/reading_bridge.py 完全一致
——那套（子进程怎么起、网页怎么暴露、token 怎么走）已经在线上跑通，
这里照抄，不发明新的。

关键行为：
- 子进程：node <app_dir>/server.js，绑 127.0.0.1:FERRIDEO_INTERNAL_PORT
  （默认 18005，read-along 占 18004），不对外暴露
- 数据：DATA_DIR 默认 <buckets_dir>/ferrideo —— 帧和房间快照落在这里
- token：优先 env FERRIDEO_WEB_TOKEN；否则首启生成并持久化到
  <DATA_DIR>/.web-token（重启不变，iPad 上的书签不会失效）。与 read-along
  的 token 各生成各的，互不影响
- 播放器页面热更新：播放器在 frontend/ferrideo/index.html（跟着 entrypoint
  的 src/+frontend/ 播种走），这里把候选目录用 FERRIDEO_PAGE_DIRS 注入子进程，
  node 每次请求现读。改页面 → do-update → iPad 刷新即生效，不用重建镜像；
  ferrideo/ 里的服务端代码仍然走镜像重建（与 read-along 同）
- 崩溃自愈：监控循环发现子进程退出即重启，指数退避 1→60s 封顶，稳定
  运行 60s 后复位；node 不存在/反复失败只降级 warning，
  **绝不拖垮 ombre 主服务**（ferrideo 是三个住户里最不重要的一个）
- 反向代理：/ferrideo/{任意路径} → http://127.0.0.1:<port>/{任意路径}，
  请求/响应双向流式；token 门禁语义在 ferrideo 服务端原样保留
  （无 token / 错 token 均 404 且不可区分），本模块不做任何鉴权判断

不做什么（边界）：
- 不改 ombre / read-along 的任何现有行为
- 不缓存/解析代理流量内容，只转发
- 不碰记忆桶

对外暴露：register(mcp) / ensure_child_on_boot() / stop_child() /
         web_token() / internal_gate_base() / data_dir() / drain_rescues() / status()
========================================
"""

import os
import json
import asyncio
import logging
import secrets
import shutil
import subprocess
import time

from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

logger = logging.getLogger("ombre_brain")

# ============================================================
# 调参面板
# ============================================================
_DEFAULT_INTERNAL_PORT = 18005      # 播放器端口（127.0.0.1）；18004 是 read-along
_DEFAULT_GATE_PORT = 18006          # AI 门禁端口；只挂 gate 路由，**不经过反向代理**
_RESCUE_FILE = "rescued.jsonl"      # 子进程淘汰房间前抢救出来的摘录/笔记
_RESCUE_DRAIN_SECONDS = 60.0        # 排空检查间隔（监控循环里顺带做）
_PUBLIC_PREFIX = "/ferrideo"        # 对外路径前缀：/ferrideo/<token>/...
_TOKEN_BYTES = 16                   # 自动生成 token 的熵（token_urlsafe → ~22 字符）
_BACKOFF_START_SECONDS = 1.0        # 崩溃重启退避起点
_BACKOFF_MAX_SECONDS = 60.0         # 退避封顶
_STABLE_RESET_SECONDS = 60.0        # 子进程存活超过该时长后退避复位
_MONITOR_POLL_SECONDS = 1.0         # 监控循环轮询间隔
_PROXY_CONNECT_TIMEOUT = 5.0        # 环回连接超时
_PROXY_READ_TIMEOUT = 60.0          # 读超时（ferrideo 没有传大文件的场景，60s 够）

# 逐跳头：代理不透传（RFC 7230 §6.1），host 也要重算
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}

# ============================================================
# 模块状态
# ============================================================
_child_proc: "subprocess.Popen | None" = None
_child_started_at: float = 0.0
_monitor_task: "asyncio.Task | None" = None
_managed = False                    # False = stop_child 后不再拉起
_last_spawn_error = ""
_proxy_client = None                # 惰性创建的 httpx.AsyncClient（连接复用）
_proxy_client_loop = None           # 创建该 client 的事件循环（换循环要重建）
_token_cache = ""


# ============================================================
# 路径 / 配置 helper
# ============================================================
# Dockerfile 的 WORKDIR。entrypoint 的持久卷热更新只播种 src/ + frontend/ 到
# CODE_DIR（<buckets>/_app），副本里**没有** ferrideo/，所以 repo_root 指向
# 代码副本时必须回退到镜像内置路径——线上就是这个形态（与 read-along 同）。
_IMAGE_APP_DIR = "/app/ferrideo"
_IMAGE_ROOT = "/app"


def _app_dir_candidates() -> list[str]:
    """server.js 的候选目录，按优先级排列（去重保序）。"""
    from . import _shared as sh
    cands = []
    if sh.repo_root:
        cands.append(os.path.join(sh.repo_root, "ferrideo"))
    file_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    cands.append(os.path.join(file_root, "ferrideo"))
    cands.append(_IMAGE_APP_DIR)
    seen: set = set()
    out = []
    for c in cands:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _app_dir() -> str:
    """ferrideo 代码目录。

    - env FERRIDEO_APP_DIR：显式指定，**不**回退扫描（配错了让 _spawn 直说）
    - 否则取候选链里第一个真的含 server.js 的目录
    - 都没有则返回首选候选，_spawn 会把完整候选清单写进错误信息
    """
    explicit = (os.environ.get("FERRIDEO_APP_DIR") or "").strip()
    if explicit:
        return os.path.abspath(explicit)
    cands = _app_dir_candidates()
    for c in cands:
        if os.path.isfile(os.path.join(c, "server.js")):
            return c
    return cands[0]


def _page_dirs() -> list[str]:
    """播放器页面（index.html）的候选目录，按优先级；注入给子进程。

    热更新目录排在最前：frontend/ 会被 entrypoint 播种到 <buckets>/_app/frontend，
    repo_root 正指向那里，所以改完 do-update 就生效。后面两个是没播种成功时
    的兜底（镜像内的 /app/frontend/ferrideo）。node 侧还会在链尾补自己的 public/。
    """
    from . import _shared as sh
    cands = []
    if sh.repo_root:
        cands.append(os.path.join(sh.repo_root, "frontend", "ferrideo"))
    file_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    cands.append(os.path.join(file_root, "frontend", "ferrideo"))
    cands.append(os.path.join(_IMAGE_ROOT, "frontend", "ferrideo"))
    seen: set = set()
    out = []
    for c in cands:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def data_dir() -> str:
    """数据目录：env FERRIDEO_DATA_DIR > <buckets_dir>/ferrideo。
    帧、房间快照、子进程日志都在这里。这块盘是跟 ombre 和 read-along 共用的。"""
    explicit = (os.environ.get("FERRIDEO_DATA_DIR") or "").strip()
    if explicit:
        return os.path.abspath(explicit)
    from . import _shared as sh
    base = (sh.config or {}).get("buckets_dir") or "buckets"
    return os.path.join(base, "ferrideo")


def _internal_port() -> int:
    try:
        return int(os.environ.get("FERRIDEO_INTERNAL_PORT") or _DEFAULT_INTERNAL_PORT)
    except (TypeError, ValueError):
        return _DEFAULT_INTERNAL_PORT


def web_token() -> str:
    """访问 token：env FERRIDEO_WEB_TOKEN > <DATA_DIR>/.web-token（无则生成并持久化）。

    token 只允许 URL 安全字符（ferrideo 服务端同样校验，不合法会拒绝启动）。
    """
    global _token_cache
    env_token = (os.environ.get("FERRIDEO_WEB_TOKEN") or "").strip()
    if env_token:
        return env_token
    if _token_cache:
        return _token_cache
    token_file = os.path.join(data_dir(), ".web-token")
    try:
        with open(token_file, "r", encoding="utf-8") as f:
            saved = f.read().strip()
        if saved:
            _token_cache = saved
            return saved
    except OSError:
        pass
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    os.makedirs(data_dir(), exist_ok=True)
    tmp = token_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(token)
    os.replace(tmp, token_file)
    try:
        os.chmod(token_file, 0o600)
    except OSError:
        pass
    _token_cache = token
    logger.info("[ferrideo] 生成新的访问 token 并持久化到 %s", token_file)
    return token


def _gate_port() -> int:
    try:
        return int(os.environ.get("FERRIDEO_GATE_PORT") or _DEFAULT_GATE_PORT)
    except (TypeError, ValueError):
        return _DEFAULT_GATE_PORT


def internal_gate_base() -> str:
    """MCP 工具唯一的后端地址：门禁端口上的 /api/gate 前缀。

    **注意这里指的是 gate 端口，不是播放器端口。** 防剧透门禁不是靠
    「python 侧不去调 /api/rooms/:id/subtitle」这个约定成立的——那种约定
    会在某个赶时间的深夜被绕过去。子进程把 gate 单独跑在一个 app 上，
    播放器的路由压根不注册在那个端口，所以 MCP 侧就算把路径拼成
    ../rooms/xxx/subtitle 也够不到。结构上做不到，不是自觉。

    这个端口不经过 /ferrideo 反向代理，公网不可达。
    """
    return f"http://127.0.0.1:{_gate_port()}/{web_token()}/api/gate"


def _child_env() -> dict:
    """子进程环境。纯函数便于测试。

    - 剔除 PORT：那是 Render 注入给 ombre 主服务的，透传会让 node 绑 0.0.0.0:$PORT 抢主端口
    - FERRIDEO_PAGE_DIRS：播放器页面候选目录（冒号分隔），让页面能热更新
    """
    env = dict(os.environ)
    env.pop("PORT", None)
    env["FERRIDEO_PORT"] = str(_internal_port())
    env["FERRIDEO_GATE_PORT"] = str(_gate_port())
    env["DATA_DIR"] = data_dir()
    env["FERRIDEO_WEB_TOKEN"] = web_token()
    env["FERRIDEO_PUBLIC_PREFIX"] = _PUBLIC_PREFIX
    env["FERRIDEO_PAGE_DIRS"] = ":".join(_page_dirs())
    return env


# ============================================================
# 抢救排空：rescued.jsonl → letter 降级通道
# ============================================================
# 子进程在淘汰一个「已散场、还没出票根、但有摘录/笔记」的房间之前，
# 会先把那些内容 append+fsync 进 <DATA_DIR>/rescued.jsonl，写不成就不淘汰。
# 这里负责把它排空成一封 letter——那是她在电影里挑出来的句子，
# 不能因为一天看了六部片子就无声无息没了。降级可以，丢失不行。


def _rescue_path() -> str:
    return os.path.join(data_dir(), _RESCUE_FILE)


def _format_rescue(rec: dict) -> str:
    """把一条抢救记录写成人能读的信。"""
    def _hms(ms) -> str:
        total = max(0, int((ms or 0) // 1000))
        return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"

    lines = [
        f"《{rec.get('title') or '未命名'}》这一场没有生成票根，房间被回收了。",
        "以下是当时留下的东西，先存在这里，等你把它补成一条记忆。",
        "",
    ]
    quotes = rec.get("quotes") or []
    notes = rec.get("notes") or []
    if quotes:
        lines.append("她记下的台词：")
        lines += [f"· [{_hms(q.get('positionMs'))}] {q.get('text', '')}" for q in quotes]
        lines.append("")
    if notes:
        lines.append("看的时候写的笔记：")
        lines += [f"· [{_hms(n.get('positionMs'))}] {n.get('text', '')}" for n in notes]
        lines.append("")
    lines.append(f"（房间 {rec.get('roomId')}，散场于 {rec.get('finishedAt') or '未知'}）")
    return "\n".join(lines)


async def drain_rescues() -> int:
    """把 rescued.jsonl 排空成 letter。返回成功写入的条数。

    先 rename 再处理：排空过程中子进程还能继续往新文件里追加，不会打架。
    单条写失败就把它追加回去，下一轮再试——绝不因为一条失败丢掉其余的。
    """
    src = _rescue_path()
    if not os.path.isfile(src) or os.path.getsize(src) == 0:
        return 0
    staging = f"{src}.draining"
    try:
        os.replace(src, staging)
    except OSError as e:
        logger.warning("[ferrideo] 抢救文件改名失败：%s", e)
        return 0

    try:
        with open(staging, "r", encoding="utf-8") as f:
            raw_lines = [ln for ln in f.read().splitlines() if ln.strip()]
    except OSError as e:
        logger.warning("[ferrideo] 抢救文件读不了：%s", e)
        return 0

    from tools.plan import core as _t_plan
    written, leftover = 0, []
    for ln in raw_lines:
        try:
            rec = json.loads(ln)
        except (ValueError, TypeError):
            logger.warning("[ferrideo] 抢救记录解析不了，丢弃这一行：%.80s", ln)
            continue        # 坏行没有重试价值，扔掉但要留痕
        try:
            await _t_plan.letter_write(
                author="ai",
                content=_format_rescue(rec),
                title=f"未成票根：{rec.get('title') or '未命名'}",
            )
            written += 1
        except Exception as e:  # noqa: BLE001 — 写不进去就留着下轮重试
            logger.warning("[ferrideo] 抢救记录写 letter 失败，留到下一轮：%s", e)
            leftover.append(ln)

    if leftover:
        try:
            with open(src, "a", encoding="utf-8") as f:
                f.write("\n".join(leftover) + "\n")
        except OSError as e:
            logger.error("[ferrideo] 抢救记录回写失败，这些内容有丢失风险：%s", e)
    try:
        os.remove(staging)
    except OSError:
        pass
    if written:
        logger.info("[ferrideo] 已把 %d 条被回收房间的摘录/笔记存进 letter", written)
    return written


# ============================================================
# 子进程生命周期
# ============================================================
def _spawn() -> "subprocess.Popen | None":
    """拉起 node 子进程。失败返回 None（原因记 _last_spawn_error）。"""
    global _last_spawn_error
    node = shutil.which("node")
    if not node:
        _last_spawn_error = "node 不在 PATH：确认部署镜像装了 Node.js ≥ 18（见 Dockerfile）"
        return None
    app = _app_dir()
    server_js = os.path.join(app, "server.js")
    if not os.path.isfile(server_js):
        tried = os.environ.get("FERRIDEO_APP_DIR", "").strip() or "、".join(_app_dir_candidates())
        _last_spawn_error = (
            f"找不到 ferrideo/server.js，已尝试：{tried}"
            f"（镜像里应在 {_IMAGE_APP_DIR}；也可用 FERRIDEO_APP_DIR 显式指定）"
        )
        return None
    if not os.path.isdir(os.path.join(app, "node_modules")):
        # 依赖没装只警告不拦：让子进程自己起、自己报错，日志里看得见
        logger.warning("[ferrideo] %s/node_modules 不存在——构建时应执行 npm install（见 Dockerfile）", app)
    os.makedirs(data_dir(), exist_ok=True)
    log_path = os.path.join(data_dir(), "ferrideo-server.log")
    try:
        log_f = open(log_path, "ab")
        return subprocess.Popen(
            [node, "server.js"],
            cwd=app,
            env=_child_env(),
            stdout=log_f,
            stderr=subprocess.STDOUT,
        )
    except Exception as e:
        _last_spawn_error = f"{type(e).__name__}: {e}"
        return None


async def _monitor() -> None:
    """监控循环：子进程退出 → 指数退避重启；稳定运行 60s 后退避复位。
    顺带每分钟排空一次抢救文件（被回收房间的摘录/笔记 → letter）。"""
    global _child_proc, _child_started_at
    backoff = _BACKOFF_START_SECONDS
    last_drain = 0.0
    while _managed:
        if time.monotonic() - last_drain >= _RESCUE_DRAIN_SECONDS:
            last_drain = time.monotonic()
            try:
                await drain_rescues()
            except Exception as e:  # noqa: BLE001 — 排空失败不影响子进程托管
                logger.warning("[ferrideo] 排空抢救文件出错：%s", e)
        proc = _child_proc
        if proc is not None and proc.poll() is None:
            if backoff > _BACKOFF_START_SECONDS and time.monotonic() - _child_started_at >= _STABLE_RESET_SECONDS:
                backoff = _BACKOFF_START_SECONDS
            await asyncio.sleep(_MONITOR_POLL_SECONDS)
            continue
        if proc is not None:
            logger.warning("[ferrideo] 子进程退出（code=%s），%.0fs 后重启", proc.poll(), backoff)
        await asyncio.sleep(backoff)
        if not _managed:  # 退避睡眠期间可能被 stop_child
            return
        backoff = min(backoff * 2, _BACKOFF_MAX_SECONDS)
        _child_proc = _spawn()
        _child_started_at = time.monotonic()
        if _child_proc is None:
            logger.warning("[ferrideo] 重启失败：%s", _last_spawn_error)
        else:
            logger.info("[ferrideo] 子进程已重启 pid=%s", _child_proc.pid)


async def ensure_child_on_boot() -> None:
    """server.py lifespan 启动钩子。任何失败只 warning，绝不抛（不拖垮 ombre）。"""
    global _child_proc, _child_started_at, _monitor_task, _managed
    try:
        if _child_proc is not None and _child_proc.poll() is None:
            return
        _managed = True
        _child_proc = _spawn()
        _child_started_at = time.monotonic()
        if _child_proc is None:
            logger.warning("[ferrideo] 观影子进程未启动：%s（ombre 其余功能不受影响）", _last_spawn_error)
        else:
            logger.info(
                "[ferrideo] 观影子进程已启动 pid=%s port=%s app=%s data=%s（播放器：%s/<token>/）",
                _child_proc.pid, _internal_port(), _app_dir(), data_dir(), _PUBLIC_PREFIX,
            )
        if _monitor_task is None or _monitor_task.done():
            _monitor_task = asyncio.get_event_loop().create_task(_monitor())
    except Exception as e:  # noqa: BLE001 — 启动期兜底
        logger.warning("[ferrideo] 观影子进程启动异常已忽略：%s", e)


async def stop_child() -> None:
    """server.py lifespan 关停钩子。"""
    global _child_proc, _monitor_task, _managed, _proxy_client
    _managed = False
    if _monitor_task is not None:
        _monitor_task.cancel()
        _monitor_task = None
    proc = _child_proc
    _child_proc = None
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            await asyncio.get_event_loop().run_in_executor(None, proc.wait, 5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    if _proxy_client is not None:
        try:
            await _proxy_client.aclose()
        except Exception:
            pass
        _proxy_client = None


def status() -> dict:
    """给日志/诊断用的状态快照。"""
    proc = _child_proc
    return {
        "running": bool(proc is not None and proc.poll() is None),
        "pid": proc.pid if proc is not None else None,
        "port": _internal_port(),
        "data_dir": data_dir(),
        "last_error": _last_spawn_error,
    }


# ============================================================
# 反向代理
# ============================================================
def _client():
    """连接复用的 AsyncClient。事件循环变了（测试/嵌入场景）就丢弃旧的重建。"""
    global _proxy_client, _proxy_client_loop
    loop = asyncio.get_running_loop()
    if _proxy_client is None or _proxy_client_loop is not loop:
        import httpx
        _proxy_client = httpx.AsyncClient(
            timeout=httpx.Timeout(_PROXY_READ_TIMEOUT, connect=_PROXY_CONNECT_TIMEOUT),
        )
        _proxy_client_loop = loop
    return _proxy_client


def register(mcp) -> None:
    """注册 /ferrideo/{path} 反向代理。token 校验完全交给 ferrideo 服务端
    （无/错 token 一律 404 且与路径不存在不可区分），这里只做转发。"""

    @mcp.custom_route(_PUBLIC_PREFIX + "/{rest:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
    async def ferrideo_proxy(request: Request):
        import httpx
        rest = request.path_params.get("rest", "")
        target = f"http://127.0.0.1:{_internal_port()}/{rest}"
        if request.url.query:
            target += f"?{request.url.query}"
        headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}
        # 只有可能带 body 的方法才透传请求流（GET/HEAD 带 chunked 空流会让部分服务端困惑）
        body = request.stream() if request.method in ("POST", "PUT", "PATCH") else None
        try:
            upstream = _client().build_request(
                request.method, target, headers=headers, content=body,
            )
            resp = await _client().send(upstream, stream=True)
        except httpx.HTTPError as e:
            logger.warning("[ferrideo] 代理转发失败：%s", e)
            return JSONResponse(
                {"error": "ferrideo service unavailable（观影子进程未就绪，稍后再试）"},
                status_code=502,
            )
        resp_headers = {k: v for k, v in resp.headers.items() if k.lower() not in _HOP_BY_HOP}
        return StreamingResponse(
            resp.aiter_raw(),
            status_code=resp.status_code,
            headers=resp_headers,
            background=BackgroundTask(resp.aclose),
        )
