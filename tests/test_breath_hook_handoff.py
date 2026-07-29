# ============================================================
# SessionStart 钩子的交接班两段
#
# 2026-07-29 用户实测：问「我最近怎么样」不调任何工具，直接拿上下文里已有
# 的内容总结。诊断下来根因不是「选错工具」——是开窗时手里那份材料里压根
# 没有最近几天，模型只能拿旧上下文糊。
#
# 修法不是逼模型多调一次 diary_read（那是反方向：用户要的是窗口连贯，
# 不是不停用工具），而是把 diary 最近 3 天 + active plan 放进钩子，
# 开窗即在场、零工具调用。
#
# 这里钉住：
#   ① 两段确实进了钩子正文
#   ② 一个记忆桶都没有、只有 diary/plan 时钩子不能整段返回空
#   ③ 任一段挂掉不影响其余部分（钩子是开窗的必经路径，不能整个炸掉）
#   ④ 配额是真的生效的（钩子正文是每次开窗都要付的固定成本）
# ============================================================

import pytest

from web import hooks


class _FakeResponse:
    def __init__(self, body: str, status_code: int = 200):
        self.body = body
        self.status_code = status_code


def _bucket(**meta):
    content = meta.pop("content", "")
    return {"id": meta.pop("id", "b1"), "content": content, "metadata": meta}


@pytest.fixture
def hook(monkeypatch, tmp_path):
    """把 hooks 模块的外部依赖全换成可控假件，只测拼装逻辑。"""
    captured: dict = {}

    class _Mgr:
        async def list_all(self, include_archive=False):
            return captured.get("buckets", [])

    class _Dehydrator:
        async def dehydrate(self, content, meta):
            return f"[摘要]{content}"

    class _Decay:
        def calculate_score(self, meta):
            return float(meta.get("score", 0))

    monkeypatch.setattr(hooks.sh, "bucket_mgr", _Mgr(), raising=False)
    monkeypatch.setattr(hooks.sh, "dehydrator", _Dehydrator(), raising=False)
    monkeypatch.setattr(hooks.sh, "decay_engine", _Decay(), raising=False)

    async def _noop(*a, **kw):
        return None

    monkeypatch.setattr(hooks.sh, "fire_webhook", _noop, raising=False)
    monkeypatch.setattr(hooks, "_is_hook_request_authorized", lambda r: True)

    routes: dict = {}

    class _Mcp:
        def custom_route(self, path, methods=None):
            def deco(fn):
                routes[path] = fn
                return fn
            return deco

    hooks.register(_Mcp())
    captured["call"] = routes["/breath-hook"]
    return captured


async def _run(hook) -> str:
    resp = await hook["call"](object())
    return resp.body.decode("utf-8") if isinstance(resp.body, bytes) else str(resp.body)


def _stub_diary(monkeypatch, rows):
    import tools.diary.core as diary
    monkeypatch.setattr(diary, "read_rows", lambda days=3: rows)


# -------------------- 两段进正文 --------------------

@pytest.mark.asyncio
async def test_hook_carries_recent_diary(hook, monkeypatch):
    hook["buckets"] = [_bucket(id="x", content="旧事", resolved=False, score=1)]
    _stub_diary(monkeypatch, [("2026-07-27", "出差到周五"), ("2026-07-28", "胃疼两天")])

    body = await _run(hook)

    assert "=== 最近几天 ===" in body
    assert "出差到周五" in body
    assert "胃疼两天" in body
    assert "2026-07-27" in body


@pytest.mark.asyncio
async def test_hook_carries_active_plans_only(hook, monkeypatch):
    hook["buckets"] = [
        _bucket(id="p1", content="答应帮她改简历", type="plan", status="active", created="2026-07-20"),
        _bucket(id="p2", content="已经做完的事", type="plan", status="resolved", created="2026-07-19"),
        _bucket(id="p3", content="不做了", type="plan", status="abandoned", created="2026-07-18"),
    ]
    _stub_diary(monkeypatch, [])

    body = await _run(hook)

    assert "=== 还欠着的 ===" in body
    assert "答应帮她改简历" in body
    assert "已经做完的事" not in body
    assert "不做了" not in body


# -------------------- 空桶也要返回 --------------------

@pytest.mark.asyncio
async def test_hook_is_not_empty_when_only_diary_exists(hook, monkeypatch):
    """一个可浮现的记忆桶都没有，但 diary 有东西——交接材料不该被整段丢掉。"""
    hook["buckets"] = []
    _stub_diary(monkeypatch, [("2026-07-28", "这周赶一个活")])

    body = await _run(hook)

    assert body != ""
    assert "这周赶一个活" in body


@pytest.mark.asyncio
async def test_hook_is_still_empty_with_nothing_at_all(hook, monkeypatch):
    hook["buckets"] = []
    _stub_diary(monkeypatch, [])

    assert await _run(hook) == ""


# -------------------- 单段失败不拖垮整体 --------------------

@pytest.mark.asyncio
async def test_diary_failure_does_not_break_the_hook(hook, monkeypatch):
    hook["buckets"] = [_bucket(id="x", content="旧事", resolved=False, score=1)]

    import tools.diary.core as diary

    def boom(days=3):
        raise RuntimeError("diary.db 挂了")

    monkeypatch.setattr(diary, "read_rows", boom)

    body = await _run(hook)

    assert "[摘要]旧事" in body
    assert "=== 最近几天 ===" not in body


# -------------------- 配额 --------------------

@pytest.mark.asyncio
async def test_diary_rows_are_capped(hook, monkeypatch):
    _stub_diary(monkeypatch, [("2026-07-28", f"第{i}条") for i in range(40)])
    hook["buckets"] = []

    body = await _run(hook)

    assert body.count("- 第") == hooks._DIARY_HOOK_MAX_ROWS
    # 保留的是最近的那些，不是最早的
    assert "第39条" in body
    assert "第0条" not in body


@pytest.mark.asyncio
async def test_plans_are_capped_and_say_where_the_rest_is(hook, monkeypatch):
    hook["buckets"] = [
        _bucket(id=f"p{i}", content=f"承诺{i}", type="plan", status="active",
                created=f"2026-07-{i + 1:02d}")
        for i in range(hooks._PLAN_HOOK_MAX_ROWS + 5)
    ]
    _stub_diary(monkeypatch, [])

    body = await _run(hook)

    assert body.count("- 2026-07-") == hooks._PLAN_HOOK_MAX_ROWS
    assert "还有 5 条" in body
    assert "dream" in body


@pytest.mark.asyncio
async def test_long_diary_entries_are_truncated(hook, monkeypatch):
    _stub_diary(monkeypatch, [("2026-07-28", "长" * 900)])
    hook["buckets"] = []

    body = await _run(hook)

    assert "长" * hooks._DIARY_HOOK_MAX_CHARS in body
    assert "长" * (hooks._DIARY_HOOK_MAX_CHARS + 1) not in body
