"""
========================================
tool_stats.py — 工具调用频次统计
========================================

起因：想收拢工具的时候发现「哪些工具不常用」只能靠印象猜。地基上本来就有
一个钩子 `web/_shared._mark_op(name)`，每个工具调用时都把自己的名字传了进去，
但那个函数只更新一个时间戳、**把 name 扔了**（它的用途是 /api/heartbeat 的
活跃灯）。所以埋点其实早就铺好了，缺的只是把名字记下来。

这里补的就是这一步：按工具名累加次数 + 首末次时间。有了真数据，「砍哪个」
才是有依据的判断而不是猜——而删工具是不可逆的。

关键行为：
- 独立 SQLite 库 <buckets_dir>/tool_stats.db，单表 tool_calls，
  与 diary / pending 同一套隔离做法（独立库、纯本地、开连接用完即关）
- record() 任何情况下都不抛：它挂在每次工具调用的路径上，
  统计失败绝不能让工具本身失败。坏路径返回 False，只记日志
- 计数落盘而不是留在内存：部署一次进程就没了，内存计数攒不出「一周的数据」

不做什么（边界）：
- 不注册 MCP 工具：这是基础设施，不给模型直接调
- 不记参数、不记返回值、不记内容——只记名字和次数。
  这东西的用途是「决定砍谁」，不是行为审计，多记一个字段都是多余的风险
- 不做清理/归档/滚动窗口：一张几十行的表，不值得

对外暴露：record(name) / snapshot(limit) / configure(buckets_dir)
========================================
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger("ombre_brain.tool_stats")

_DB_FILENAME = "tool_stats.db"

# server.py 启动时注入（那里 config 已装配好）。留空则回退环境变量，
# 保证脚本/裸测试场景下也能定位。
_buckets_dir_override: str = ""

# 名字长度上限：工具名都是短标识符，超长的多半是调用方传错了东西，
# 截断而不是拒绝——统计不该因为一个脏输入就断掉。
_MAX_NAME_LEN = 64


def configure(buckets_dir: str) -> None:
    global _buckets_dir_override
    _buckets_dir_override = str(buckets_dir or "")


def _db_path() -> str:
    base = _buckets_dir_override
    if not base:
        base = os.environ.get("OMBRE_VAULT_DIR") or os.environ.get("OMBRE_BUCKETS_DIR") or ""
    if not base:
        raise RuntimeError("找不到 buckets_dir，无法定位 tool_stats.db")
    return os.path.join(base, _DB_FILENAME)


def _connect() -> sqlite3.Connection:
    path = _db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=2.0)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tool_calls (
                name TEXT PRIMARY KEY,
                count INTEGER NOT NULL,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL
            )
        """)
        conn.commit()
    except Exception:
        conn.close()
        raise
    return conn


def record(name: str) -> bool:
    """记一次调用。永不抛异常——它在每次工具调用的路径上。"""
    try:
        clean = str(name or "").strip()[:_MAX_NAME_LEN]
        if not clean:
            return False
        now = datetime.now().isoformat(timespec="seconds")
        conn = _connect()
        try:
            conn.execute(
                """
                INSERT INTO tool_calls (name, count, first_seen, last_seen)
                VALUES (?, 1, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    count = count + 1,
                    last_seen = excluded.last_seen
                """,
                (clean, now, now),
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as e:
        logger.warning(f"[tool_stats] record({name!r}) failed: {e}")
        return False


def snapshot(limit: Optional[int] = None) -> list[dict[str, Any]]:
    """按次数降序返回统计。读不到就返回空列表，不抛。"""
    try:
        conn = _connect()
        try:
            sql = "SELECT name, count, first_seen, last_seen FROM tool_calls ORDER BY count DESC, name ASC"
            params: tuple = ()
            if limit is not None and int(limit) > 0:
                sql += " LIMIT ?"
                params = (int(limit),)
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
    except Exception as e:
        logger.warning(f"[tool_stats] snapshot failed: {e}")
        return []

    return [
        {"name": n, "count": c, "first_seen": f, "last_seen": l}
        for n, c, f, l in rows
    ]
