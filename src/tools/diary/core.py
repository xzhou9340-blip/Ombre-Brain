"""
========================================
tools/diary/core.py — diary 分区读写实现
========================================

记忆桶只装「已经改变关系形状」的事。日常进展（出差到周五、这周赶一个活、
胃疼两天、跟同事闹别扭没和好）挤进桶里只会稀释重要记忆，但下一个会话窗口
又确实需要知道。diary 就是这条中间路径：一张独立的表，纯追加、纯读取。

判断标准只有一句：「这件事明天还在不在？」
在 → diary；不在（今天午饭吃了什么、路上看见一只猫）→ 不记。
diary 记「正在发生」，桶记「已经改变」。同一天的事可以分别进两个地方。

关键行为：
- 独立 SQLite 表 diary（<buckets_dir>/diary.db），不复用 buckets，
  不与桶共享任何字段/目录/索引
- date 是「记录归属的日期」而非写入时间；created_at 才是写入时间。
  同一 date 允许多条，追加不覆盖
- diary_read 按 date 正序、同日按写入顺序（id 递增）返回，按天分组，
  空的日期直接跳过

不做什么（边界）：
- 不调用 dehydrator / 不拆桶 / 不建向量索引 / 不参与语义检索 /
  不参与 breath / dream / decay。整条路径不碰任何外部 API——
  siliconflow 429 挂掉时 diary 必须依然可写可读，这是它存在的前提之一。
- 不做删除与编辑接口；不做超过 7 天的查询；不迁移 buckets 里的旧数据
- 不做标签、不做分类：一条 diary 就是一句到一段自然语言

对外暴露：diary_write(content, date) → str / diary_read(days) → str
========================================
"""

import os
import sqlite3
from datetime import datetime, timedelta
from typing import Optional

from .. import _runtime as rt

# 查询窗口上限：diary 是「最近几天」的交接班材料，不是历史档案。
# 想看更早的东西说明那件事已经改变了什么，应该在桶里而不在这里。
MAX_DAYS = 7
DEFAULT_DAYS = 3

_DB_FILENAME = "diary.db"
_DATE_FMT = "%Y-%m-%d"


# -------------------- 存储 --------------------

def _db_path() -> str:
    """diary.db 落在 buckets_dir 下（与 dehydration_cache.db 同级）。

    rt.config 正常由 server.py 注入；取不到时回退环境变量，保证 diary
    在运行时上下文没装配好的场景（脚本、裸测试）里依然能定位到持久盘。"""
    base = ""
    if rt.config:
        base = str(rt.config.get("buckets_dir") or "")
    if not base:
        base = os.environ.get("OMBRE_VAULT_DIR") or os.environ.get("OMBRE_BUCKETS_DIR") or ""
    if not base:
        raise RuntimeError("找不到 buckets_dir，无法定位 diary.db")
    return os.path.join(base, _DB_FILENAME)


def _connect() -> sqlite3.Connection:
    """打开（必要时创建）diary.db，并保证表与索引存在。

    每次调用开一个新连接、用完即关：diary 的调用频次极低，省掉长连接
    就省掉了「buckets_dir 被改了但连接还指着旧路径」这类脏状态。"""
    path = _db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS diary (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date DATE NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_diary_date ON diary(date)")
        conn.commit()
    except Exception:
        conn.close()
        raise
    return conn


# -------------------- 参数处理 --------------------

def _today() -> str:
    return datetime.now().strftime(_DATE_FMT)


def _normalize_date(date: str) -> Optional[str]:
    """校验 YYYY-MM-DD；不合法返回 None（由调用方给可读提示，不抛异常）。"""
    try:
        return datetime.strptime(date.strip(), _DATE_FMT).strftime(_DATE_FMT)
    except (ValueError, AttributeError):
        return None


def _clamp_days(days: Optional[int]) -> int:
    """days 收进 [1, MAX_DAYS]。非法输入按默认值处理，不报错——
    交接班场景下宁可少给几天也不能因为一个参数把整次读取打断。"""
    if days is None:
        return DEFAULT_DAYS
    try:
        n = int(days)
    except (TypeError, ValueError):
        return DEFAULT_DAYS
    if n < 1:
        return 1
    if n > MAX_DAYS:
        return MAX_DAYS
    return n


# -------------------- 工具实现 --------------------

async def diary_write(content: str, date: Optional[str] = "") -> str:
    if content is None:
        content = ""
    if date is None:
        date = ""

    if rt.mark_op:
        rt.mark_op("diary_write")

    text = content.strip()
    if not text:
        return "diary 需要内容。"

    if date.strip():
        d = _normalize_date(date)
        if not d:
            return f"日期格式不对：{date.strip()}。要 YYYY-MM-DD，比如 {_today()}。"
    else:
        d = _today()

    created_at = datetime.now().isoformat(timespec="seconds")
    try:
        conn = _connect()
        try:
            cur = conn.execute(
                "INSERT INTO diary (date, content, created_at) VALUES (?, ?, ?)",
                (d, text, created_at),
            )
            conn.commit()
            row_id = cur.lastrowid
        finally:
            conn.close()
    except Exception as e:
        return f"diary 写入失败: {e}"

    return f"📔 diary #{row_id} {d}"


async def diary_read(days: Optional[int] = DEFAULT_DAYS) -> str:
    if rt.mark_op:
        rt.mark_op("diary_read")

    n = _clamp_days(days)
    since = (datetime.now() - timedelta(days=n - 1)).strftime(_DATE_FMT)

    try:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT date, content FROM diary WHERE date >= ? ORDER BY date ASC, id ASC",
                (since,),
            ).fetchall()
        finally:
            conn.close()
    except Exception as e:
        return f"diary 读取失败: {e}"

    if not rows:
        return f"最近 {n} 天没有记录。"

    # 按天分组：rows 已按 date 正序，同一天内按写入顺序，直接顺着切段即可。
    # 没有记录的日期不会出现在 rows 里，天然跳过，不占版面。
    lines = [f"=== 最近 {n} 天 ==="]
    current = ""
    for d, text in rows:
        if d != current:
            current = d
            lines.append(f"\n{d}")
        lines.append(f"- {text}")
    return "\n".join(lines)
