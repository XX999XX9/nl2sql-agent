# 评测日志审查接口（只读）：浏览 test_env/results 下的历次评测结果，
# 供前端「评测日志」页面做错误类型勾选筛选、逐题明细查看。
# - 数据源固定为项目内 test_env/results（不存在时返回空列表，不报错）
# - 所有按目录取数的接口都做根目录约束，防止路径穿越
# - 明细做了瘦身（pred_rows 只回预览、usage 只回总量），避免整库结果撑爆响应

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.api.eval_access import issue_token, require_eval_access, verify_password
from app.schemas.common import ApiResponse
from app.utils.path_tool import BACKEND_ROOT

router = APIRouter()


class EvalAuthRequest(BaseModel):
    password: str

RESULTS_ROOT = (BACKEND_ROOT.parent / "test_env" / "results").resolve()
LATEST = RESULTS_ROOT / "latest"
FAIL_TEST = LATEST / "fail_test"
ALL_HISTORY = RESULTS_ROOT / "all_history"
HISTORY = RESULTS_ROOT / "history"

ROWS_PREVIEW = 20        # 单题结果集预览行数
ERROR_PREVIEW = 600      # 单题错误信息预览长度
GOLD_CACHE_FILE = RESULTS_ROOT / "gold_cache.json"  # gold SQL 执行结果缓存 {id: {ok, rows, error}}

# 错误分类标签：category = error_kind（有明确错误类型时）或 status/特殊桶
CATEGORY_LABELS = {
    "pass": "通过",
    "mismatch": "结果不符（SQL 执行成功，结果集与标准答案不一致）",
    "crash": "运行崩溃（流水线抛异常，题目未跑完）",
    "pipeline_error": "流水线构建失败（整库不可用）",
    "gold_error": "标准答案不可执行（不计入准确率分母）",
    "generate": "SQL 生成失败（模型未产出有效 SQL）",
    "correction": "纠错轮次耗尽仍失败",
    "guard": "安全守卫拦截",
    "validation_syntax": "静态校验：语法/非 SELECT/多语句",
    "validation_join": "静态校验：无 ON 笛卡尔积 JOIN",
    "validation_table": "静态校验：表不存在",
    "validation_column": "静态校验：字段不存在",
    "syntax": "数据库执行报错：SQL 语法/执行被拒",
    "permission": "数据库执行报错：账号无权限",
    "timeout": "数据库执行报错：执行超时",
    "other": "数据库执行报错：其他异常",
}
BADGE_STYLE = {
    "pass": "ok",
    "mismatch": "warn",
    "crash": "err", "pipeline_error": "err",
    "gold_error": "muted",
    "generate": "err", "correction": "err", "guard": "err",
    "validation_syntax": "warn", "validation_join": "warn",
    "validation_table": "warn", "validation_column": "warn",
    "syntax": "err", "permission": "err", "timeout": "err", "other": "err",
}


def _resolve_run_dir(rel_id: str) -> Path:
    """把相对 id 约束在 RESULTS_ROOT 内，拒绝穿越与绝对路径"""
    if not rel_id or rel_id.startswith("/") or ".." in Path(rel_id).parts:
        raise HTTPException(status_code=400, detail="非法的运行目录标识")
    target = (RESULTS_ROOT / rel_id).resolve()
    try:
        target.relative_to(RESULTS_ROOT)
    except ValueError:
        raise HTTPException(status_code=400, detail="运行目录越界")
    if not target.is_dir():
        raise HTTPException(status_code=404, detail=f"运行目录不存在：{rel_id}")
    return target


def _read_jsonl(path: Path) -> list:
    rows = []
    if not path.exists():
        return rows
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return rows


def _load_rows(run_dir: Path) -> list:
    """pass 明细在前、fail 明细在后（与评测脚本口径一致）"""
    return _read_jsonl(run_dir / "detail_pass.jsonl") + _read_jsonl(run_dir / "detail_fail.jsonl")


def _category_of(row: dict) -> str:
    status = row.get("status")
    if status == "pass":
        return "pass"
    if status in ("crash", "pipeline_error", "gold_error"):
        return status
    if status == "fail" and row.get("error_kind"):
        return str(row["error_kind"])
    return "mismatch"


def _safe_pass_count(s: dict):
    by_db = s.get("accuracy_by_db")
    if isinstance(by_db, dict) and by_db:
        return sum(v.get("pass", 0) for v in by_db.values())
    acc, n = s.get("execution_accuracy"), s.get("evaluated")
    if isinstance(acc, (int, float)) and isinstance(n, int):
        return round(acc * n)
    return None


def _safe_fail_count(s: dict):
    n, p = s.get("evaluated"), _safe_pass_count(s)
    if isinstance(n, int) and isinstance(p, int):
        return n - p
    return None


def _summarize_rows(rows: list) -> dict:
    evaluated = [r for r in rows if r.get("status") in ("pass", "fail")]
    n_pass = sum(1 for r in evaluated if r["status"] == "pass")
    usage_rows = [r for r in rows if r.get("usage")]
    return {
        "evaluated": len(evaluated),
        "execution_accuracy": round(n_pass / len(evaluated), 4) if evaluated else None,
        "crashed": sum(1 for r in rows if r.get("status") in ("crash", "pipeline_error")),
        "total_elapsed_s": round(sum((r.get("usage") or {}).get("elapsed_ms", 0)
                                     for r in usage_rows) / 1000, 1),
        "tokens": {
            "prompt": sum((r.get("usage") or {}).get("prompt_tokens", 0) for r in usage_rows),
            "completion": sum((r.get("usage") or {}).get("completion_tokens", 0) for r in usage_rows),
            "total": sum((r.get("usage") or {}).get("total_tokens", 0) for r in usage_rows),
        },
    }


def _run_meta(run_dir: Path, rel_id: str) -> dict:
    """从 summary.json 提取运行卡片所需字段；无 summary 时用明细现场汇总"""
    summary_path = run_dir / "summary.json"
    if summary_path.exists():
        try:
            s = json.loads(summary_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            s = {}
    else:
        s = _summarize_rows(_load_rows(run_dir))
    return {
        "id": rel_id,
        "name": run_dir.name,
        "started_at": s.get("started_at"),
        "mode": s.get("mode"),
        "schema_retriever": s.get("schema_retriever"),
        "total": s.get("evaluated", s.get("total_questions")),
        "pass": _safe_pass_count(s),
        "fail": _safe_fail_count(s),
        "accuracy": s.get("execution_accuracy"),
        "crashed": s.get("crashed"),
        "rescued": s.get("rescued"),
        "rescue_rate": s.get("rescue_rate"),
        "elapsed_s": s.get("total_elapsed_s"),
        "avg_latency_s": s.get("avg_latency_s"),
        "tokens": s.get("tokens"),
        "accuracy_by_db": s.get("accuracy_by_db"),
    }


def _list_group(base: Path) -> list:
    if not base.is_dir():
        return []
    items = []
    for d in sorted(base.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        if not ((d / "detail_pass.jsonl").exists() or (d / "detail_fail.jsonl").exists()):
            continue
        rel = d.relative_to(RESULTS_ROOT).as_posix()
        items.append(_run_meta(d, rel))
    return items


@router.post("/auth", response_model=ApiResponse[dict])
def eval_auth(body: EvalAuthRequest):
    """口令校验（服务端）：通过则签发短期令牌，后续请求放在 X-Eval-Token 头"""
    if not verify_password(body.password):
        raise HTTPException(status_code=401, detail="口令不正确")
    token, ttl = issue_token()
    return ApiResponse(data={"token": token, "expires_in": ttl})


@router.get("/runs", response_model=ApiResponse[dict])
def list_runs(_access: None = Depends(require_eval_access)):
    """运行列表：latest（最新全量）、fail_tests（失败题复测）、
    all_history（全量历史）、history（单库/冒烟等非全量历史）"""
    groups = {"latest": [], "fail_tests": [], "all_history": [], "history": []}
    if (LATEST / "summary.json").exists() or (LATEST / "detail_fail.jsonl").exists():
        groups["latest"] = [_run_meta(LATEST, "latest")]
    groups["fail_tests"] = _list_group(FAIL_TEST)
    groups["all_history"] = _list_group(ALL_HISTORY)
    groups["history"] = _list_group(HISTORY)
    return ApiResponse(data={"results_available": RESULTS_ROOT.is_dir(), **groups})


def _load_gold_cache() -> dict:
    """读取 gold 执行结果缓存（按 mtime 自动重载），供前端做 gold/pred 结果对比"""
    if not GOLD_CACHE_FILE.exists():
        return {}
    mtime = GOLD_CACHE_FILE.stat().st_mtime
    cached = getattr(_load_gold_cache, "_cache", None)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        data = json.loads(GOLD_CACHE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    _load_gold_cache._cache = (mtime, data)
    return data


def _attach_gold(item: dict, gold_cache: dict):
    """把 gold SQL 的执行结果预览附加到明细行（gold 与 pred 并排对比用）"""
    gold = gold_cache.get(item.get("id"))
    if not isinstance(gold, dict):
        return
    item["gold_ok"] = gold.get("ok")
    if gold.get("error"):
        item["gold_error"] = str(gold["error"])[:ERROR_PREVIEW]
    gold_rows = gold.get("rows")
    if isinstance(gold_rows, list):
        item["gold_row_count"] = len(gold_rows)
        item["gold_rows_preview"] = gold_rows[:ROWS_PREVIEW]
        item["gold_row_truncated"] = len(gold_rows) > ROWS_PREVIEW


def _slim_row(row: dict) -> dict:
    """列表瘦身：裁掉大结果集与分步明细，只留审查需要的字段"""
    pred_rows = row.get("pred_rows")
    out = {k: v for k, v in row.items() if k != "pred_rows"}
    if isinstance(pred_rows, list):
        out["row_count"] = len(pred_rows)
        out["row_preview"] = pred_rows[:ROWS_PREVIEW]
        out["row_truncated"] = len(pred_rows) > ROWS_PREVIEW
    if isinstance(out.get("error"), str):
        out["error"] = out["error"][:ERROR_PREVIEW]
    usage = out.get("usage")
    if isinstance(usage, dict):
        out["usage"] = {k: usage.get(k) for k in
                        ("elapsed_ms", "prompt_tokens", "completion_tokens", "total_tokens")}
    return out


@router.get("/run", response_model=ApiResponse[dict])
def get_run(
    id: str = Query(..., description="相对 results 的目录标识，如 latest / all_history/all-xxx"),
    categories: str = Query("", description="错误分类过滤，逗号分隔（与标签计数同源）"),
    dbs: str = Query("", description="库名过滤，逗号分隔"),
    q: str = Query("", description="题目/id 模糊搜索"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    _access: None = Depends(require_eval_access),
):
    """单次运行详情：汇总信息 + 分类/分库计数（未过滤口径）+ 过滤后的分页明细"""
    run_dir = _resolve_run_dir(id)
    rows = _load_rows(run_dir)
    gold_cache = _load_gold_cache()

    # 未过滤口径的分面计数（勾选框始终显示该运行的真实分布，不受其他筛选影响）
    cat_counter, db_counter = {}, {}
    for r in rows:
        cat = _category_of(r)
        cat_counter[cat] = cat_counter.get(cat, 0) + 1
        db = r.get("database") or "（未知）"
        db_counter[db] = db_counter.get(db, 0) + 1
    facets = {
        "categories": [
            {"key": k, "label": CATEGORY_LABELS.get(k, k),
             "style": BADGE_STYLE.get(k, "muted"), "count": v}
            for k, v in sorted(cat_counter.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        "dbs": [{"name": k, "count": v} for k, v in
                sorted(db_counter.items(), key=lambda kv: (-kv[1], kv[0]))],
    }

    cat_set = {c.strip() for c in categories.split(",") if c.strip()}
    db_set = {d.strip() for d in dbs.split(",") if d.strip()}
    keyword = q.strip().lower()
    filtered = []
    for r in rows:
        if cat_set and _category_of(r) not in cat_set:
            continue
        if db_set and (r.get("database") or "（未知）") not in db_set:
            continue
        if keyword and keyword not in (r.get("question") or "").lower() \
                and keyword not in (r.get("id") or "").lower():
            continue
        filtered.append(r)

    items = []
    for r in filtered[offset: offset + limit]:
        item = _slim_row(r)
        _attach_gold(item, gold_cache)
        cat = _category_of(r)
        item["category"] = cat
        item["category_label"] = CATEGORY_LABELS.get(cat, cat)
        item["badge_style"] = BADGE_STYLE.get(cat, "muted")
        items.append(item)

    return ApiResponse(data={
        "run": _run_meta(run_dir, id),
        "facets": facets,
        "total": len(filtered),
        "limit": limit,
        "offset": offset,
        "rows": items,
    })
