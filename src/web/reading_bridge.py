"""
========================================
web/reading_bridge.py — read-along 共读后端：子进程托管 + 反向代理
========================================

把仓库内 read-along/（Node 服务）作为**子进程**跑在 ombre-brain 同一个
Render 服务里，共用同一持久盘，零新增费用。取代早前「独立 Web Service」方案。

关键行为：
- 子进程：node <repo_root>/read-along/server.js，绑定 127.0.0.1 的内部端口
  （READING_INTERNAL_PORT，默认 18004），不对外暴露
- 数据：DATA_DIR 默认 <buckets_dir>/read-along —— buckets_dir 即持久盘挂载点，
  书/进度/批注随盘持久（github_sync 只同步 .md，这些 JSON/epub 不会被传上 GitHub，
  与 peek/、night_fall/ 同款「持久盘子目录」先例）
- token：优先 env READING_WEB_TOKEN；否则首启生成并持久化到 <DATA_DIR>/.web-token
  （重启不变，手机书签和 MCP base 不会失效）
- 崩溃自愈：监控循环发现子进程退出即重启，指数退避 1→2→4→…→60s 封顶，
  子进程稳定运行 60s 后退避复位；node 不存在/反复失败只降级 warning，
  **绝不拖垮 ombre 主服务**
- 反向代理：/reading/{任意路径} → http://127.0.0.1:<port>/{任意路径}，
  请求/响应双向流式（50MB 传书、封面图都过）；token 门禁语义在 read-along
  服务端原样保留（无 token / 错 token 均 404 且不可区分），本模块不做任何鉴权判断
- MCP 工具接线：子进程起来后若 READING_API_BASE 未设置，写入
  http://127.0.0.1:<port>/<token>（进程内环回，不出公网）；已设置则尊重用户配置
- DRY-RUN 保障：构造子进程环境时**主动剔除** READING_PUSH_ENABLED /
  READING_PUSH_WEBHOOK / PORT（PORT 是 Render 注入给 ombre 的，透传会让
  子进程抢 0.0.0.0 主端口）

不做什么（边界）：
- 不改 read-along 的门禁/推送逻辑（那在 read-along/ 里）
- 不缓存/解析代理流量内容，只转发

对外暴露：register(mcp) / ensure_child_on_boot() / stop_child() /
         web_token() / internal_base() / status()
========================================
"""

import os
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
_DEFAULT_INTERNAL_PORT = 18004      # 子进程监听端口（127.0.0.1）
_PUBLIC_PREFIX = "/reading"         # 对外路径前缀：/reading/<token>/...
_TOKEN_BYTES = 16                   # 自动生成 token 的熵（token_urlsafe → ~22 字符）
_BACKOFF_START_SECONDS = 1.0        # 崩溃重启退避起点
_BACKOFF_MAX_SECONDS = 60.0         # 退避封顶
_STABLE_RESET_SECONDS = 60.0        # 子进程存活超过该时长后退避复位
_MONITOR_POLL_SECONDS = 1.0         # 监控循环轮询间隔
_PROXY_CONNECT_TIMEOUT = 5.0        # 环回连接超时
_PROXY_READ_TIMEOUT = 300.0         # 读超时（50MB 传书 + epub 解析要留足）

# 逐跳头：代理不透传（RFC 7230 §6.1），host 也要重算
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}

# ============================================================
# 模块状态（类比 ollama_local 的子进程三件套）
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
# Dockerfile 的 WORKDIR（镜像内置副本的固定位置）。entrypoint 的持久卷热更新
# 只播种 src/ + frontend/ 到 CODE_DIR（<buckets>/_app）并从那里运行——此时
# repo_root / __file__ 都指向代码副本，副本里**没有** read-along/，必须回退到
# 镜像内置路径。read-along 的更新随镜像重建走，不参与 do-update 热更新。
_IMAGE_APP_DIR = "/app/read-along"


def _app_dir_candidates() -> list[str]:
    """server.js 的候选目录，按优先级排列（去重保序）。"""
    from . import _shared as sh
    cands = []
    if sh.repo_root:
        cands.append(os.path.join(sh.repo_root, "read-along"))
    file_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    cands.append(os.path.join(file_root, "read-along"))
    cands.append(_IMAGE_APP_DIR)
    seen: set = set()
    out = []
    for c in cands:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _app_dir() -> str:
    """read-along 代码目录。

    - env READING_APP_DIR：显式指定，**不**回退扫描（配错了让 _spawn 的报错
      直说，而不是静默用别的目录）
    - 否则取候选链里第一个真的含 server.js 的目录（repo_root 可能指向持久盘
      上的代码副本 _app/，那里没有 read-along —— 线上 Render 就是这个形态，
      要能落到镜像内置的 /app/read-along）
    - 都没有则返回首选候选，_spawn 会把完整候选清单写进错误信息
    """
    explicit = (os.environ.get("READING_APP_DIR") or "").strip()
    if explicit:
        return os.path.abspath(explicit)
    cands = _app_dir_candidates()
    for c in cands:
        if os.path.isfile(os.path.join(c, "server.js")):
            return c
    return cands[0]


def _data_dir() -> str:
    """数据目录：env READING_DATA_DIR > <buckets_dir>/read-along。
    buckets_dir 是持久盘挂载点（Render disk / Docker 卷），书和进度随盘持久。"""
    explicit = (os.environ.get("READING_DATA_DIR") or "").strip()
    if explicit:
        return os.path.abspath(explicit)
    from . import _shared as sh
    base = (sh.config or {}).get("buckets_dir") or "buckets"
    return os.path.join(base, "read-along")


def _internal_port() -> int:
    try:
        return int(os.environ.get("READING_INTERNAL_PORT") or _DEFAULT_INTERNAL_PORT)
    except (TypeError, ValueError):
        return _DEFAULT_INTERNAL_PORT


def web_token() -> str:
    """访问 token：env READING_WEB_TOKEN > <DATA_DIR>/.web-token（无则生成并持久化）。

    token 只允许 URL 安全字符（read-along 服务端同样校验，不合法会拒绝启动）。
    """
    global _token_cache
    env_token = (os.environ.get("READING_WEB_TOKEN") or "").strip()
    if env_token:
        return env_token
    if _token_cache:
        return _token_cache
    token_file = os.path.join(_data_dir(), ".web-token")
    try:
        with open(token_file, "r", encoding="utf-8") as f:
            saved = f.read().strip()
        if saved:
            _token_cache = saved
            return saved
    except OSError:
        pass
    token = secrets.token_urlsafe(_TOKEN_BYTES)
    os.makedirs(_data_dir(), exist_ok=True)
    tmp = token_file + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(token)
    os.replace(tmp, token_file)
    try:
        os.chmod(token_file, 0o600)
    except OSError:
        pass
    _token_cache = token
    logger.info("[reading] 生成新的访问 token 并持久化到 %s", token_file)
    return token


def internal_base() -> str:
    """MCP 工具用的内部地址：http://127.0.0.1:<port>/<token>（进程内环回）。"""
    return f"http://127.0.0.1:{_internal_port()}/{web_token()}"


def _child_env() -> dict:
    """子进程环境。纯函数便于测试。

    - 剔除 PORT：那是 Render 注入给 ombre 主服务的，透传会让 node 绑 0.0.0.0:$PORT 抢主端口
    - 剔除两个推送开关：DRY-RUN 是本部署的硬要求，推送只写 outbox.log
    - READING_PUBLIC_PREFIX：让 reader.html 里的 API 常量带上 /reading 代理前缀
    """
    env = dict(os.environ)
    for k in ("PORT", "READING_PUSH_ENABLED", "READING_PUSH_WEBHOOK"):
        env.pop(k, None)
    env["READING_PORT"] = str(_internal_port())
    env["DATA_DIR"] = _data_dir()
    env["READING_WEB_TOKEN"] = web_token()
    env["READING_PUBLIC_PREFIX"] = _PUBLIC_PREFIX
    return env


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
        tried = os.environ.get("READING_APP_DIR", "").strip() or "、".join(_app_dir_candidates())
        _last_spawn_error = (
            f"找不到 read-along/server.js，已尝试：{tried}"
            f"（镜像里应在 {_IMAGE_APP_DIR}；也可用 READING_APP_DIR 显式指定）"
        )
        return None
    if not os.path.isdir(os.path.join(app, "node_modules")):
        # 依赖没装只警告不拦：epub 解析等会在用到时报错，txt 导入等纯内置功能仍可用
        logger.warning("[reading] %s/node_modules 不存在——构建时应执行 npm install（见 Dockerfile）", app)
    os.makedirs(_data_dir(), exist_ok=True)
    log_path = os.path.join(_data_dir(), "reading-server.log")
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


def _wire_api_base() -> None:
    """子进程起来后给 MCP 工具接线：READING_API_BASE 未设时写内部环回地址。
    用户显式设置过（比如仍想走外部实例）则不动。"""
    if not (os.environ.get("READING_API_BASE") or "").strip():
        os.environ["READING_API_BASE"] = internal_base()
        logger.info("[reading] READING_API_BASE → %s（内部环回，不出公网）", internal_base())


async def _monitor() -> None:
    """监控循环：子进程退出 → 指数退避重启；稳定运行 60s 后退避复位。"""
    global _child_proc, _child_started_at
    backoff = _BACKOFF_START_SECONDS
    while _managed:
        proc = _child_proc
        if proc is not None and proc.poll() is None:
            if backoff > _BACKOFF_START_SECONDS and time.monotonic() - _child_started_at >= _STABLE_RESET_SECONDS:
                backoff = _BACKOFF_START_SECONDS
            await asyncio.sleep(_MONITOR_POLL_SECONDS)
            continue
        if proc is not None:
            logger.warning("[reading] 子进程退出（code=%s），%.0fs 后重启", proc.poll(), backoff)
        await asyncio.sleep(backoff)
        if not _managed:  # 退避睡眠期间可能被 stop_child
            return
        backoff = min(backoff * 2, _BACKOFF_MAX_SECONDS)
        _child_proc = _spawn()
        _child_started_at = time.monotonic()
        if _child_proc is None:
            logger.warning("[reading] 重启失败：%s", _last_spawn_error)
        else:
            logger.info("[reading] 子进程已重启 pid=%s", _child_proc.pid)


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
            logger.warning("[reading] 共读子进程未启动：%s（ombre 其余功能不受影响）", _last_spawn_error)
        else:
            logger.info(
                "[reading] 共读子进程已启动 pid=%s port=%s app=%s data=%s（阅读器：%s/<token>/）",
                _child_proc.pid, _internal_port(), _app_dir(), _data_dir(), _PUBLIC_PREFIX,
            )
        _wire_api_base()
        if _monitor_task is None or _monitor_task.done():
            _monitor_task = asyncio.get_event_loop().create_task(_monitor())
    except Exception as e:  # noqa: BLE001 — 启动期兜底
        logger.warning("[reading] 共读子进程启动异常已忽略：%s", e)


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
        "data_dir": _data_dir(),
        "last_error": _last_spawn_error,
    }


# ============================================================
# 反向代理
# ============================================================
def _client():
    """连接复用的 AsyncClient。生产（单一 uvicorn 循环）只建一次；
    事件循环变了（测试/嵌入场景）就丢弃旧的重建，避免绑死在已关闭的循环上。"""
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
    """注册 /reading/{path} 反向代理。token 校验完全交给 read-along 服务端
    （无/错 token 一律 404 且与路径不存在不可区分），这里只做转发。"""

    @mcp.custom_route(_PUBLIC_PREFIX + "/{rest:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"])
    async def reading_proxy(request: Request):
        import httpx
        rest = request.path_params.get("rest", "")
        target = f"http://127.0.0.1:{_internal_port()}/{rest}"
        if request.url.query:
            target += f"?{request.url.query}"
        headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}
        # 只有可能带 body 的方法才透传请求流（GET/HEAD 等带 chunked 空流会让部分
        # 服务端困惑）；上传书（POST /api/import，≤50MB）走这里的流式转发，不落内存
        body = request.stream() if request.method in ("POST", "PUT", "PATCH") else None
        try:
            upstream = _client().build_request(
                request.method, target, headers=headers, content=body,
            )
            resp = await _client().send(upstream, stream=True)
        except httpx.HTTPError as e:
            logger.warning("[reading] 代理转发失败：%s", e)
            return JSONResponse(
                {"error": "reading service unavailable（共读子进程未就绪，稍后再试）"},
                status_code=502,
            )
        resp_headers = {k: v for k, v in resp.headers.items() if k.lower() not in _HOP_BY_HOP}
        return StreamingResponse(
            resp.aiter_raw(),
            status_code=resp.status_code,
            headers=resp_headers,
            background=BackgroundTask(resp.aclose),
        )
