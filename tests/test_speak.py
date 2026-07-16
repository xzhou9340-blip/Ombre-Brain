# ============================================================
# speak 单元测试
# requests 用 monkeypatch 替身、Bark 走 httpx.MockTransport，
# 不发真实网络请求、不需要真实凭证。
# 重点覆盖：
#   ① 全链路成功：ElevenLabs 请求形状(模型/语言/voice_settings/头)、
#      Supabase bucket 创建+上传、返回公开 URL、Bark 推送带 url 参数
#      且 body=台词前 20 字
#   ② bucket 已存在时忽略继续
#   ③ ElevenLabs 报错原样带回 + key 打码
#   ④ 缺环境变量时返回可读提示
#   ⑤ 推送失败不影响返回音频 URL
# ============================================================

import httpx
import pytest

from tools.bark import core as bark_core
from tools.speak import core as speak_core

_FAKE_EL_KEY = "fake_elevenlabs_key_123"
_FAKE_VOICE_ID = "fakeVoice456"
_FAKE_SB_URL = "https://fakeproj.supabase.co"
_FAKE_SB_KEY = "sb_secret_fake_service_key_789"
_FAKE_BARK_KEY = "fakeBarkKey123"
_FAKE_MP3 = b"ID3fake-mp3-bytes"


class _FakeResp:
    def __init__(self, status=200, content=b"", text=""):
        self.status_code = status
        self.content = content
        self.text = text

    @property
    def ok(self):
        return self.status_code < 400


def _fake_requests_post(seen, *, tts_status=200, tts_text="",
                        bucket_status=200, bucket_text=""):
    """替身 requests.post：按 URL 分发 ElevenLabs / bucket 创建 / 上传。"""
    def post(url, **kw):
        if "api.elevenlabs.io" in url:
            seen["tts"] = {"url": url, **kw}
            if tts_status != 200:
                return _FakeResp(tts_status, text=tts_text)
            return _FakeResp(200, content=_FAKE_MP3)
        if url.endswith("/storage/v1/bucket"):
            seen["bucket"] = {"url": url, **kw}
            return _FakeResp(bucket_status, text=bucket_text)
        if "/storage/v1/object/voices/" in url:
            seen["upload"] = {"url": url, **kw}
            return _FakeResp(200, text="{}")
        raise AssertionError(f"unexpected url {url}")
    return post


def _mock_bark_client(handler):
    def factory():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return factory


class TestSpeak:
    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        monkeypatch.setenv("ELEVENLABS_API_KEY", _FAKE_EL_KEY)
        monkeypatch.setenv("ELEVENLABS_VOICE_ID", _FAKE_VOICE_ID)
        monkeypatch.setenv("SUPABASE_URL", _FAKE_SB_URL)
        monkeypatch.setenv("SUPABASE_SERVICE_KEY", _FAKE_SB_KEY)
        monkeypatch.setenv("BARK_KEY", _FAKE_BARK_KEY)

    @pytest.mark.asyncio
    async def test_full_flow_success(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(speak_core.requests, "post", _fake_requests_post(seen))

        bark_seen = {}

        def bark_handler(request: httpx.Request) -> httpx.Response:
            bark_seen["raw_path"] = request.url.raw_path
            bark_seen["url_param"] = request.url.params.get("url")
            return httpx.Response(200, json={"code": 200, "message": "success"})

        monkeypatch.setattr(bark_core, "_make_client", _mock_bark_client(bark_handler))

        text = "宝,今天降温了,记得穿外套,别冻着自己了好吗"  # >20 字,验证截断
        out = await speak_core.speak(text)

        # ElevenLabs 请求形状
        tts = seen["tts"]
        assert tts["url"].endswith(f"/v1/text-to-speech/{_FAKE_VOICE_ID}")
        assert tts["headers"]["xi-api-key"] == _FAKE_EL_KEY
        assert tts["params"]["output_format"] == "mp3_44100_128"
        assert tts["json"]["model_id"] == "eleven_v3"
        assert tts["json"]["language_code"] == "zh"
        assert tts["json"]["voice_settings"] == {
            "stability": 0.34, "style": 0.84, "speed": 1.2,
        }

        # bucket 创建 + 上传
        assert seen["bucket"]["json"] == {"id": "voices", "name": "voices", "public": True}
        up = seen["upload"]
        assert up["headers"]["Content-Type"] == "audio/mpeg"
        assert up["headers"]["Authorization"] == f"Bearer {_FAKE_SB_KEY}"
        assert up["data"] == _FAKE_MP3

        # 返回：音频 URL + 推送结果
        assert out.startswith("✅")
        audio_url_prefix = f"{_FAKE_SB_URL}/storage/v1/object/public/voices/"
        assert audio_url_prefix in out and ".mp3" in out
        assert "推送" in out and "code=200" in out

        # Bark：标题「克」+ body=前 20 字 + url 参数带音频地址
        from urllib.parse import quote
        assert bark_seen["raw_path"].startswith(
            f"/{_FAKE_BARK_KEY}/{quote('克', safe='')}/{quote(text[:20], safe='')}".encode()
        )
        assert bark_seen["url_param"].startswith(audio_url_prefix)

    @pytest.mark.asyncio
    async def test_custom_params_passed_through(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(speak_core.requests, "post", _fake_requests_post(seen))
        monkeypatch.setattr(bark_core, "_make_client", _mock_bark_client(
            lambda r: httpx.Response(200, json={"code": 200, "message": "success"})
        ))
        await speak_core.speak("测试", stability=0.5, style=0.2, speed=0.9)
        assert seen["tts"]["json"]["voice_settings"] == {
            "stability": 0.5, "style": 0.2, "speed": 0.9,
        }

    @pytest.mark.asyncio
    async def test_bucket_already_exists_ignored(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(speak_core.requests, "post", _fake_requests_post(
            seen, bucket_status=400,
            bucket_text='{"statusCode":"409","error":"Duplicate","message":"The resource already exists"}',
        ))
        monkeypatch.setattr(bark_core, "_make_client", _mock_bark_client(
            lambda r: httpx.Response(200, json={"code": 200, "message": "success"})
        ))
        out = await speak_core.speak("测试")
        assert out.startswith("✅") and "upload" in seen

    @pytest.mark.asyncio
    async def test_elevenlabs_error_verbatim_and_redacted(self, monkeypatch):
        seen = {}
        # 极端假设：错误体把 key 回显了，也必须打码；其余内容原样带回
        monkeypatch.setattr(speak_core.requests, "post", _fake_requests_post(
            seen, tts_status=401,
            tts_text=f'{{"detail":{{"status":"invalid_api_key","message":"bad key {_FAKE_EL_KEY}"}}}}',
        ))
        out = await speak_core.speak("测试")
        assert out.startswith("❌") and "HTTP 401" in out
        assert "invalid_api_key" in out          # 原始错误不吞
        assert _FAKE_EL_KEY not in out           # key 绝不出现在返回内容里
        assert "***ELEVENLABS_KEY***" in out
        assert "bucket" not in seen              # TTS 失败后不再碰 Supabase

    @pytest.mark.asyncio
    async def test_missing_env(self, monkeypatch):
        monkeypatch.delenv("ELEVENLABS_VOICE_ID", raising=False)
        called = {}
        monkeypatch.setattr(speak_core.requests, "post",
                            lambda *a, **k: called.setdefault("hit", True))
        out = await speak_core.speak("测试")
        assert out.startswith("❌") and "ELEVENLABS_VOICE_ID" in out
        assert "hit" not in called               # 缺配置时不发任何请求

    @pytest.mark.asyncio
    async def test_empty_text_rejected(self):
        out = await speak_core.speak("  ")
        assert out.startswith("❌")

    @pytest.mark.asyncio
    async def test_push_failure_still_returns_audio_url(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(speak_core.requests, "post", _fake_requests_post(seen))
        monkeypatch.setattr(bark_core, "_make_client", _mock_bark_client(
            lambda r: httpx.Response(400, json={"code": 400, "message": "device key error"})
        ))
        out = await speak_core.speak("测试")
        # 音频已生成：URL 必须在；推送失败如实带回
        assert f"{_FAKE_SB_URL}/storage/v1/object/public/voices/" in out
        assert "device key error" in out
