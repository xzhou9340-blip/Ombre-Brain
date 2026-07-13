"""
========================================
tools/phone_activity/core.py — phone_activity_query 实现
========================================

查询她的 app 使用记录：Supabase REST（PostgREST）读 phone_activity 表。

关键行为：
- 凭证取自环境变量 SUPABASE_URL + SUPABASE_SERVICE_KEY（service_role key
  权限很大，只在服务器端配置）；缺任何一个都返回可读提示，不抛异常。
- 查询：GET {SUPABASE_URL}/rest/v1/phone_activity
    ?select=*&opened_at=gte.{UTC 时间}&order=opened_at.desc&limit=1000
    headers: apikey + Authorization: Bearer {service key}
- 返回两部分：
    a) 聚合：每个 app_name 的打开次数 + 最后打开时间（按次数降序）
    b) 明细：时间倒序的原始记录（超过 _MAX_DETAIL_ROWS 条截断并注明）
- 所有时间戳转换成 UTC+8 再返回，并在输出里标注时区。
- service key 绝不进日志、绝不出现在返回内容里：错误文本先经 _redact()
  打码再输出（Supabase 4xx 响应体理论上不含 key，但异常/URL 信息可能含）。

不做什么（边界）：
- 只读，不写不删 phone_activity 表。
- 不做分页拉全量：limit=1000 上限对"最近几小时"的场景绰绰有余。

对外暴露：phone_activity_query(hours=24) → str
========================================
"""

import os
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

logger = logging.getLogger("ombre_brain")

_TABLE = "phone_activity"
_TZ_UTC8 = timezone(timedelta(hours=8))
_DEFAULT_HOURS = 24
_MAX_HOURS = 24 * 30            # 最多查 30 天，防止无意义全表扫
_FETCH_LIMIT = 1000             # 单次最多拉取的行数
_MAX_DETAIL_ROWS = 200          # 明细最多展示的行数
_REQUEST_TIMEOUT_SECONDS = 15.0
_ERROR_BODY_SNIPPET_CHARS = 300
# 明细行里不重复展示的列（时间/应用名单独格式化，id 类无阅读价值）
_DETAIL_SKIP_FIELDS = ("id", "uuid", "app_name", "opened_at", "created_at", "inserted_at", "updated_at")


def _supabase_env() -> tuple[str, str]:
    url = (os.environ.get("SUPABASE_URL") or "").strip().rstrip("/")
    key = (os.environ.get("SUPABASE_SERVICE_KEY") or "").strip()
    return url, key


def _redact(text: str) -> str:
    """把 service key 从任意文本里打码，防止经错误信息泄漏。"""
    _, key = _supabase_env()
    return text.replace(key, "***SERVICE_KEY***") if key else text


def _make_client() -> httpx.AsyncClient:
    """单独提出来便于测试注入 MockTransport。"""
    return httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS)


def _parse_ts(value: object) -> Optional[datetime]:
    """解析 opened_at：ISO 字符串（可带 Z / 无时区，无时区按 UTC）或 unix 秒。"""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    s = str(value).strip()
    if not s:
        return None
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _fmt_utc8(dt: datetime) -> str:
    return dt.astimezone(_TZ_UTC8).strftime("%Y-%m-%d %H:%M:%S")


def _detail_extras(row: dict) -> str:
    """明细行附加字段：除时间/应用名/id 外的非空标量，拼成 ` | k=v`。"""
    parts = []
    for k, v in row.items():
        if k in _DETAIL_SKIP_FIELDS or v is None:
            continue
        s = str(v).strip()
        if s:
            parts.append(f"{k}={s}")
    return (" | " + " ".join(parts)) if parts else ""


async def phone_activity_query(hours: int = _DEFAULT_HOURS) -> str:
    url, key = _supabase_env()
    if not url or not key:
        missing = " / ".join(
            n for n, v in (("SUPABASE_URL", url), ("SUPABASE_SERVICE_KEY", key)) if not v
        )
        return f"❌ phone_activity_query 未配置：服务器缺少环境变量 {missing}，查询功能不可用。"
    try:
        hours = int(hours)
    except (TypeError, ValueError):
        hours = _DEFAULT_HOURS
    if hours <= 0:
        hours = _DEFAULT_HOURS
    hours = min(hours, _MAX_HOURS)

    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    since_iso = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    endpoint = f"{url}/rest/v1/{_TABLE}"
    params = {
        "select": "*",
        "opened_at": f"gte.{since_iso}",
        "order": "opened_at.desc",
        "limit": str(_FETCH_LIMIT),
    }
    headers = {"apikey": key, "Authorization": f"Bearer {key}"}
    try:
        async with _make_client() as client:
            resp = await client.get(endpoint, params=params, headers=headers)
    except Exception as e:
        logger.warning(f"[phone_activity] request failed: {_redact(f'{type(e).__name__}: {e}')}")
        return f"❌ 查询 phone_activity 失败：{_redact(f'{type(e).__name__}: {e}')}"
    if resp.status_code != 200:
        snippet = _redact((resp.text or "")[:_ERROR_BODY_SNIPPET_CHARS])
        logger.warning(f"[phone_activity] http={resp.status_code} body={snippet}")
        return f"❌ Supabase 查询失败（HTTP {resp.status_code}）：{snippet}"
    try:
        rows = resp.json()
    except Exception as e:
        return f"❌ Supabase 响应不是合法 JSON：{_redact(str(e))}"
    if not isinstance(rows, list):
        return f"❌ Supabase 响应格式异常（期望数组）：{_redact(str(rows)[:_ERROR_BODY_SNIPPET_CHARS])}"
    if not rows:
        return f"[Ombre Brain · phone_activity] 最近 {hours} 小时没有手机使用记录。（时间按 UTC+8 计）"

    # a) 聚合：app_name → 次数 + 最后打开时间
    agg: dict[str, dict] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        app = str(r.get("app_name") or "").strip() or "(未知应用)"
        ts = _parse_ts(r.get("opened_at"))
        ent = agg.setdefault(app, {"count": 0, "last": None})
        ent["count"] += 1
        if ts and (ent["last"] is None or ts > ent["last"]):
            ent["last"] = ts

    lines = [
        f"[Ombre Brain · phone_activity] 最近 {hours} 小时的手机使用记录"
        f"（共 {len(rows)} 条，以下时间均为 UTC+8）",
        "",
        "—— 聚合（按打开次数降序）——",
    ]
    def _agg_sort_key(item):
        _, ent = item
        last = ent["last"] or datetime.min.replace(tzinfo=timezone.utc)
        return (-ent["count"], -last.timestamp())
    for app, ent in sorted(agg.items(), key=_agg_sort_key):
        last_str = _fmt_utc8(ent["last"]) if ent["last"] else "时间未知"
        lines.append(f"· {app} ×{ent['count']} | 最后打开 {last_str}")

    # b) 明细：时间倒序（服务端已排序，本地不再重排以保留原始顺序）
    lines += ["", "—— 明细（时间倒序）——"]
    for r in rows[:_MAX_DETAIL_ROWS]:
        if not isinstance(r, dict):
            continue
        app = str(r.get("app_name") or "").strip() or "(未知应用)"
        ts = _parse_ts(r.get("opened_at"))
        ts_str = _fmt_utc8(ts) if ts else str(r.get("opened_at") or "时间未知")
        lines.append(f"· {ts_str} | {app}{_detail_extras(r)}")
    if len(rows) > _MAX_DETAIL_ROWS:
        lines.append(f"（明细共 {len(rows)} 条，仅显示最新 {_MAX_DETAIL_ROWS} 条）")
    if len(rows) >= _FETCH_LIMIT:
        lines.append(f"（已达单次拉取上限 {_FETCH_LIMIT} 条，更早的记录可能未包含）")
    return "\n".join(lines)
