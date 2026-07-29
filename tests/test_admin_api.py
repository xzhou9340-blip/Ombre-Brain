# ============================================================
# /admin/* 整理后台（任务书 §3.1）
#
# 这组接口能读全库正文、能批量删除，所以测试的重点不是「功能跑通」，
# 是几条不能破的约束：
#   ① 没登录态也没 token → 拒绝（不能有「没配就放行」的默认值）
#   ② 任务书的验收用例：筛出「2026-06 以前、importance ≤ 3、未 resolved」
#   ③ 删除必须先导出；导出失败一条都不许删
#   ④ 删除必须二次确认（confirm）
#   ⑤ 明确不做「批量迁往 diary」——要拒绝并说明理由，不是静默不支持
#   ⑥ 归档可还原
# ============================================================

import json
import os

import pytest

from web import admin


# -------------------- 假件 --------------------

class _FakeRequest:
    def __init__(self, query=None, headers=None, body=None, path_params=None):
        self.query_params = query or {}
        self.headers = headers or {}
        self._body = body
        self.path_params = path_params or {}

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


def _bucket(bid, content="", **meta):
    meta.setdefault("name", bid)
    meta.setdefault("type", "dynamic")
    meta.setdefault("importance", 5)
    meta.setdefault("created", "2026-07-01T10:00:00")
    return {"id": bid, "content": content, "metadata": meta}


class _FakeMgr:
    def __init__(self, buckets):
        self._buckets = {b["id"]: b for b in buckets}
        self.updates = []
        self.archived = []
        self.unarchived = []
        self.deleted = []
        self.unarchive_ok = True

    async def list_all(self, include_archive=False):
        return list(self._buckets.values())

    async def get(self, bid):
        return self._buckets.get(bid)

    async def update(self, bid, **kw):
        self.updates.append((bid, kw))
        self._buckets[bid]["metadata"].update(kw)
        return True

    async def archive(self, bid):
        self.archived.append(bid)
        self._buckets[bid]["metadata"]["type"] = "archived"
        return True

    async def unarchive(self, bid):
        if not self.unarchive_ok:
            return False
        self.unarchived.append(bid)
        return True

    async def delete(self, bid):
        self.deleted.append(bid)
        self._buckets.pop(bid, None)
        return True


class _Routes(dict):
    def custom_route(self, path, methods=None):
        def deco(fn):
            for m in (methods or ["GET"]):
                self[(m, path)] = fn
            return fn
        return deco


@pytest.fixture
def app(monkeypatch, tmp_path):
    """装配路由 + 假 bucket_mgr；默认「已登录」，鉴权用例再单独覆盖。"""
    buckets = [
        _bucket("old-low", "很久以前的小事", created="2026-03-02T09:00:00",
                importance=2, resolved=False, tags=["work"]),
        _bucket("old-low-2", "另一条旧的小事", created="2026-05-30T09:00:00",
                importance=3, resolved=False),
        _bucket("old-high", "旧但重要", created="2026-03-05T09:00:00",
                importance=9, resolved=False),
        _bucket("old-done", "旧的小事但已放下", created="2026-04-01T09:00:00",
                importance=1, resolved=True),
        _bucket("new-low", "最近的小事", created="2026-07-20T09:00:00",
                importance=2, resolved=False),
        _bucket("gone", "已归档的", created="2026-02-01T09:00:00",
                importance=2, resolved=False, type="archived"),
    ]
    mgr = _FakeMgr(buckets)

    class _Decay:
        threshold = 0.3

        def calculate_score(self, meta):
            return float(meta.get("importance", 5)) / 10.0

    monkeypatch.setattr(admin.sh, "bucket_mgr", mgr, raising=False)
    monkeypatch.setattr(admin.sh, "decay_engine", _Decay(), raising=False)
    monkeypatch.setattr(admin.sh, "config", {"buckets_dir": str(tmp_path)}, raising=False)
    monkeypatch.setattr(admin.sh, "_require_auth", lambda r: None)

    routes = _Routes()
    admin.register(routes)
    routes.mgr = mgr
    routes.tmp = tmp_path
    return routes


async def _call(app, method, path, **kw):
    return await app[(method, path)](_FakeRequest(**kw))


def _json(resp):
    return json.loads(resp.body.decode("utf-8"))


# -------------------- ① 鉴权 --------------------

@pytest.mark.asyncio
async def test_denied_without_session_or_token(app, monkeypatch):
    """没登录态、也没配 token —— 必须拒绝，不能默认放行。"""
    monkeypatch.setattr(admin.sh, "_require_auth", lambda r: object())
    monkeypatch.delenv("OMBRE_ADMIN_TOKEN", raising=False)
    monkeypatch.setattr(admin.sh, "config", {"buckets_dir": str(app.tmp)}, raising=False)

    resp = await _call(app, "GET", "/admin/buckets")

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_token_in_header_is_accepted(app, monkeypatch):
    monkeypatch.setattr(admin.sh, "_require_auth", lambda r: object())
    monkeypatch.setenv("OMBRE_ADMIN_TOKEN", "s3cret")

    ok = await _call(app, "GET", "/admin/buckets", headers={"x-ombre-admin-token": "s3cret"})
    bad = await _call(app, "GET", "/admin/buckets", headers={"x-ombre-admin-token": "nope"})

    assert ok.status_code == 200
    assert bad.status_code == 403


@pytest.mark.asyncio
async def test_batch_delete_is_denied_without_auth(app, monkeypatch):
    """最危险的那个操作也走同一道门。"""
    monkeypatch.setattr(admin.sh, "_require_auth", lambda r: object())
    monkeypatch.delenv("OMBRE_ADMIN_TOKEN", raising=False)

    resp = await _call(app, "POST", "/admin/buckets/batch",
                       body={"action": "delete", "ids": ["old-low"], "confirm": True})

    assert resp.status_code == 403
    assert app.mgr.deleted == []


# -------------------- ② 任务书的验收用例 --------------------

@pytest.mark.asyncio
async def test_acceptance_filter_old_low_importance_unresolved(app):
    """2026-06 以前 + importance ≤ 3 + 未 resolved。"""
    resp = await _call(app, "GET", "/admin/buckets", query={
        "created_before": "2026-06-01",
        "importance_max": "3",
        "resolved": "0",
        "archived": "0",
    })

    ids = [r["id"] for r in _json(resp)["items"]]
    assert sorted(ids) == ["old-low", "old-low-2"]


@pytest.mark.asyncio
async def test_acceptance_then_batch_mark(app):
    """筛完能批量标记——验收要求的是这一整条链路。"""
    listed = _json(await _call(app, "GET", "/admin/buckets", query={
        "created_before": "2026-06-01", "importance_max": "3",
        "resolved": "0", "archived": "0",
    }))
    ids = [r["id"] for r in listed["items"]]

    resp = await _call(app, "POST", "/admin/buckets/batch",
                       body={"action": "set_resolved", "ids": ids, "resolved": True})

    assert _json(resp)["counts"]["done"] == 2
    assert all(app.mgr._buckets[i]["metadata"]["resolved"] is True for i in ids)


@pytest.mark.asyncio
async def test_keyword_and_tag_filters(app):
    by_word = _json(await _call(app, "GET", "/admin/buckets", query={"q": "很久以前"}))
    by_tag = _json(await _call(app, "GET", "/admin/buckets", query={"tags": "work"}))

    assert [r["id"] for r in by_word["items"]] == ["old-low"]
    assert [r["id"] for r in by_tag["items"]] == ["old-low"]


@pytest.mark.asyncio
async def test_pagination_reports_totals(app):
    page1 = _json(await _call(app, "GET", "/admin/buckets",
                              query={"page_size": "2", "page": "1"}))

    assert page1["total"] == 6
    assert page1["pages"] == 3
    assert len(page1["items"]) == 2


# -------------------- ③④ 删除：先导出 + 二次确认 --------------------

@pytest.mark.asyncio
async def test_delete_requires_confirm(app):
    resp = await _call(app, "POST", "/admin/buckets/batch",
                       body={"action": "delete", "ids": ["old-low"]})

    assert resp.status_code == 400
    assert "confirm" in _json(resp)["error"]
    assert app.mgr.deleted == []


@pytest.mark.asyncio
async def test_delete_exports_before_removing(app):
    resp = await _call(app, "POST", "/admin/buckets/batch",
                       body={"action": "delete", "ids": ["old-low", "old-high"], "confirm": True})
    data = _json(resp)

    assert app.mgr.deleted == ["old-low", "old-high"]
    export = data["export_path"]
    assert os.path.isfile(export)

    with open(export, encoding="utf-8") as f:
        dumped = json.load(f)
    assert dumped["count"] == 2
    # 导出的必须是可恢复的内容，不能只有 id
    bodies = {b["id"]: b["content"] for b in dumped["buckets"]}
    assert bodies["old-low"] == "很久以前的小事"


@pytest.mark.asyncio
async def test_nothing_is_deleted_when_the_export_fails(app, monkeypatch):
    """导不出来就整单不删——否则删完才发现没有恢复依据。"""
    async def boom(_buckets):
        raise OSError("盘满了")

    monkeypatch.setattr(admin, "_export_before_delete", boom)

    resp = await _call(app, "POST", "/admin/buckets/batch",
                       body={"action": "delete", "ids": ["old-low"], "confirm": True})

    assert resp.status_code == 500
    assert "未删除任何内容" in _json(resp)["error"]
    assert app.mgr.deleted == []


# -------------------- ⑤ 明确不做的事 --------------------

@pytest.mark.asyncio
async def test_migrate_to_diary_is_refused_with_a_reason(app):
    """不是「未实现」，是「明确不做」——理由要说出来，否则下一个人会来补上。"""
    resp = await _call(app, "POST", "/admin/buckets/batch",
                       body={"action": "move_to_diary", "ids": ["old-low"]})
    data = _json(resp)

    assert resp.status_code == 400
    assert "diary" in data["error"]
    assert "7 天" in data["reason"]
    assert app.mgr.updates == []


@pytest.mark.asyncio
async def test_unknown_action_lists_what_is_allowed(app):
    resp = await _call(app, "POST", "/admin/buckets/batch",
                       body={"action": "explode", "ids": ["old-low"]})

    assert resp.status_code == 400
    assert "set_resolved" in _json(resp)["allowed"]


@pytest.mark.asyncio
async def test_batch_size_is_capped(app):
    resp = await _call(app, "POST", "/admin/buckets/batch", body={
        "action": "set_resolved",
        "ids": [f"id{i}" for i in range(admin._MAX_BATCH + 1)],
    })

    assert resp.status_code == 400
    assert app.mgr.updates == []


# -------------------- ⑥ 归档还原 --------------------

@pytest.mark.asyncio
async def test_unarchive_batch(app):
    resp = await _call(app, "POST", "/admin/buckets/batch",
                       body={"action": "unarchive", "ids": ["gone"]})

    assert _json(resp)["counts"]["done"] == 1
    assert app.mgr.unarchived == ["gone"]


@pytest.mark.asyncio
async def test_unarchive_failure_is_reported_not_swallowed(app):
    app.mgr.unarchive_ok = False

    resp = await _call(app, "POST", "/admin/buckets/batch",
                       body={"action": "unarchive", "ids": ["gone"]})
    data = _json(resp)

    assert data["counts"]["done"] == 0
    assert data["errors"][0]["id"] == "gone"


# -------------------- 单条编辑 --------------------

@pytest.mark.asyncio
async def test_patch_updates_only_given_fields(app):
    resp = await _call(app, "PATCH", "/admin/buckets/{bucket_id}",
                       path_params={"bucket_id": "old-low"},
                       body={"importance": 7, "tags": ["work", "整理过"]})

    assert _json(resp)["updated"] == ["importance", "tags"]
    bid, kw = app.mgr.updates[-1]
    assert bid == "old-low"
    assert set(kw) == {"importance", "tags"}


@pytest.mark.asyncio
async def test_patch_clamps_importance(app):
    await _call(app, "PATCH", "/admin/buckets/{bucket_id}",
                path_params={"bucket_id": "old-low"}, body={"importance": 99})

    assert app.mgr.updates[-1][1]["importance"] == 10


@pytest.mark.asyncio
async def test_patch_rejects_empty_body(app):
    resp = await _call(app, "PATCH", "/admin/buckets/{bucket_id}",
                       path_params={"bucket_id": "old-low"}, body={})

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_patch_404_for_missing_bucket(app):
    resp = await _call(app, "PATCH", "/admin/buckets/{bucket_id}",
                       path_params={"bucket_id": "nope"}, body={"importance": 3})

    assert resp.status_code == 404


# -------------------- 概览 --------------------

@pytest.mark.asyncio
async def test_stats_counts_and_at_risk(app):
    data = _json(await _call(app, "GET", "/admin/stats"))

    assert data["total"] == 6
    assert data["archived"] == 1
    assert data["resolved"] == 1
    assert data["unresolved"] == 5
    # score = importance/10；threshold 0.3 → importance < 3 且未归档未 pinned
    assert data["at_risk"] == 3
    assert data["by_type"]["archived"] == 1
