"""
========================================
tools/phone_activity/__init__.py — phone_activity_query 工具入口
========================================

让克查她的 app 使用记录（Supabase phone_activity 表）。

对外暴露：phone_activity_query(hours) → str（参数与 server.py 中的 tool 同名）
========================================
"""

from .core import phone_activity_query  # noqa: F401
