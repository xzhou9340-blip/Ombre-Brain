"""
========================================
tools/speak/__init__.py — speak 工具入口
========================================

让克主动给她发语音：ElevenLabs 生成 → Supabase Storage 拿公开 URL →
Bark 推送到她的 iPhone。

对外暴露：speak(text, stability, style, speed) → str（参数与 server.py 中的 tool 同名）
========================================
"""

from .core import speak  # noqa: F401
