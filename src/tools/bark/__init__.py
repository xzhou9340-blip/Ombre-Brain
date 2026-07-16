"""
========================================
tools/bark/__init__.py — bark_push 工具入口
========================================

让克通过 Bark 主动给她的 iPhone 发推送。

对外暴露：bark_push(title, body, icon, url) → str（参数与 server.py 中的 tool 同名）
========================================
"""

from .core import bark_push  # noqa: F401
