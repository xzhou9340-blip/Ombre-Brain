"""
========================================
pending_store.py — 待处理区（任务书 §1.4）
========================================

grow 的降级兜底（tools/grow/fallback.py）在 LLM 挂掉时会把原文整存成桶。
但存在一个它也救不了的场景：embedding 接口同时挂着——
`bucket_manager.create()` 在 `_sync_embedding` 失败时会删掉刚写的文件并抛
（那是 bucket_manager 明确写死的不变量：不允许「文件在但向量缺」）。
compress 与 embed 在本部署里同源（siliconflow），一次 429 很可能两条都打掉，
于是任何桶都建不出来，内容仍会丢。

待处理区就是这个场景的最后落点：**先把原文落到本地，不要求向量、不要求
LLM**，等接口恢复后再补建桶。它和 diary 用同一套隔离做法——独立 SQLite、
纯本地、每次开连接用完即关。

本次只做「写入」这一半。
对账补建那一半留到阶段二，与 §2.1 的 letter PENDING_MIGRATE 回迁
**合并成同一个函数**，不写两套。

给将来那个对账函数的硬性要求（写在这里免得被漏掉）：
- 它必须同时消费两个来源：
  ① 本表 status='pending' 的记录（新机制，结构化字段）
  ② 标题里带 PENDING_MIGRATE 的历史 letter（旧机制，字符串标记）
- 已知的历史数据至少有一条：letter ``ea47fc1b4ee5``
  《2026-07-27 03-22-26 降级存档 PENDING_MIGRATE20260723 …》，
  对应 plan ``a86df2e98081``。用 is_legacy_pending_title() 判定，别写死全名。
- 补建成功后：本表置 status='migrated' + 回填 bucket_id/migrated_at；
  letter 那侧清除标题标记并留一行迁移记录。

关键行为：
- 独立 SQLite 库 <buckets_dir>/pending.db，单表 pending_writes
- 不建向量、不打标签、不调 dehydrator、不碰 bucket_mgr——
  它存在的全部理由就是「那些都挂了的时候还能写进去」
- 写失败也不抛：调用方（fallback）此刻正在救火，不能再被异常打断，
  record() 失败返回 0 并记日志

不做什么（边界）：
- 不做对账、不做补建桶、不做重试、不做定时任务（阶段二的事）
- 不注册 MCP 工具：这是基础设施，不是给模型直接调的
- 不做删除接口

对外暴露：record() / list_pending() / count_pending() /
         is_legacy_pending_title() / LEGACY_PENDING_MARKER
========================================
"""

import os
import sqlite3
import logging
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger("ombre_brain")

_DB_FILENAME = "pending.db"

STATUS_PENDING = "pending"
STATUS_MIGRATED = "migrated"

# §2.1 的历史标记：降级写成 letter 时打在标题字符串里。
# 对账函数必须认它，否则 ea47fc1b4ee5 那条永远迁不回来。
LEGACY_PENDING_MARKER = "PENDING_MIGRATE"


def is_legacy_pending_title(title: str) -> bool:
    """标题里带 PENDING_MIGRATE 就算待迁移（旧机制）。

    只做子串判定，不匹配全名：历史标题经过 sanitize_name 洗过，
    日期后缀、空格、下划线都可能被改，写死全名一定会漏。"""
    return LEGACY_PENDING_MARKER in (title or "")


def db_path(buckets_dir: str) -> str:
    return os.path.join(buckets_dir, _DB_FILENAME)


def _connect(buckets_dir: str) -> sqlite3.Connection:
    """打开（必要时创建）pending.db，保证表与索引存在。

    与 diary 同样每次开连接、用完即关：调用频次极低（只在故障时写），
    省掉长连接就省掉「buckets_dir 变了但连接还指着旧路径」这类脏状态。"""
    path = db_path(buckets_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_writes (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                content     TEXT NOT NULL,
                source_tool TEXT NOT NULL,
                reason      TEXT NOT NULL DEFAULT '',
                status      TEXT NOT NULL DEFAULT 'pending',
                created_at  TIMESTAMP NOT NULL,
                -- 下面两列本次不写，留给阶段二的对账函数回填，
                -- 现在建好省一次 schema 迁移
                migrated_at TIMESTAMP,
                bucket_id   TEXT NOT NULL DEFAULT ''
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pending_status ON pending_writes(status)"
        )
        conn.commit()
    except Exception:
        conn.close()
        raise
    return conn


def record(buckets_dir: str, content: str, source_tool: str, reason: str = "") -> int:
    """把一条救不回来的写入落到待处理区，返回 id；失败返回 0。

    绝不抛异常：调用方是降级路径，此刻主链路已经挂了，
    再抛一次只会把最后一点内容也弄丢。"""
    text = (content or "").strip()
    if not text:
        return 0
    try:
        conn = _connect(buckets_dir)
        try:
            cur = conn.execute(
                "INSERT INTO pending_writes "
                "(content, source_tool, reason, status, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    text,
                    (source_tool or "unknown").strip(),
                    (reason or "").strip(),
                    STATUS_PENDING,
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
            conn.commit()
            return int(cur.lastrowid or 0)
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"[pending_store] record failed / 待处理区写入失败: {e}")
        return 0


def list_pending(buckets_dir: str, limit: Optional[int] = None) -> list[dict[str, Any]]:
    """按写入顺序列出待处理记录。阶段二的对账函数从这里取活儿。"""
    try:
        conn = _connect(buckets_dir)
        try:
            sql = (
                "SELECT id, content, source_tool, reason, status, created_at "
                "FROM pending_writes WHERE status = ? ORDER BY id ASC"
            )
            params: tuple = (STATUS_PENDING,)
            if limit is not None:
                sql += " LIMIT ?"
                params = (STATUS_PENDING, int(limit))
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"[pending_store] list failed / 待处理区读取失败: {e}")
        return []

    keys = ("id", "content", "source_tool", "reason", "status", "created_at")
    return [dict(zip(keys, r)) for r in rows]


def count_pending(buckets_dir: str) -> int:
    try:
        conn = _connect(buckets_dir)
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM pending_writes WHERE status = ?",
                (STATUS_PENDING,),
            ).fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"[pending_store] count failed / 待处理区计数失败: {e}")
        return 0
