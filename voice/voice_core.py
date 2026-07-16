"""TTS 核心逻辑:ElevenLabs 生成语音 → 上传 Supabase Storage → 返回公开 URL。

第二阶段会把 generate_speech 原样搬进 ombre 仓库注册为 MCP 工具,因此本模块:
- 只依赖 requests 和环境变量,不碰命令行参数、不写任何本地文件
- 所有错误抛 SpeechError,并携带 API 返回的原始错误信息(不吞)

环境变量:
    ELEVENLABS_API_KEY    ElevenLabs API key
    ELEVENLABS_VOICE_ID   ElevenLabs voice id
    SUPABASE_URL          https://<project>.supabase.co
    SUPABASE_SERVICE_KEY  Supabase service_role key(权限很大,只放服务器环境变量)
"""

import os
from datetime import datetime

import requests

ELEVENLABS_MODEL_ID = "eleven_v3"
ELEVENLABS_LANGUAGE_CODE = "zh"
ELEVENLABS_OUTPUT_FORMAT = "mp3_44100_128"
SUPABASE_BUCKET = "voices"

TTS_TIMEOUT_SECONDS = 120
UPLOAD_TIMEOUT_SECONDS = 60


class SpeechError(RuntimeError):
    """TTS 或上传失败。message 中包含对端返回的原始错误信息。"""


def generate_speech(text: str, stability: float = 0.34, style: float = 0.84,
                    speed: float = 1.2) -> str:
    """把台词转成语音并上传到 Supabase Storage,返回公开访问 URL。"""
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
        raise SpeechError(f"缺少环境变量 {name}")
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
            f"ElevenLabs 返回 HTTP {resp.status_code},原始响应:\n{resp.text}"
        )
    if not resp.content:
        raise SpeechError("ElevenLabs 返回 200 但音频内容为空")
    return resp.content


def _supabase_headers(supabase_key: str) -> dict:
    return {"Authorization": f"Bearer {supabase_key}", "apikey": supabase_key}


def _ensure_bucket(supabase_url: str, supabase_key: str) -> None:
    """确保 public bucket 存在;已存在时 Supabase 返回错误,识别后忽略。"""
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
        f"创建 Supabase bucket 失败,HTTP {resp.status_code},原始响应:\n{resp.text}"
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
            f"上传 Supabase Storage 失败,HTTP {resp.status_code},原始响应:\n{resp.text}"
        )
