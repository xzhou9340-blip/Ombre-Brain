"""
========================================
web/admin.py — 记忆库整理后台（任务书 §3.1）
========================================

仪表板的「记忆」页是给日常看的：一屏一屏翻。整理是另一回事——要一次筛出
几十条、一次改完、改错了还能回头。这组 /admin/* 就是为整理这件事做的。

不新建服务：挂在现有 HTTP 栈上，与 dashboard 共用同一个进程、同一个
bucket_mgr。前端是单个静态 HTML（frontend/admin.html），原生 JS + fetch，
不用框架不用构建。

关键行为：
- 鉴权双通道：Dashboard 登录态，或 OMBRE_ADMIN_TOKEN / config admin.token。
  两者都没有 → 403 拒绝（fail closed）。整理后台能读全库正文、能批量删除，
  不给「没配 token 就放行」这种默认值。
- 批量删除**先导出再删**，导出失败就整单不删。导出落在
  <buckets_dir>/_admin_exports/ 里，不依赖浏览器把文件收好。
- 归档可还原（bucket_mgr.unarchive）。归档一直是单向的，而人是会手滑的。

不做什么（边界）：
- **不做「批量迁往 diary」**。diary 只读最近 7 天，把旧桶迁进去等于删除，
  但界面上看起来像「保存了」——那是会骗人的操作。batch 收到这个 action
  会显式拒绝并说明理由，而不是假装没这个功能。
- 不做「批量改 content」：改正文要重建向量、要逐条看，不该批量盲改。
  单条 PATCH 可以改，批量只允许改元数据。
- 不自己实现筛选之外的检索：语义检索是 breath / search 的事，这里只做
  结构化筛选 + 关键词子串匹配，结果可预期、可复现。

对外暴露：register(mcp)。
========================================
"""

import hmac
import json
import os
from datetime import datetime
from typing import Any

from starlette.requests import Request
from starlette.responses import Response

from . import _shared as sh

logger = sh.logger

try:
    from utils import strip_wikilinks  # type: ignore
except ImportError:  # pragma: no cover
    from ..utils import strip_wikilinks  # type: ignore


_DEFAULT_PAGE_SIZE = 50
_MAX_PAGE_SIZE = 200
_EXPORT_DIRNAME = "_admin_exports"
_TOP_TAGS = 30

# 批量操作一次最多处理多少条。不设上限的话，一次 3000 条的误操作要等到
# 半路超时才发现，而前面那些已经改掉了。
_MAX_BATCH = 500

_SORT_KEYS = ("score", "created", "last_active", "importance", "name")

_BATCH_ACTIONS = (
    "add_tags", "remove_tags", "set_resolved", "set_dont_surface",
    "set_importance", "set_pinned", "archive", "unarchive", "delete",
)


# --------------------------------------------------------------
# 鉴权
# --------------------------------------------------------------

def _admin_setting(name: str, default=None):
    admin_cfg = (getattr(sh, "config", {}) or {}).get("admin") or {}
    return admin_cfg.get(name, default)


def _header_value(request, name: str) -> str:
    headers = getattr(request, "headers", {}) or {}
    try:
        return str(headers.get(name, "") or "")
    except Exception:
        wanted = name.lower()
        for k, v in dict(headers).items():
            if str(k).lower() == wanted:
                return str(v or "")
    return ""


def _is_admin_authorized(request) -> bool:
    """Dashboard 登录态 或 admin token。两者都没有就拒绝。

    刻意不提供 allow_public 开关：hook 那边有（它只吐摘要），这里能读全库
    正文、能批量删除，没有任何场景值得为它开一个「公开」的口子。
    """
    if sh._require_auth(request) is None:
        return True

    token = (os.environ.get("OMBRE_ADMIN_TOKEN") or str(_admin_setting("token", "") or "")).strip()
    if not token:
        return False

    auth = _header_value(request, "authorization")
    supplied = [
        str((getattr(request, "query_params", {}) or {}).get("token", "") or ""),
        _header_value(request, "x-ombre-admin-token"),
        auth[7:] if auth.startswith("Bearer ") else "",
    ]
    return any(v and hmac.compare_digest(v, token) for v in supplied)


def _deny() -> Response:
    from starlette.responses import JSONResponse
    return JSONResponse(
        {
            "error": "Unauthorized",
            "hint": "需要 Dashboard 登录态，或在请求头带 X-Ombre-Admin-Token"
                    "（token 来自环境变量 OMBRE_ADMIN_TOKEN 或 config admin.token）",
        },
        status_code=403,
    )


# --------------------------------------------------------------
# 取值 / 筛选
# --------------------------------------------------------------

def _qp(request, name: str, default: str = "") -> str:
    try:
        return str((request.query_params or {}).get(name, default) or default).strip()
    except Exception:
        return default


def _qp_int(request, name: str, default=None):
    raw = _qp(request, name)
    if raw == "":
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


def _qp_bool(request, name: str):
    """三态：未传 → None（不筛），传了 → True/False。"""
    raw = _qp(request, name).lower()
    if raw == "":
        return None
    return raw in ("1", "true", "yes", "on")


def _csv(raw: str) -> list[str]:
    return [p.strip() for p in raw.split(",") if p.strip()]


def _is_archived(meta: dict) -> bool:
    return str(meta.get("type", "")) == "archived"


def _row(bucket: dict) -> dict[str, Any]:
    meta = bucket.get("metadata", {}) or {}
    content = bucket.get("content", "") or ""
    try:
        score = sh.decay_engine.calculate_score(meta)
    except Exception:
        score = 0.0
    return {
        "id": bucket["id"],
        "name": meta.get("name", bucket["id"]),
        "type": meta.get("type", "dynamic"),
        "archived": _is_archived(meta),
        "domain": meta.get("domain", []) or [],
        "tags": meta.get("tags", []) or [],
        "importance": meta.get("importance", 5),
        "resolved": bool(meta.get("resolved", False)),
        "pinned": bool(meta.get("pinned", False)),
        "dont_surface": bool(meta.get("dont_surface", False)),
        "digested": bool(meta.get("digested", False)),
        "created": meta.get("created", ""),
        "last_active": meta.get("last_active", ""),
        "activation_count": meta.get("activation_count", 1),
        "why_remembered": meta.get("why_remembered", ""),
        "score": score,
        "content_preview": strip_wikilinks(content)[:300],
    }


def _matches(row: dict, content: str, f: dict) -> bool:
    """结构化筛选。每个条件都是「没传就不筛」，传了就必须命中。"""
    if f["types"] and row["type"] not in f["types"]:
        return False
    if f["archived"] is not None and row["archived"] is not f["archived"]:
        return False
    if f["resolved"] is not None and row["resolved"] is not f["resolved"]:
        return False
    if f["pinned"] is not None and row["pinned"] is not f["pinned"]:
        return False
    if f["dont_surface"] is not None and row["dont_surface"] is not f["dont_surface"]:
        return False

    if f["domains"] and not (set(f["domains"]) & set(row["domain"])):
        return False
    # tags 是 AND：整理时「同时带这两个标签」才是有用的筛法
    if f["tags"] and not set(f["tags"]).issubset(set(row["tags"])):
        return False

    try:
        importance = int(row["importance"])
    except (TypeError, ValueError):
        importance = 5
    if f["importance_min"] is not None and importance < f["importance_min"]:
        return False
    if f["importance_max"] is not None and importance > f["importance_max"]:
        return False

    # ISO 日期字符串按字典序比较即可，不用解析
    created = str(row["created"] or "")[:10]
    if f["created_before"] and not (created and created < f["created_before"]):
        return False
    if f["created_after"] and not (created and created > f["created_after"]):
        return False

    if f["score_max"] is not None and row["score"] > f["score_max"]:
        return False

    if f["q"]:
        haystack = f"{row['name']}\n{content}".lower()
        if f["q"] not in haystack:
            return False
    return True


def _read_filters(request) -> dict:
    return {
        "q": _qp(request, "q").lower(),
        "types": _csv(_qp(request, "type")),
        "domains": _csv(_qp(request, "domain")),
        "tags": _csv(_qp(request, "tags")),
        "archived": _qp_bool(request, "archived"),
        "resolved": _qp_bool(request, "resolved"),
        "pinned": _qp_bool(request, "pinned"),
        "dont_surface": _qp_bool(request, "dont_surface"),
        "importance_min": _qp_int(request, "importance_min"),
        "importance_max": _qp_int(request, "importance_max"),
        "created_before": _qp(request, "created_before")[:10],
        "created_after": _qp(request, "created_after")[:10],
        "score_max": (lambda v: None if v is None else float(v))(
            _qp(request, "score_max") or None
        ),
    }


# --------------------------------------------------------------
# 批量操作的公共部分
# --------------------------------------------------------------

def _export_dir() -> str:
    base = str((getattr(sh, "config", {}) or {}).get("buckets_dir") or "").strip()
    if not base:
        raise RuntimeError("找不到 buckets_dir，无法写导出文件")
    path = os.path.join(base, _EXPORT_DIRNAME)
    os.makedirs(path, exist_ok=True)
    return path


async def _export_before_delete(buckets: list[dict]) -> str:
    """把即将删除的桶整体写进一个 JSON，返回文件路径。

    写在盘上而不是只回给浏览器：删除是不可逆的，恢复依据不能依赖
    「用户当时有没有把下载存好」。写失败就抛，由调用方整单放弃删除。
    """
    payload = {
        "schema_version": 1,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "reason": "admin batch delete",
        "count": len(buckets),
        "buckets": [
            {
                "id": b["id"],
                "metadata": b.get("metadata", {}),
                "content": b.get("content", ""),
            }
            for b in buckets
        ],
    }
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(_export_dir(), f"deleted-{ts}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return path


def _merge_tags(existing: list, add: list, remove: list) -> list[str]:
    out = [str(t) for t in (existing or [])]
    for t in add:
        if t not in out:
            out.append(t)
    if remove:
        drop = set(remove)
        out = [t for t in out if t not in drop]
    return out


# --------------------------------------------------------------
# 路由
# --------------------------------------------------------------

def register(mcp) -> None:

    @mcp.custom_route("/admin", methods=["GET"])
    async def admin_page(request: Request) -> Response:
        """整理后台页面。

        页面本身不含任何记忆内容，鉴权发生在它调用的 /admin/* 接口上——
        所以这里不拦：拦了的话没有 Dashboard cookie 的人连输 token 的
        输入框都打不开。
        """
        from starlette.responses import HTMLResponse
        page = os.path.join(sh.repo_root, "frontend", "admin.html")
        try:
            with open(page, "r", encoding="utf-8") as f:
                html = f.read()
        except FileNotFoundError:
            return HTMLResponse(
                "<h1>admin.html not found</h1>"
                "<p>整理后台的前端随仓库一起下发，缺失通常意味着部署目录是旧版本。</p>",
                status_code=404,
            )
        return HTMLResponse(
            html,
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    @mcp.custom_route("/admin/stats", methods=["GET"])
    async def admin_stats(request: Request) -> Response:
        from starlette.responses import JSONResponse
        if not _is_admin_authorized(request):
            return _deny()
        try:
            buckets = await sh.bucket_mgr.list_all(include_archive=True)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

        by_type: dict[str, int] = {}
        by_month: dict[str, int] = {}
        tag_counts: dict[str, int] = {}
        resolved = pinned = dont_surface = archived = 0
        at_risk = 0
        try:
            threshold = float(getattr(sh.decay_engine, "threshold", 0.3))
        except Exception:
            threshold = 0.3

        for b in buckets:
            meta = b.get("metadata", {}) or {}
            if meta.get("deleted_at"):
                continue
            t = str(meta.get("type", "dynamic"))
            by_type[t] = by_type.get(t, 0) + 1
            if _is_archived(meta):
                archived += 1
            if meta.get("resolved"):
                resolved += 1
            if meta.get("pinned"):
                pinned += 1
            if meta.get("dont_surface"):
                dont_surface += 1
            month = str(meta.get("created", ""))[:7]
            if month:
                by_month[month] = by_month.get(month, 0) + 1
            for tag in (meta.get("tags") or []):
                tag_counts[str(tag)] = tag_counts.get(str(tag), 0) + 1
            # 「下一轮衰减会吃掉多少」——决定要不要现在动手整理的关键数字
            if not _is_archived(meta) and not meta.get("pinned"):
                try:
                    if sh.decay_engine.calculate_score(meta) < threshold:
                        at_risk += 1
                except Exception:
                    pass

        top_tags = sorted(tag_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:_TOP_TAGS]
        return JSONResponse({
            "total": sum(by_type.values()),
            "by_type": by_type,
            "archived": archived,
            "resolved": resolved,
            "unresolved": sum(by_type.values()) - resolved,
            "pinned": pinned,
            "dont_surface": dont_surface,
            "decay_threshold": threshold,
            "at_risk": at_risk,
            "by_month": dict(sorted(by_month.items())),
            "top_tags": [{"tag": t, "count": c} for t, c in top_tags],
        })

    @mcp.custom_route("/admin/buckets", methods=["GET"])
    async def admin_list_buckets(request: Request) -> Response:
        """分页 + 多维筛选 + 关键词。

        关键词走正文子串匹配而不是语义检索：整理的时候要的是「结果可复现、
        再点一次还是这批」，语义检索给不了这个保证。
        """
        from starlette.responses import JSONResponse
        if not _is_admin_authorized(request):
            return _deny()

        page = max(1, _qp_int(request, "page", 1) or 1)
        page_size = _qp_int(request, "page_size", _DEFAULT_PAGE_SIZE) or _DEFAULT_PAGE_SIZE
        page_size = max(1, min(_MAX_PAGE_SIZE, page_size))
        sort = _qp(request, "sort", "score")
        if sort not in _SORT_KEYS:
            sort = "score"
        reverse = _qp(request, "order", "desc").lower() != "asc"

        try:
            f = _read_filters(request)
        except (TypeError, ValueError) as e:
            return JSONResponse({"error": f"筛选参数不合法: {e}"}, status_code=400)

        try:
            buckets = await sh.bucket_mgr.list_all(include_archive=True)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

        matched = []
        for b in buckets:
            meta = b.get("metadata", {}) or {}
            if meta.get("deleted_at"):
                continue
            row = _row(b)
            if _matches(row, b.get("content", "") or "", f):
                matched.append(row)

        matched.sort(key=lambda r: (r.get(sort) is None, r.get(sort)), reverse=reverse)
        total = len(matched)
        start = (page - 1) * page_size
        return JSONResponse({
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": (total + page_size - 1) // page_size,
            "sort": sort,
            "order": "desc" if reverse else "asc",
            "items": matched[start:start + page_size],
        })

    @mcp.custom_route("/admin/buckets/{bucket_id}", methods=["PATCH"])
    async def admin_patch_bucket(request: Request) -> Response:
        """单条编辑。只传要改的字段，没传的一律不动。"""
        from starlette.responses import JSONResponse
        if not _is_admin_authorized(request):
            return _deny()

        bucket_id = request.path_params["bucket_id"]
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "body must be an object"}, status_code=400)

        bucket = await sh.bucket_mgr.get(bucket_id)
        if not bucket:
            return JSONResponse({"error": "bucket not found"}, status_code=404)

        updates: dict[str, Any] = {}
        if "tags" in body:
            if not isinstance(body["tags"], list):
                return JSONResponse({"error": "tags must be a list"}, status_code=400)
            updates["tags"] = [str(t).strip() for t in body["tags"] if str(t).strip()]
        for key in ("resolved", "pinned", "dont_surface", "digested"):
            if key in body:
                updates[key] = bool(body[key])
        if "importance" in body:
            try:
                updates["importance"] = max(1, min(10, int(body["importance"])))
            except (TypeError, ValueError):
                return JSONResponse({"error": "importance must be 1-10"}, status_code=400)
        if "why_remembered" in body:
            updates["why_remembered"] = str(body["why_remembered"])[:500]
        if "content" in body:
            text = str(body["content"])
            if not text.strip():
                return JSONResponse({"error": "content 不能改成空"}, status_code=400)
            updates["content"] = text

        if not updates:
            return JSONResponse({"error": "没有可更新的字段"}, status_code=400)

        try:
            ok = await sh.bucket_mgr.update(bucket_id, **updates)
        except Exception as e:
            # 改 content 会重建向量；embedding 不可用时 update 直接抛，
            # 这里如实回传原因，别让前端只看到一个 500。
            return JSONResponse(
                {"error": f"更新失败: {e}", "fields": sorted(updates)},
                status_code=502 if "content" in updates else 500,
            )
        if not ok:
            return JSONResponse({"error": "更新失败"}, status_code=500)

        return JSONResponse({"ok": True, "id": bucket_id, "updated": sorted(updates)})

    @mcp.custom_route("/admin/buckets/batch", methods=["POST"])
    async def admin_batch(request: Request) -> Response:
        """批量操作。每条独立成败，最后一起汇报，不因为一条失败就整单回滚——
        整理场景下「改了 47 条、3 条报错」比「一条没改」有用得多。

        唯一的例外是 delete：它先整体导出，导出失败就一条都不删。
        """
        from starlette.responses import JSONResponse
        if not _is_admin_authorized(request):
            return _deny()

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "body must be an object"}, status_code=400)

        action = str(body.get("action", "")).strip()
        ids = body.get("ids") or []

        # 任务书里点名不做的操作。给个明确的拒绝理由，比「未知 action」有用——
        # 否则下一个人只会以为是漏做了，然后把它补上。
        if action in ("move_to_diary", "migrate_to_diary"):
            return JSONResponse({
                "error": "不提供「批量迁往 diary」",
                "reason": "diary 只读最近 7 天。把旧桶迁进去，界面上像是保存了，"
                          "实际等于删除——这个操作会骗人，所以不做。"
                          "想让旧桶不再浮现，用 set_dont_surface 或 archive。",
            }, status_code=400)

        if action not in _BATCH_ACTIONS:
            return JSONResponse(
                {"error": f"未知 action: {action or '(空)'}", "allowed": list(_BATCH_ACTIONS)},
                status_code=400,
            )
        if not isinstance(ids, list) or not ids:
            return JSONResponse({"error": "ids 必须是非空列表"}, status_code=400)
        if len(ids) > _MAX_BATCH:
            return JSONResponse(
                {"error": f"一次最多 {_MAX_BATCH} 条，收到 {len(ids)} 条"},
                status_code=400,
            )

        # ---- delete：二次确认 + 先导出 ----
        export_path = ""
        if action == "delete":
            if not bool(body.get("confirm")):
                return JSONResponse(
                    {"error": "删除不可恢复，需要 confirm=true"},
                    status_code=400,
                )
            targets = []
            for bid in ids:
                b = await sh.bucket_mgr.get(bid)
                if b:
                    targets.append(b)
            if not targets:
                return JSONResponse({"error": "这些 id 一条都没找到"}, status_code=404)
            try:
                export_path = await _export_before_delete(targets)
            except Exception as e:
                # 导不出来就不删。宁可这次白点一下，也不要删完才发现没有备份。
                logger.error(f"[admin] 删除前导出失败，已放弃删除: {e}")
                return JSONResponse(
                    {"error": f"删除前导出失败，未删除任何内容: {e}"},
                    status_code=500,
                )

        add_tags = [str(t).strip() for t in (body.get("tags") or []) if str(t).strip()]

        done: list[str] = []
        missing: list[str] = []
        errors: list[dict] = []

        for bid in ids:
            try:
                bucket = await sh.bucket_mgr.get(bid)
                if not bucket:
                    missing.append(bid)
                    continue
                meta = bucket.get("metadata", {}) or {}

                if action == "add_tags":
                    await sh.bucket_mgr.update(bid, tags=_merge_tags(meta.get("tags"), add_tags, []))
                elif action == "remove_tags":
                    await sh.bucket_mgr.update(bid, tags=_merge_tags(meta.get("tags"), [], add_tags))
                elif action == "set_resolved":
                    await sh.bucket_mgr.update(bid, resolved=bool(body.get("resolved", True)))
                elif action == "set_dont_surface":
                    await sh.bucket_mgr.update(bid, dont_surface=bool(body.get("dont_surface", True)))
                elif action == "set_pinned":
                    await sh.bucket_mgr.update(bid, pinned=bool(body.get("pinned", True)))
                elif action == "set_importance":
                    imp = max(1, min(10, int(body.get("importance", 5))))
                    await sh.bucket_mgr.update(bid, importance=imp)
                elif action == "archive":
                    if not await sh.bucket_mgr.archive(bid):
                        errors.append({"id": bid, "error": "归档失败"})
                        continue
                elif action == "unarchive":
                    if not await sh.bucket_mgr.unarchive(bid):
                        errors.append({"id": bid, "error": "还原失败（可能本来就不在归档区）"})
                        continue
                elif action == "delete":
                    if not await sh.bucket_mgr.delete(bid):
                        errors.append({"id": bid, "error": "删除失败"})
                        continue
                done.append(bid)
            except Exception as e:
                errors.append({"id": bid, "error": str(e)})
                logger.warning(f"[admin] batch {action} failed for {bid}: {e}")

        result = {
            "ok": True,
            "action": action,
            "done": done,
            "missing": missing,
            "errors": errors,
            "counts": {"done": len(done), "missing": len(missing), "errors": len(errors)},
        }
        if export_path:
            result["export_path"] = export_path
        return JSONResponse(result)
