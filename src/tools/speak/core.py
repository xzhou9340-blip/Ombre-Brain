"""
========================================
tools/speak/core.py — speak 实现
========================================

克主动给她发语音：ElevenLabs 生成 mp3 → 上传 Supabase Storage（public
bucket voices）→ 拿公开 URL → 复用 tools/bark 推送到她的 iPhone
（通知带 url 参数，点开直接播放）。

关键行为：
- generate_speech 与 voice/voice_core.py 是同一份逻辑（第一阶段已真实
  跑通），同步 requests 实现；speak() 用 asyncio.to_thread 包一层，
  TTS 最长两分钟也不会阻塞事件循环。
- 凭证取自环境变量 ELEVENLABS_API_KEY + ELEVENLABS_VOICE_ID +
  SUPABASE_URL + SUPABASE_SERVICE_KEY；缺任何一个都返回可读提示，不抛异常。
- TTS：POST https://api.elevenlabs.io/v1/text-to-speech/{voice_id}
  模型 eleven_v3、language_code=zh、输出 mp3_44100_128。
- 上传：POST {SUPABASE_URL}/storage/v1/object/voices/{时间戳}.mp3；
  bucket 不存在时自动创建（public），公开 URL 按固定规则拼出。
- API 报错时把对端原始响应完整带回（不吞），但所有输出先经 _redact()
  把两把 key 打码——key 绝不进日志、绝不出现在返回内容里。
- 推送：bark_push(title="克", body=台词前 20 字, url=音频地址)；
  推送失败不影响返回音频 URL，两个结果都如实带回。

不做什么（边界）：
- 不落地本地文件、不存生成历史；声音库记录是本地 CLI
  （voice/speak_cli.py）的职责。
- 不重试；失败与否由调用方（克）自行决定下一步。

对外暴露：speak(text, stability, style, speed) → str
========================================
"""

import os
import asyncio
import logging
from datetime import datetime

import requests

from tools.bark import core as _bark_core

logger = logging.getLogger("ombre_brain")

ELEVENLABS_MODEL_ID = "eleven_v3"
ELEVENLABS_LANGUAGE_CODE = "zh"
ELEVENLABS_OUTPUT_FORMAT = "mp3_44100_128"
SUPABASE_BUCKET = "voices"

DEFAULT_STABILITY = 0.34
DEFAULT_STYLE = 0.84
DEFAULT_SPEED = 1.2

TTS_TIMEOUT_SECONDS = 120
UPLOAD_TIMEOUT_SECONDS = 60

_PUSH_TITLE = "克"
_PUSH_BODY_CHARS = 20


class SpeechError(RuntimeError):
    """TTS 或上传失败。message 中包含对端返回的原始错误信息。"""


def _redact(text: str) -> str:
    """把 ElevenLabs key 和 Supabase service key 从任意文本里打码。"""
    for env_name, mask in (
        ("ELEVENLABS_API_KEY", "***ELEVENLABS_KEY***"),
        ("SUPABASE_SERVICE_KEY", "***SERVICE_KEY***"),
    ):
        key = (os.environ.get(env_name) or "").strip()
        if key:
            text = text.replace(key, mask)
    return text


def generate_speech(text: str, stability: float = DEFAULT_STABILITY,
                    style: float = DEFAULT_STYLE,
                    speed: float = DEFAULT_SPEED) -> str:
    """把台词转成语音并上传到 Supabase Storage，返回公开访问 URL。

    与 voice/voice_core.py 的 generate_speech 保持同一份逻辑；同步实现，
    异步上下文里请经 speak() 调用。
    """
    if not text or not text.strip():
        raise SpeechError("text 不能为空")

    api_key = _require_env("ELEVENLABS_API_KEY")
    voice_id = _require_env("ELEVENLABS_VOICE_ID")
    supabase_url = _require_env("SUPABASE_URL").rstrip("/")
    supabase_key = _require_env("SUPABASE_SERVICE_KEY")

    audio = _elevenlabs_tts(text, api_key, voice_id, stability, style, speed)

    filename = datetime.now().strftime("%Y%m%d_%H%M%S") + ".mp3"
    _ensure_bucket(supabase_url, supabase_key)
    _upload(audio, filename, supabase_url, supabase_key)

    return f"{supabase_url}/storage/v1/object/public/{SUPABASE_BUCKET}/{filename}"


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SpeechError(f"服务器缺少环境变量 {name}")
    return value


def _elevenlabs_tts(text: str, api_key: str, voice_id: str,
                    stability: float, style: float, speed: float) -> bytes:
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    resp = requests.post(
        url,
        params={"output_format": ELEVENLABS_OUTPUT_FORMAT},
        headers={"xi-api-key": api_key, "Content-Type": "application/json"},
        json={
            "text": text,
            "model_id": ELEVENLABS_MODEL_ID,
            "language_code": ELEVENLABS_LANGUAGE_CODE,
            "voice_settings": {
                "stability": stability,
                "style": style,
                "speed": speed,
            },
        },
        timeout=TTS_TIMEOUT_SECONDS,
    )
    if resp.status_code != 200:
        raise SpeechError(
            f"ElevenLabs 返回 HTTP {resp.status_code}，原始响应：\n{resp.text}"
        )
    if not resp.content:
        raise SpeechError("ElevenLabs 返回 200 但音频内容为空")
    return resp.content


def _supabase_headers(supabase_key: str) -> dict:
    return {"Authorization": f"Bearer {supabase_key}", "apikey": supabase_key}


def _ensure_bucket(supabase_url: str, supabase_key: str) -> None:
    """确保 public bucket 存在；已存在时 Supabase 返回错误，识别后忽略。"""
    resp = requests.post(
        f"{supabase_url}/storage/v1/bucket",
        headers=_supabase_headers(supabase_key),
        json={"id": SUPABASE_BUCKET, "name": SUPABASE_BUCKET, "public": True},
        timeout=UPLOAD_TIMEOUT_SECONDS,
    )
    if resp.ok:
        return
    if "already exists" in resp.text.lower() or "duplicate" in resp.text.lower():
        return
    raise SpeechError(
        f"创建 Supabase bucket 失败，HTTP {resp.status_code}，原始响应：\n{resp.text}"
    )


def _upload(audio: bytes, filename: str, supabase_url: str,
            supabase_key: str) -> None:
    resp = requests.post(
        f"{supabase_url}/storage/v1/object/{SUPABASE_BUCKET}/{filename}",
        headers={**_supabase_headers(supabase_key), "Content-Type": "audio/mpeg"},
        data=audio,
        timeout=UPLOAD_TIMEOUT_SECONDS,
    )
    if not resp.ok:
        raise SpeechError(
            f"上传 Supabase Storage 失败，HTTP {resp.status_code}，原始响应：\n{resp.text}"
        )


async def speak(text: str, stability: float | None = None,
                style: float | None = None,
                speed: float | None = None) -> str:
    stability = DEFAULT_STABILITY if stability is None else float(stability)
    style = DEFAULT_STYLE if style is None else float(style)
    speed = DEFAULT_SPEED if speed is None else float(speed)

    try:
        audio_url = await asyncio.to_thread(
            generate_speech, text,
            stability=stability, style=style, speed=speed,
        )
    except SpeechError as e:
        logger.warning(f"[speak] failed: {_redact(str(e))}")
        return f"❌ 语音生成失败：{_redact(str(e))}"
    except Exception as e:
        logger.warning(f"[speak] failed: {_redact(f'{type(e).__name__}: {e}')}")
        return f"❌ 语音生成失败：{_redact(f'{type(e).__name__}: {e}')}"

    logger.info(f"[speak] ok url={audio_url}")
    push_result = await _bark_core.bark_push(
        title=_PUSH_TITLE,
        body=text.strip()[:_PUSH_BODY_CHARS],
        url=audio_url,
    )
    return f"✅ 语音已生成：{audio_url}\n推送：{push_result}"
