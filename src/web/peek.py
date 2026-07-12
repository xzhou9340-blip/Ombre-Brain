"""
========================================
web/peek.py — Peek 功能：接收 iOS 快捷指令上传的屏幕截图
========================================

给 Claude 一双"眼睛"，能看到用户手机最近分享的屏幕截图。

关键行为：
- POST /peek/upload
    · Header X-Peek-Token（或 Authorization: Bearer <token>）鉴权，token 取自
      环境变量 OMBRE_PEEK_TOKEN；未配置 token 时接口 503 拒收（避免裸奔）。
    · 支持两种上传格式：
        - multipart/form-data，字段名 image / file / photo（iOS 快捷指令默认）。
        - application/json，body 为 {"image_base64": "<base64...>"}。
    · 服务端用 Pillow 压缩：长边缩到 1000px，JPEG 质量 80，去 EXIF 元数据。
    · 存储位置 <buckets_dir>/peek/：
        - latest.jpg：覆盖式最新截图
        - meta.json：{"uploaded_at": <unix ts>, "size": <bytes>}
        - archive/YYYYmmdd_HHMMSS.jpg：时间戳副本
- 副本 24 小时后自动清理：
    · lifespan 启动时先清理一次
    · 每小时循环清理一次（非精确 cron，够用）
- 该模块只暴露 HTTP 端点；MCP tool `peek` 在 server.py 里注册（要返回 image
  content block，不能走 _with_notice 的 str 包装）。

不做什么（边界）：
- 不做 OCR、不做长期存储；副本只留 24h。
- 不动 buckets_dir 里的记忆结构，peek 目录是独立的兄弟目录。

对外暴露：
    register(mcp) — 注册 /peek/upload 路由 + 启动清理循环
    read_latest() — 读取最新截图（供 MCP peek tool 调用）
    PEEK_WINDOW_SECONDS — 24 小时窗口常量
========================================
"""

import os
import io
import json
import time
import asyncio
import base64
import hmac
import logging
from typing import Optional

from starlette.responses import JSONResponse
from starlette.requests import Request

logger = logging.getLogger("ombre_brain")

# ============================================================
# 常量
# ============================================================
PEEK_WINDOW_SECONDS = 24 * 60 * 60          # MCP tool 判定"最近截图"的窗口
_ARCHIVE_RETENTION_SECONDS = 24 * 60 * 60   # 副本保留时长（同上，两者语义一致）
_CLEANUP_INTERVAL_SECONDS = 60 * 60         # 副本清理循环间隔
_JPEG_LONG_EDGE_PX = 1000                   # 压缩长边像素
_JPEG_QUALITY = 80                          # JPEG 质量
_MAX_UPLOAD_BYTES = 20 * 1024 * 1024        # 上传原图上限 20MB，防打爆

_cleanup_task: "asyncio.Task | None" = None


# ============================================================
# 路径 helper
# ============================================================
def _peek_dir() -> str:
    """<buckets_dir>/peek/ — 与记忆桶目录同盘，Render 上都在持久 disk。"""
    from . import _shared as sh
    base = (sh.config or {}).get("buckets_dir") or "buckets"
    return os.path.join(base, "peek")


def _latest_path() -> str:
    return os.path.join(_peek_dir(), "latest.jpg")


def _meta_path() -> str:
    return os.path.join(_peek_dir(), "meta.json")


def _archive_dir() -> str:
    return os.path.join(_peek_dir(), "archive")


def _ensure_dirs() -> None:
    os.makedirs(_archive_dir(), exist_ok=True)


# ============================================================
# Token 鉴权
# ============================================================
def _configured_token() -> str:
    return (os.environ.get("OMBRE_PEEK_TOKEN") or "").strip()


def _check_auth(request: Request) -> bool:
    token = _configured_token()
    if not token:
        return False
    headers = request.headers
    candidates = [
        headers.get("x-peek-token", "") or "",
        headers.get("x-ombre-peek-token", "") or "",
    ]
    auth = headers.get("authorization", "") or ""
    if auth.startswith("Bearer "):
        candidates.append(auth[7:])
    return any(v and hmac.compare_digest(v, token) for v in candidates)


# ============================================================
# 图片压缩
# ============================================================
def _compress_to_jpeg(raw: bytes) -> bytes:
    """Pillow 压缩：长边 1000px、JPEG q80、去 EXIF、强制 RGB。"""
    from PIL import Image

    with Image.open(io.BytesIO(raw)) as im:
        im.load()
        # RGBA / P / LA 等模式转 RGB，避免 JPEG 保存报错
        if im.mode not in ("RGB", "L"):
            im = im.convert("RGB")
        w, h = im.size
        long_edge = max(w, h)
        if long_edge > _JPEG_LONG_EDGE_PX:
            scale = _JPEG_LONG_EDGE_PX / float(long_edge)
            new_size = (max(1, int(round(w * scale))), max(1, int(round(h * scale))))
            im = im.resize(new_size, Image.LANCZOS)
        out = io.BytesIO()
        im.save(out, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
        return out.getvalue()


# ============================================================
# 写入 / 读取
# ============================================================
def _write_meta(uploaded_at: float, size: int) -> None:
    tmp = _meta_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"uploaded_at": uploaded_at, "size": size}, f)
    os.replace(tmp, _meta_path())


def _read_meta() -> dict:
    try:
        with open(_meta_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_screenshot(jpeg_bytes: bytes) -> tuple[float, str]:
    """把压缩好的 JPEG 落到 latest.jpg + archive/<ts>.jpg，返回 (uploaded_at, archive_path)。"""
    _ensure_dirs()
    now = time.time()
    ts_str = time.strftime("%Y%m%d_%H%M%S", time.localtime(now))
    archive_path = os.path.join(_archive_dir(), f"{ts_str}.jpg")
    # 先写 archive 再写 latest（原子替换 latest，防中途读到半张图）
    with open(archive_path, "wb") as f:
        f.write(jpeg_bytes)
    tmp = _latest_path() + ".tmp"
    with open(tmp, "wb") as f:
        f.write(jpeg_bytes)
    os.replace(tmp, _latest_path())
    _write_meta(now, len(jpeg_bytes))
    return now, archive_path


def read_latest() -> Optional[dict]:
    """给 MCP peek tool 用。返回 {"data": bytes, "uploaded_at": float} 或 None。

    None 语义：没有截图，或截图已超过 PEEK_WINDOW_SECONDS。
    """
    path = _latest_path()
    if not os.path.exists(path):
        return None
    meta = _read_meta()
    uploaded_at = float(meta.get("uploaded_at") or 0)
    if uploaded_at <= 0:
        # meta 丢了但文件在，用文件 mtime 兜底
        try:
            uploaded_at = os.path.getmtime(path)
        except OSError:
            return None
    if time.time() - uploaded_at > PEEK_WINDOW_SECONDS:
        return None
    try:
        with open(path, "rb") as f:
            return {"data": f.read(), "uploaded_at": uploaded_at}
    except OSError:
        return None


# ============================================================
# 副本清理
# ============================================================
def _cleanup_archive_once() -> int:
    """删除 archive/ 下超过 24h 的 jpg，返回删除数。"""
    d = _archive_dir()
    if not os.path.isdir(d):
        return 0
    cutoff = time.time() - _ARCHIVE_RETENTION_SECONDS
    n = 0
    for name in os.listdir(d):
        if not name.lower().endswith(".jpg"):
            continue
        p = os.path.join(d, name)
        try:
            if os.path.getmtime(p) < cutoff:
                os.remove(p)
                n += 1
        except OSError:
            pass
    return n


async def _cleanup_loop() -> None:
    """启动即扫一次，然后每小时扫一次。"""
    try:
        n = _cleanup_archive_once()
        if n:
            logger.info(f"[peek] startup cleanup removed {n} stale archive(s)")
    except Exception as e:
        logger.warning(f"[peek] startup cleanup failed: {e}")
    while True:
        try:
            await asyncio.sleep(_CLEANUP_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            return
        try:
            n = _cleanup_archive_once()
            if n:
                logger.info(f"[peek] hourly cleanup removed {n} stale archive(s)")
        except Exception as e:
            logger.warning(f"[peek] hourly cleanup failed: {e}")


def _start_cleanup_task() -> None:
    """在当前 event loop 里起一次清理循环。已在跑就不重启。"""
    global _cleanup_task
    if _cleanup_task and not _cleanup_task.done():
        return
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        return
    _cleanup_task = loop.create_task(_cleanup_loop())


# ============================================================
# 上传体解析
# ============================================================
async def _extract_image_bytes(request: Request) -> tuple[Optional[bytes], Optional[str]]:
    """从 request 里抽出原始图片字节。返回 (bytes, err_msg)，成功时 err_msg=None。"""
    ctype = (request.headers.get("content-type") or "").lower()
    # JSON body 场景（易于 curl 手测）
    if ctype.startswith("application/json"):
        try:
            payload = await request.json()
        except Exception as e:
            return None, f"invalid json body: {e}"
        b64 = (payload.get("image_base64") or payload.get("image") or "").strip()
        if not b64:
            return None, "missing image_base64 field"
        # data URL 前缀去掉
        if b64.startswith("data:"):
            _, _, b64 = b64.partition(",")
        try:
            data = base64.b64decode(b64, validate=False)
        except Exception as e:
            return None, f"base64 decode failed: {e}"
        return data, None
    # multipart 场景（iOS 快捷指令首选）
    if ctype.startswith("multipart/form-data"):
        try:
            form = await request.form()
        except Exception as e:
            return None, f"invalid multipart body: {e}"
        for field in ("image", "file", "photo", "screenshot"):
            up = form.get(field)
            if up is None:
                continue
            if hasattr(up, "read"):
                data = await up.read()
                return data, None
            if isinstance(up, (bytes, bytearray)):
                return bytes(up), None
            if isinstance(up, str):
                return up.encode("utf-8"), None
        return None, "no image field found (expected 'image' / 'file' / 'photo' / 'screenshot')"
    # 直接 body 塞原图（Content-Type: image/*）
    if ctype.startswith("image/"):
        body = await request.body()
        return body, None
    # 兜底：也允许纯 body（不推荐但便于调试）
    body = await request.body()
    if body:
        return body, None
    return None, f"unsupported content-type: {ctype!r}"


# ============================================================
# 路由注册
# ============================================================
def register(mcp) -> None:
    """注册 /peek/upload 并把清理循环挂到 lifespan（用启动信号 hook）。"""

    @mcp.custom_route("/peek/upload", methods=["POST"])
    async def peek_upload(request: Request):
        # 未配置 token → 直接拒收，避免生产环境裸奔
        if not _configured_token():
            return JSONResponse(
                {"ok": False, "error": "OMBRE_PEEK_TOKEN not configured on server"},
                status_code=503,
            )
        if not _check_auth(request):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        # 提前用 Content-Length 拦超大 body
        try:
            cl = int(request.headers.get("content-length") or 0)
        except (TypeError, ValueError):
            cl = 0
        if cl and cl > _MAX_UPLOAD_BYTES:
            return JSONResponse(
                {"ok": False, "error": f"upload too large (>{_MAX_UPLOAD_BYTES} bytes)"},
                status_code=413,
            )
        raw, err = await _extract_image_bytes(request)
        if err or not raw:
            return JSONResponse(
                {"ok": False, "error": err or "empty upload"},
                status_code=400,
            )
        if len(raw) > _MAX_UPLOAD_BYTES:
            return JSONResponse(
                {"ok": False, "error": f"upload too large (>{_MAX_UPLOAD_BYTES} bytes)"},
                status_code=413,
            )
        try:
            jpeg = _compress_to_jpeg(raw)
        except Exception as e:
            logger.warning(f"[peek] pillow compress failed: {e}")
            return JSONResponse(
                {"ok": False, "error": f"image decode/compress failed: {e}"},
                status_code=400,
            )
        try:
            uploaded_at, archive_path = _save_screenshot(jpeg)
        except Exception as e:
            logger.error(f"[peek] save failed: {e}")
            return JSONResponse(
                {"ok": False, "error": f"save failed: {e}"},
                status_code=500,
            )
        # 上传后顺便触发一次副本清理（不阻塞响应）
        try:
            _cleanup_archive_once()
        except Exception:
            pass
        # 起 cleanup 循环（幂等，已在跑就 no-op）
        _start_cleanup_task()
        return JSONResponse({
            "ok": True,
            "uploaded_at": uploaded_at,
            "size_bytes": len(jpeg),
            "archive": os.path.basename(archive_path),
        })

    # 尝试立刻起清理循环（有 loop 就起，没有等第一次 upload 触发）
    _start_cleanup_task()
