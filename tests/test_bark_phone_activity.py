# ============================================================
# bark_push / phone_activity_query 单元测试
# 全部走 httpx.MockTransport，不发真实网络请求、不需要真实凭证。
# 重点覆盖：
#   ① bark_push：URL encode（中文）、icon query、成功/失败返回、key 打码
#   ② phone_activity_query：请求头/参数、聚合+明细、UTC+8 转换、key 打码
# ============================================================

import json

import httpx
import pytest

from tools.bark import core as bark_core
from tools.phone_activity import core as phone_core

_FAKE_BARK_KEY = "fakeBarkKey123"
_FAKE_SB_URL = "https://fakeproj.supabase.co"
_FAKE_SB_KEY = "sb_secret_fake_service_key_456"


def _mock_client(handler):
    """返回一个可替换 _make_client 的工厂：AsyncClient + MockTransport。"""
    def factory():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return factory


# ------------------------------------------------------------
# bark_push
# ------------------------------------------------------------
class TestBarkPush:
    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        monkeypatch.setenv("BARK_KEY", _FAKE_BARK_KEY)

    @pytest.mark.asyncio
    async def test_success_encodes_chinese_and_icon(self, monkeypatch):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["raw_path"] = request.url.raw_path
            seen["icon"] = request.url.params.get("icon")
            return httpx.Response(200, json={"code": 200, "message": "success", "timestamp": 1})

        monkeypatch.setattr(bark_core, "_make_client", _mock_client(handler))
        out = await bark_core.bark_push("测试", "管道通了", icon="https://example.com/i.png")
        # 中文必须 URL encode 进路径（看 wire 上的 raw_path）；key 在路径第一段
        assert seen["raw_path"].startswith(
            f"/{_FAKE_BARK_KEY}/%E6%B5%8B%E8%AF%95/%E7%AE%A1%E9%81%93%E9%80%9A%E4%BA%86".encode()
        )
        assert seen["icon"] == "https://example.com/i.png"
        assert out.startswith("✅")
        assert "code=200" in out and "success" in out

    @pytest.mark.asyncio
    async def test_body_slash_is_encoded_not_path(self, monkeypatch):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["raw_path"] = request.url.raw_path
            return httpx.Response(200, json={"code": 200, "message": "success"})

        monkeypatch.setattr(bark_core, "_make_client", _mock_client(handler))
        await bark_core.bark_push("a/b", "c/d")
        # 斜杠必须被编码，不能变成额外路径段（看 wire 上的 raw_path）
        assert seen["raw_path"] == f"/{_FAKE_BARK_KEY}/a%2Fb/c%2Fd".encode()

    @pytest.mark.asyncio
    async def test_url_param_passed(self, monkeypatch):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = request.url.params.get("url")
            return httpx.Response(200, json={"code": 200, "message": "success"})

        monkeypatch.setattr(bark_core, "_make_client", _mock_client(handler))
        out = await bark_core.bark_push("克", "语音", url="https://x.supabase.co/a.mp3")
        assert seen["url"] == "https://x.supabase.co/a.mp3"
        assert out.startswith("✅")

    @pytest.mark.asyncio
    async def test_failure_returns_bark_error_verbatim(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"code": 400, "message": "device key error"})

        monkeypatch.setattr(bark_core, "_make_client", _mock_client(handler))
        out = await bark_core.bark_push("测试", "x")
        assert out.startswith("❌")
        assert "HTTP 400" in out and "code=400" in out and "device key error" in out

    @pytest.mark.asyncio
    async def test_network_error_redacts_key(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"connect fail for https://api.day.app/{_FAKE_BARK_KEY}/x")

        monkeypatch.setattr(bark_core, "_make_client", _mock_client(handler))
        out = await bark_core.bark_push("t", "b")
        assert out.startswith("❌")
        assert _FAKE_BARK_KEY not in out           # key 绝不出现在返回内容里
        assert "***BARK_KEY***" in out

    @pytest.mark.asyncio
    async def test_missing_key_env(self, monkeypatch):
        monkeypatch.delenv("BARK_KEY", raising=False)
        out = await bark_core.bark_push("t", "b")
        assert "BARK_KEY" in out and out.startswith("❌")

    @pytest.mark.asyncio
    async def test_empty_title_and_body_rejected(self):
        out = await bark_core.bark_push("", "  ")
        assert out.startswith("❌")


# ------------------------------------------------------------
# phone_activity_query
# ------------------------------------------------------------
class TestPhoneActivityQuery:
    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_URL", _FAKE_SB_URL)
        monkeypatch.setenv("SUPABASE_SERVICE_KEY", _FAKE_SB_KEY)

    @pytest.mark.asyncio
    async def test_query_headers_params_and_output(self, monkeypatch):
        seen = {}
        rows = [
            # 06:30 UTC = 14:30 UTC+8
            {"id": 3, "app_name": "小红书", "opened_at": "2026-07-13T06:30:00Z"},
            {"id": 2, "app_name": "微信", "opened_at": "2026-07-13T05:00:00+00:00", "note": "回消息"},
            {"id": 1, "app_name": "小红书", "opened_at": "2026-07-13T04:00:00Z"},
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            seen["headers"] = request.headers
            seen["params"] = dict(request.url.params)
            seen["path"] = request.url.path
            return httpx.Response(200, json=rows)

        monkeypatch.setattr(phone_core, "_make_client", _mock_client(handler))
        out = await phone_core.phone_activity_query(hours=24)

        # 请求形状：REST 路径 + apikey/Bearer 头 + gte 过滤 + 倒序
        assert seen["path"] == "/rest/v1/phone_activity"
        assert seen["headers"]["apikey"] == _FAKE_SB_KEY
        assert seen["headers"]["authorization"] == f"Bearer {_FAKE_SB_KEY}"
        assert seen["params"]["opened_at"].startswith("gte.")
        assert seen["params"]["order"] == "opened_at.desc"

        # 输出：时区标注 + UTC+8 转换
        assert "UTC+8" in out
        assert "2026-07-13 14:30:00" in out    # 06:30Z → 14:30 UTC+8
        assert "2026-07-13 13:00:00" in out    # 05:00Z → 13:00 UTC+8
        # 聚合：小红书 ×2（排最前）、微信 ×1 + 最后打开时间
        agg_part = out.split("—— 明细")[0]
        assert "小红书 ×2" in agg_part and "最后打开 2026-07-13 14:30:00" in agg_part
        assert "微信 ×1" in agg_part
        assert agg_part.index("小红书 ×2") < agg_part.index("微信 ×1")
        # 明细：时间倒序 + 附加字段
        detail_part = out.split("—— 明细")[1]
        assert detail_part.index("14:30:00") < detail_part.index("13:00:00") < detail_part.index("12:00:00")
        assert "note=回消息" in detail_part
        # service key 绝不出现在返回内容里
        assert _FAKE_SB_KEY not in out

    @pytest.mark.asyncio
    async def test_empty_result(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[])

        monkeypatch.setattr(phone_core, "_make_client", _mock_client(handler))
        out = await phone_core.phone_activity_query(hours=6)
        assert "6 小时" in out and "没有" in out

    @pytest.mark.asyncio
    async def test_http_error_redacts_key(self, monkeypatch):
        def handler(request: httpx.Request) -> httpx.Response:
            # 极端假设：错误体把 key 回显了，也必须打码
            return httpx.Response(401, text=json.dumps({"message": f"bad key {_FAKE_SB_KEY}"}))

        monkeypatch.setattr(phone_core, "_make_client", _mock_client(handler))
        out = await phone_core.phone_activity_query()
        assert out.startswith("❌") and "HTTP 401" in out
        assert _FAKE_SB_KEY not in out
        assert "***SERVICE_KEY***" in out

    @pytest.mark.asyncio
    async def test_missing_env(self, monkeypatch):
        monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
        out = await phone_core.phone_activity_query()
        assert "SUPABASE_SERVICE_KEY" in out and out.startswith("❌")

    @pytest.mark.asyncio
    async def test_invalid_hours_falls_back_to_default(self, monkeypatch):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["params"] = dict(request.url.params)
            return httpx.Response(200, json=[])

        monkeypatch.setattr(phone_core, "_make_client", _mock_client(handler))
        out = await phone_core.phone_activity_query(hours=0)
        assert "24 小时" in out
