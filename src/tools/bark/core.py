"""
========================================
tools/bark/core.py — bark_push 实现
========================================

通过 Bark（https://bark.day.app）给她的 iPhone 发一条推送。

关键行为：
- 设备 key 取自环境变量 BARK_KEY（只在服务器端配置）；未配置时返回
  可读的提示字符串，不抛异常。
- title / body 先 URL encode（中文必须编码）再拼进路径：
    GET https://api.day.app/{BARK_KEY}/{title}/{body}?icon=...&url=...
  title 为空时退化为单段 /{body}（Bark 支持只发内容）。
- url 参数（可选）：Bark 的点击跳转地址，点通知直接打开（speak 工具
  用它带上音频 URL）。
- 返回 Bark 的响应 code + message；失败时把 Bark 的错误原样带回。
- key 绝不进日志、绝不出现在返回内容里：所有可能含 URL 的错误文本
  先经 _redact() 打码再输出。

不做什么（边界）：
- 不存推送历史、不重试；失败与否由调用方（克）自行决定下一步。
- 不校验 icon URL 可达性，Bark 端自己处理。

对外暴露：bark_push(title, body, icon, url) → str
========================================
"""

import os
import logging
from urllib.parse import quote

import httpx

logger = logging.getLogger("ombre_brain")

_BARK_API_BASE = "https://api.day.app"
_REQUEST_TIMEOUT_SECONDS = 15.0
_ERROR_BODY_SNIPPET_CHARS = 300  # 非 JSON 错误响应截断长度


def _bark_key() -> str:
    return (os.environ.get("BARK_KEY") or "").strip()


def _redact(text: str) -> str:
    """把 BARK_KEY 从任意文本（异常信息常含完整 URL）里打码。"""
    key = _bark_key()
    return text.replace(key, "***BARK_KEY***") if key else text


def _make_client() -> httpx.AsyncClient:
    """单独提出来便于测试注入 MockTransport。"""
    return httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS)


async def bark_push(title: str, body: str, icon: str = "", url: str = "") -> str:
    key = _bark_key()
    if not key:
        return "❌ bark_push 未配置：服务器缺少环境变量 BARK_KEY，推送功能不可用。"
    title = (title or "").strip()
    body = (body or "").strip()
    if not title and not body:
        return "❌ bark_push 需要 title 或 body 至少一个非空。"
    # quote(safe="") 连 "/" 一起编码，避免正文里的斜杠被当成路径分隔
    q_title = quote(title, safe="")
    q_body = quote(body, safe="")
    if title and body:
        path = f"/{q_title}/{q_body}"
    else:
        path = f"/{q_title or q_body}"
    push_url = f"{_BARK_API_BASE}/{key}{path}"
    params = {}
    if icon and icon.strip():
        params["icon"] = icon.strip()
    if url and url.strip():
        params["url"] = url.strip()
    try:
        async with _make_client() as client:
            resp = await client.get(push_url, params=params or None)
    except Exception as e:
        logger.warning(f"[bark_push] request failed: {_redact(f'{type(e).__name__}: {e}')}")
        return f"❌ Bark 推送请求失败：{_redact(f'{type(e).__name__}: {e}')}"
    # Bark 正常返回 JSON：{"code": 200, "message": "success", "timestamp": ...}
    code: object = resp.status_code
    message = ""
    try:
        data = resp.json()
        if isinstance(data, dict):
            code = data.get("code", resp.status_code)
            message = str(data.get("message", ""))
    except Exception:
        message = _redact((resp.text or "")[:_ERROR_BODY_SNIPPET_CHARS])
    if resp.status_code == 200 and code == 200:
        logger.info(f"[bark_push] ok code={code}")
        return f"✅ 推送已发出（Bark code={code} message={message}）"
    logger.warning(f"[bark_push] fail http={resp.status_code} code={code} message={_redact(message)}")
    return f"❌ Bark 推送失败（HTTP {resp.status_code}，code={code}）：{_redact(message)}"
