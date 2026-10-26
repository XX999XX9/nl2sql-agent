# ============================================================
# Spider 测试集批量评测脚本
# 用法：（默认断点续跑；中断后不加 --rerun 即可续跑）
#   python test_env/run_eval.py                  # 默认：全量 Schema 加载测试（检索关闭）
#   python test_env/run_eval.py --run 2          # schema_retriever 开启，threshold=10
#   python test_env/run_eval.py --run all        # 顺序跑两轮并输出对比报告
#   python test_env/run_eval.py --limit 5        # 每库只测前 N 题（冒烟验证）
#   python test_env/run_eval.py --db car_1       # 只测指定库
#   python test_env/run_eval.py --rerun          # 全量跑前清空 latest/ 旧明细后重跑
#                                                # 
#
# 输出目录（test_env/results/）：
#   latest/                            最近一次【全量·全量Schema】结果，每次全量跑覆盖
#     detail_pass.jsonl                逐题明细-通过
#     detail_fail.jsonl                逐题明细-未通过（fail/gold不可执行/crash 全在这）
#     summary.json                     统计汇总
#   all-YYYYmmdd-HHMMSS/               每次全量跑（run1）的历史归档，内容同 latest 三件套
#   all-retrieval-YYYYmmdd-HHMMSS/     run2（检索开启）全量跑的历史归档（不覆盖 latest）
#   <db名>-YYYYmmdd-HHMMSS/            单库测试（run1），每次生成全新目录，pass/fail 拆分
#   <db名>-retrieval-YYYYmmdd-HHMMSS/  单库测试（run2）
#   smoke-YYYYmmdd-HHMMSS/             --limit 冒烟跑（不限定单库时）
#   gold_cache.json                    标准答案执行结果缓存，各轮/各次共享
#   report.md                          run1 vs run2 对比报告（两轮都有结果时）
#
# 配套脚本：
#   python test_env/run_fail.py        # 只重跑 latest/detail_fail.jsonl，强制全量 Schema 加载
#
# 指标：
#   1. 执行准确率（Execution Accuracy，Spider 官方口径）：预测 SQL 与标准 SQL
#      的结果集（无序 multiset，值规范化后）一致的比例，核心指标
#   2. 语法通过率：最终交付 SQL 能正常执行不报错的比例（含自动纠错后的产物）
#   3. 消耗总时长（含每题平均）
#   4. token 消耗（prompt/completion/total）
#   附加：意图分布、错误类型分布、纠错触发率与挽回数、各库准确率、
#         gold 不可执行数、run1 vs run2 逐题翻转对比
# ============================================================

import os as _os
# 评测默认关闭 SQL 业务解读节点（省 token、与执行准确率无关）；
# 如需在评测中验证解读/裁判标签，显式 export NL2SQL_SQL_EXPLAINER=on 即可覆盖
_os.environ.setdefault("NL2SQL_SQL_EXPLAINER", "off")
_os.environ.setdefault("NL2SQL_EXPLAIN_GATE", "off")     # 评测库索引未经生产级维护：关闭 EXPLAIN 门禁，避免无意义的优化重写
_os.environ.setdefault("NL2SQL_CLARIFICATION", "off")  # 评测无人值守：关闭表筛选反问，避免空等
# 评测关闭结果截断：Guard 不追加强制 LIMIT、执行器不裁行，保证 pred 与 gold
# 在同一（完整）结果集上比对；超时 max_execution_time 仍是兜底。
# 如需恢复截断压测，显式 export NL2SQL_FORCED_LIMIT=1000 NL2SQL_RESULT_ROW_LIMIT=1000
_os.environ.setdefault("NL2SQL_FORCED_LIMIT", "0")
_os.environ.setdefault("NL2SQL_RESULT_ROW_LIMIT", "0")
# 评测是批量无人值守，不需要 Trace/会话投影：关闭检查点，避免每题的完整图状态写入 Redis
_os.environ.setdefault("NL2SQL_CHECKPOINTING", "off")
# 评测统一使用同一个用户：即使临时打开检查点，所有运行/Trace 也归到该用户下，
# 不会与界面登录用户混在一起。可用 NL2SQL_EVAL_USER 覆盖。
EVAL_USER = _os.environ.get("NL2SQL_EVAL_USER", "u_test")

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime, time as dtime
from decimal import Decimal
from pathlib import Path

TEST_ENV = Path(__file__).resolve().parent
BACKEND = TEST_ENV.parent / "backend"
RESULTS = TEST_ENV / "results"
GOLD_CACHE_FILE = RESULTS / "gold_cache.json"
LATEST_DIR = RESULTS / "latest"                 # 最新一次全量评测（断点续跑/失败题复测的数据源）
ALL_HISTORY_DIR = RESULTS / "all_history"      # 全量评测历史归档（含全量 run1 快照、全量 run2 检索轮）
HISTORY_DIR = RESULTS / "history"              # 非全量评测历史（单库、冒烟等）

# ----------------------------------------------------------- 环境准备
# 必须在 import app.* 之前：注入测试 DB_* 环境变量（优先级高于 backend/.env）
os.environ.setdefault("NL2SQL_LOG_LEVEL", "WARNING")


def setup_test_env():
    env = {}
    with open(TEST_ENV / ".env.test") as f:
        for line in f:
            m = re.match(r"(\w+)=(.+)", line.strip())
            if m:
                env[m.group(1)] = m.group(2).strip().strip('"')
    # 动态探测 WSL 网关（Windows 宿主）IP
    try:
        env["DB_HOST"] = subprocess.check_output(
            ["ip", "route", "show", "default"], text=True
        ).split()[2]
    except Exception:
        pass
    for k, v in env.items():
        os.environ[k] = v


setup_test_env()
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

# gold 结果集不设行数上限（fetchall）：pred 侧评测时也已关闭截断，两侧必须在
# 完整结果集上比对，否则大结果题会因两侧截断阈值不同（旧 5000 vs 1000）被误判。
# Spider 评测库单表规模有限，执行超时 GOLD_TIMEOUT_MS 仍是兜底。
GOLD_TIMEOUT_MS = 30_000  # gold SQL 执行超时


# ----------------------------------------------------------- 结果目录与明细读写
def now_ts() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def detail_paths(out_dir: Path):
    return out_dir / "detail_pass.jsonl", out_dir / "detail_fail.jsonl"


def load_detail(out_dir: Path):
    """读取一个结果目录下的 pass/fail 明细（pass 在前、fail 在后）"""
    rows = []
    for p in detail_paths(out_dir):
        if p.exists():
            for line in open(p, encoding="utf-8"):
                if line.strip():
                    rows.append(json.loads(line))
    return rows


def load_done_ids(out_dir: Path):
    return {r["id"] for r in load_detail(out_dir)}


def write_detail_row(out_dir: Path, row: dict):
    """按状态追加：pass → detail_pass.jsonl；其余（fail/gold_error/crash/...）→ detail_fail.jsonl"""
    pf = detail_paths(out_dir)[0] if row.get("status") == "pass" else detail_paths(out_dir)[1]
    with open(pf, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        f.flush()


def ensure_latest_from_legacy():
    """一次性迁移：旧版 detail_run1.jsonl + summary_run1.json → latest/（已存在 latest 则跳过）"""
    if LATEST_DIR.exists():
        return False
    legacy_detail = RESULTS / "detail_run1.jsonl"
    if not legacy_detail.exists():
        return False
    LATEST_DIR.mkdir(parents=True, exist_ok=True)
    n_pass = n_fail = 0
    with open(legacy_detail, encoding="utf-8") as src:
        for line in src:
            if not line.strip():
                continue
            row = json.loads(line)
            write_detail_row(LATEST_DIR, row)
            if row.get("status") == "pass":
                n_pass += 1
            else:
                n_fail += 1
    legacy_summary = RESULTS / "summary_run1.json"
    if legacy_summary.exists():
        shutil.copy2(legacy_summary, LATEST_DIR / "summary.json")
    print(f"已将旧版明细迁移到 {LATEST_DIR}/（pass {n_pass} / fail {n_fail}），"
          f"原始文件 detail_run1.jsonl 保留未删")
    return True


def plan_outputs(run_id: int, db_filter, limit):
    """返回 (输出目录, 历史归档目录或None)
    全量 run1：latest/（断点续跑/覆盖）+ all_history/all-<时间>/ 归档
    全量 run2：all_history/all-retrieval-<时间>/，不碰 latest
    单库/冒烟：history/ 下每次全新带时间戳目录"""
    ts = now_ts()
    retrieval = run_id == 2
    tag = "-retrieval" if retrieval else ""
    if db_filter:
        d = HISTORY_DIR / f"{db_filter}{tag}-{ts}"
        d.mkdir(parents=True, exist_ok=True)
        return d, None
    if limit:
        d = HISTORY_DIR / f"smoke{tag}-{ts}"
        d.mkdir(parents=True, exist_ok=True)
        return d, None
    if retrieval:
        d = ALL_HISTORY_DIR / f"all-retrieval-{ts}"
        d.mkdir(parents=True, exist_ok=True)
        return d, None
    # 全量 run1：清理上次崩溃留下的空 all-* 目录，再预建本次归档目录
    ALL_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    for d in ALL_HISTORY_DIR.glob("all-*"):
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()
    archive = ALL_HISTORY_DIR / f"all-{ts}"
    archive.mkdir(parents=True, exist_ok=True)
    LATEST_DIR.mkdir(parents=True, exist_ok=True)
    return LATEST_DIR, archive


# ----------------------------------------------------------- gold 执行与比对
def _norm_val(v):
    """值规范化：数字（Decimal/int/float 及数字形字符串）统一为 float 控精度，
    datetime/date 及两种字符串形态统一 ISO，保证跨来源（实时执行 vs JSON 缓存）可比较。
    gold 缓存经 json.dump(default=str) 后，Decimal 会变成 '407.0000'、
    datetime 变成 'YYYY-MM-DD HH:MM:SS'；pred 侧则是 float/ISO，必须在此归一"""
    if v is None:
        return None
    if isinstance(v, bool):          # bool 是 int 子类，须先排除
        return v
    if isinstance(v, Decimal):
        v = float(v)
    if isinstance(v, (int, float)):
        return round(float(v), 6)
    if isinstance(v, (datetime, date, dtime)):
        return v.isoformat()
    if isinstance(v, str):
        s = v.strip()
        if re.fullmatch(r"-?\d+(\.\d+)?", s):
            try:
                return round(float(s), 6)
            except ValueError:
                pass
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(s, fmt).isoformat()
            except ValueError:
                continue
        return v
    return str(v)


def _norm_rows(rows):
    """行集合规范化：每行值序保留，行间无序（multiset）"""
    return sorted(repr(tuple(_norm_val(c) for c in r)) for r in rows)


def _norm_rows_colagnostic(rows):
    """列序无关规范化：每行内部的值先排序（混合类型用 repr 作键），行间仍无序。
    仅用于严格比较失败后的兜底——SELECT a,b 与 SELECT b,a 只是列顺序不同，
    返回的信息等价，不应判错。"""
    return sorted(
        repr(tuple(sorted((_norm_val(c) for c in r), key=repr))) for r in rows
    )


def compare_results(gold_rows, pred_rows):
    """结果集比对，返回 (matched, col_reordered)。
    口径：行数必须相等；先做列序敏感的严格比较；失败后仅在【列数相同】时
    退化为列序无关比较（防止不同列数的结果因值巧合而误通过）。"""
    if len(gold_rows) != len(pred_rows):
        return False, False
    if _norm_rows(gold_rows) == _norm_rows(pred_rows):
        return True, False
    if (
        gold_rows
        and len(gold_rows[0]) == len(pred_rows[0])
        and _norm_rows_colagnostic(gold_rows) == _norm_rows_colagnostic(pred_rows)
    ):
        return True, True
    return False, False


def run_gold_sql(sql: str, db: str, db_cfg: dict) -> dict:
    """直连执行标准 SQL（带超时，不截断行数）；返回 {ok, rows, error}"""
    import pymysql

    try:
        conn = pymysql.connect(
            host=db_cfg["host"], port=int(db_cfg["port"]),
            user=db_cfg["user"], password=db_cfg["password"],
            database=db, connect_timeout=10,
        )
        with conn.cursor() as cur:
            cur.execute(f"SET SESSION max_execution_time = {GOLD_TIMEOUT_MS}")
            cur.execute(sql)
            rows = cur.fetchall()
        conn.close()
        return {"ok": True, "rows": [list(r) for r in rows], "error": None}
    except Exception as e:
        return {"ok": False, "rows": None, "error": f"{type(e).__name__}: {e}"[:200]}


def match_results(gold_rows, pred_rows) -> bool:
    """兼容包装：严格优先 + 列序宽容兜底，只返回是否匹配（run_fail 复用）"""
    return compare_results(gold_rows, pred_rows)[0]


# ----------------------------------------------------------- 评测主流程
def load_queries(db_filter=None, limit=None):
    rows = list(csv.DictReader(open(TEST_ENV.parent / "test_queries.csv", encoding="utf-8")))
    if db_filter:
        rows = [r for r in rows if r["database"] == db_filter]
    # 按库分组（同库相邻，最小化切库开销）
    by_db = defaultdict(list)
    for r in rows:
        by_db[r["database"]].append(r)
    if limit:
        by_db = {db: qs[:limit] for db, qs in by_db.items()}
    return by_db


def build_pipeline(db: str, conversation, cache, retriever_on: bool):
    """按库构建 pipeline：切 DB_NAME 环境变量后重建组件；
    retriever_on 时手动构造 SchemaRetriever（threshold=10）注入，
    并把索引目录 patch 到 test_env/ 下按库隔离（项目 data/ 零污染）。
    关系图谱快照 test_env/relation_index/<db>/relations.json 命中即用，零重建"""
    os.environ["DB_NAME"] = db
    from app.nl2sql.pipeline import NL2SQLPipeline

    # 关系图谱快照目录同样按库隔离：避免 20 库共用 relations.json 时指纹互相失效
    import app.nl2sql.relation_graph as rg_mod
    rel_dir = TEST_ENV / "relation_index" / db
    rel_dir.mkdir(parents=True, exist_ok=True)
    rg_mod.STORE_DIR = rel_dir

    p = NL2SQLPipeline(conversation_manager=conversation, result_cache=cache)
    if retriever_on:
        import app.nl2sql.schema_retriever as sr_mod
        from app.model.factory import ModelFactory
        from app.nl2sql.schema_retriever import SchemaRetriever

        idx_dir = TEST_ENV / "schema_index" / db
        idx_dir.mkdir(parents=True, exist_ok=True)
        # 每库独立索引目录：避免 20 库共用一个 collection 时表名互相覆盖/快照冲突
        sr_mod.STORE_DIR = idx_dir
        retriever = SchemaRetriever(p.schema_manager, ModelFactory().create_embedding())
        retriever.threshold = 10          # 本轮评测阈值
        p.schema_retriever = retriever
    return p


def load_gold(db_cfg, db, qid, sql, gold_cache):
    """gold 执行（带跨轮缓存；原子写防中断留下半截文件）"""
    if qid in gold_cache:
        return gold_cache[qid]
    res = run_gold_sql(sql, db, db_cfg)
    # 入库前规范化：Decimal/datetime 转为 JSON 原生可表达的 float/ISO 字符串，
    # 否则 default=str 会把 Decimal 存成 '407.0000'，读回后与 pred 侧 float 误判不等
    if res.get("ok"):
        res["rows"] = [[_norm_val(c) for c in row] for row in res["rows"]]
    gold_cache[qid] = res
    tmp = GOLD_CACHE_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(gold_cache, f, ensure_ascii=False, default=str)
    tmp.replace(GOLD_CACHE_FILE)
    return res


def run_round(run_id: int, db_filter=None, limit=None, rerun: bool = False):
    """执行一轮评测（run_id=1 检索关闭/全量Schema；run_id=2 检索开启 threshold=10）"""
    from app.api.deps import get_conversation_manager, get_result_cache
    from app.utils.config_handler import load_yaml
    from app.utils.path_tool import BACKEND_ROOT

    db_cfg = load_yaml(BACKEND_ROOT / "config" / "database.yml")["database"]
    retriever_on = run_id == 2
    out_dir, archive_dir = plan_outputs(run_id, db_filter, limit)
    out_dir.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 全量跑 + --rerun：清空 latest 明细后全新跑；单库/冒烟目录本身就是全新的
    if rerun and out_dir == LATEST_DIR:
        for p in detail_paths(out_dir):
            if p.exists():
                p.unlink()
        print(f"--rerun：已清空 {out_dir}/ 下旧明细")

    gold_cache = {}
    if GOLD_CACHE_FILE.exists():
        try:
            gold_cache = json.loads(GOLD_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            gold_cache = {}   # 半截/损坏缓存回退为空，重新执行 gold
    done_ids = load_done_ids(out_dir) if out_dir == LATEST_DIR else set()

    by_db = load_queries(db_filter, limit)
    total = sum(len(v) for v in by_db.values())
    pending = sum(1 for qs in by_db.values() for q in qs for q in qs if q["id"] not in done_ids)
    mode_desc = "开启, threshold=10" if retriever_on else "关闭（全量 Schema 加载）"
    print(f"\n{'=' * 60}\n第 {run_id} 轮（schema_retriever={mode_desc}）"
          f"\n题目 {total} 条（已完成跳过 {total - pending}，待测 {pending}），库 {len(by_db)} 个"
          f"\n输出目录：{out_dir}"
          + (f"\n完成后归档：{archive_dir}" if archive_dir else "")
          + f"\n{'=' * 60}")

    conversation = get_conversation_manager()
    cache = get_result_cache()
    t0 = time.time()

    for db_idx, (db, queries) in enumerate(sorted(by_db.items()), 1):
        db_todo = [q for q in queries if q["id"] not in done_ids]
        if not db_todo:
            print(f"[{db_idx}/{len(by_db)}] {db}: 全部已完成，跳过")
            continue
        print(f"[{db_idx}/{len(by_db)}] {db}: {len(db_todo)} 题待测 ...", flush=True)
        try:
            pipeline = build_pipeline(db, conversation, cache, retriever_on)
        except Exception as e:
            print(f"  ✗ pipeline 构建失败: {e}")
            for q in db_todo:
                write_detail_row(out_dir, {"id": q["id"], "database": db,
                                           "status": "pipeline_error", "error": str(e)[:200]})
            continue

        retrieval_mode = None
        if retriever_on and pipeline.schema_retriever is not None:
            tbl_n = len(pipeline.schema_manager.get_table_names())
            retrieval_mode = "topk" if tbl_n >= 10 else "full-schema"

        for q in db_todo:
            row = {
                "id": q["id"], "database": db, "question": q["question"],
                "gold_sql": q["sql"], "retrieval_mode": retrieval_mode,
            }
            try:
                r = pipeline.run(q["question"], session_id=None, user_id=EVAL_USER)
                row.update({
                    "intent": r.intent, "pred_sql": r.sql, "executed": r.executed,
                    "pred_rows": r.rows, "error": r.error, "error_kind": r.error_kind,
                    "corrections": len(r.correction_trail),
                    "opt_rounds": len(r.optimization_trail), "usage": r.usage,
                    "capability_trail": list(r.capability_trail),
                    "skills_used": sorted({g.split(":", 1)[1]
                                           for t in r.capability_trail for g in t["granted"]
                                           if g.startswith("skill:")}),
                    "tools_used": sorted({g.split(":", 1)[1]
                                          for t in r.capability_trail for g in t["granted"]
                                          if g.startswith("tool:")}),
                    "cap_denied": sum(len(t["denied"]) for t in r.capability_trail),
                })
            except Exception as e:
                row.update({"status": "crash", "error": f"{type(e).__name__}: {e}"[:200]})
                write_detail_row(out_dir, row)
                continue

            # gold 执行与比对
            gold = load_gold(db_cfg, db, q["id"], q["sql"], gold_cache)
            row["gold_ok"] = gold["ok"]
            if not gold["ok"]:
                row["status"] = "gold_error"       # 标准答案在 MySQL 不可执行，剔除出分母
                row["gold_error"] = gold["error"]
            else:
                matched, col_reordered = (
                    compare_results(gold["rows"], r.rows) if r.executed else (False, False)
                )
                row["status"] = "pass" if matched else "fail"
                row["col_reordered"] = col_reordered   # 仅列顺序不同而宽容通过的标记
            write_detail_row(out_dir, row)
            mark = {"pass": "✓", "fail": "✗", "gold_error": "⊘"}.get(row["status"], "?")
            print(f"  {mark} {q['id'][:8]} {row['status']}"
                  + (f" ({r.error_kind})" if r.error_kind else ""), flush=True)

        # 释放该库连接池，防 20 库连接堆积
        try:
            pipeline.executor.dispose()
        except Exception:
            pass

    elapsed = time.time() - t0
    print(f"\n第 {run_id} 轮执行完毕，耗时 {elapsed / 60:.1f} 分钟，汇总中 ...")
    summary = summarize(out_dir, elapsed, run_id=run_id, started_at=started_at)
    print_failures(out_dir, run_id=run_id)

    # 全量 run1：把本次 latest 完整结果快照到历史归档目录
    if archive_dir is not None:
        for name in ("detail_pass.jsonl", "detail_fail.jsonl", "summary.json"):
            src = out_dir / name
            if src.exists():
                shutil.copy2(src, archive_dir / name)
        print(f"历史归档已生成：{archive_dir}")
    return out_dir, summary


# ----------------------------------------------------------- 统计汇总
def summarize(out_dir: Path, elapsed_s: float, run_id: int = 1, started_at: str = "",
              scope_ids=None, extra: dict = None):
    rows = load_detail(out_dir)
    if scope_ids is not None:   # 范围跑时只统计该范围（目录本身通常已隔离，保留兼容）
        rows = [r for r in rows if r.get("id") in scope_ids]

    evaluated = [r for r in rows if r.get("status") in ("pass", "fail")]  # gold 可执行
    gold_err = [r for r in rows if r.get("status") == "gold_error"]
    crashed = [r for r in rows if r.get("status") in ("crash", "pipeline_error")]

    n_eval = len(evaluated)
    n_pass = sum(1 for r in evaluated if r["status"] == "pass")
    n_exec_ok = sum(1 for r in evaluated if r.get("executed"))

    usage_rows = [r for r in rows if r.get("usage")]
    tokens = {
        "prompt": sum(u["usage"].get("prompt_tokens", 0) for u in usage_rows),
        "completion": sum(u["usage"].get("completion_tokens", 0) for u in usage_rows),
        "total": sum(u["usage"].get("total_tokens", 0) for u in usage_rows),
    }
    latencies = [u["usage"]["elapsed_ms"] for u in usage_rows if u["usage"].get("elapsed_ms")]
    if elapsed_s < 1 and latencies:
        # 断点续跑全部跳过（无新题）时，回退用明细中各题耗时求和
        elapsed_s = sum(latencies) / 1000

    by_db_acc = {}
    for db in sorted({r["database"] for r in evaluated}):
        sub = [r for r in evaluated if r["database"] == db]
        by_db_acc[db] = {
            "n": len(sub), "pass": sum(1 for r in sub if r["status"] == "pass"),
            "acc": round(sum(1 for r in sub if r["status"] == "pass") / len(sub), 4),
            "tables_retrieval": next((r.get("retrieval_mode") for r in sub), None),
        }

    corrected = [r for r in evaluated if r.get("corrections", 0) > 0]
    summary = {
        "run": run_id,
        "mode": "retrieval-topk10" if run_id == 2 else "full-schema",
        "schema_retriever": "on(threshold=10)" if run_id == 2 else "off",
        "started_at": started_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_questions": len(rows),
        "evaluated": n_eval,                     # gold 可执行的题数（准确率分母）
        "gold_unexecutable": len(gold_err),      # 标准 SQL 在 MySQL 9.0 执行失败，剔除
        "crashed": len(crashed),
        "execution_accuracy": round(n_pass / n_eval, 4) if n_eval else None,
        "col_reordered_pass": sum(1 for r in evaluated if r.get("col_reordered")),  # 仅列序不同的宽容通过数
        "syntax_pass_rate": round(n_exec_ok / n_eval, 4) if n_eval else None,
        "total_elapsed_s": round(elapsed_s, 1),
        "avg_latency_s": round(sum(latencies) / len(latencies) / 1000, 2) if latencies else None,
        "tokens": tokens,
        "intent_dist": dict(Counter(r.get("intent") for r in rows if r.get("intent"))),
        "error_kinds": dict(Counter(r["error_kind"] for r in rows if r.get("error_kind"))),
        "correction_triggered": len(corrected),
        "correction_rescued": sum(1 for r in corrected if r["status"] == "pass"),
        "perf_optimized_questions": sum(1 for r in evaluated if r.get("opt_rounds", 0) > 0),
        "perf_optimization_rounds": sum(r.get("opt_rounds", 0) for r in evaluated),
        "skill_used_questions": sum(1 for r in evaluated if r.get("skills_used")),
        "tool_used_questions": sum(1 for r in evaluated if r.get("tools_used")),
        "capability_granted_total": sum(
            len(t.get("granted", [])) for r in evaluated for t in r.get("capability_trail", [])),
        "capability_denied_total": sum(r.get("cap_denied", 0) for r in evaluated),
        "accuracy_by_db": by_db_acc,
    }
    if extra:
        summary.update(extra)
    with open(out_dir / "summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(json.dumps({k: v for k, v in summary.items() if k != "accuracy_by_db"},
                     ensure_ascii=False, indent=2))
    return summary


# ----------------------------------------------------------- 错误明细输出
def _failure_reason(r: dict, gold_rows) -> str:
    """单题失败原因归类与描述"""
    if r.get("status") == "crash" or r.get("status") == "pipeline_error":
        return f"流水线异常: {r.get('error', '未知')}"
    if r.get("status") == "gold_error":
        return f"标准答案 SQL 在 MySQL 不可执行（不计入准确率）: {r.get('gold_error', '')}"
    if r.get("error_kind"):
        return f"{r['error_kind']} | {r.get('error', '')}"
    if not r.get("pred_sql"):
        return f"意图误判为 {r.get('intent', '?')}，未生成查询 SQL"
    if not r.get("executed"):
        return "SQL 未被执行"
    # 执行成功但结果不一致：给出行数对比帮助定位
    n_pred = len(r.get("pred_rows") or [])
    n_gold = len(gold_rows) if gold_rows is not None else "?"
    return f"结果集与标准答案不一致（预测 {n_pred} 行 vs 标准 {n_gold} 行）"


def print_failures(out_dir: Path, run_id: int = 1, scope_ids=None, title: str = None):
    """运行结束后终端打印全部错误题目与原因，按错误类型分组"""
    rows = load_detail(out_dir)
    if not rows:
        return
    if scope_ids is not None:
        rows = [r for r in rows if r.get("id") in scope_ids]
    failures = [r for r in rows if r.get("status") != "pass"]
    head = title or f"第 {run_id} 轮"
    if not failures:
        print(f"\n{'=' * 60}\n{head}全部题目通过，无错误明细\n{'=' * 60}")
        return

    gold_cache = {}
    if GOLD_CACHE_FILE.exists():
        try:
            gold_cache = json.loads(GOLD_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass

    def _sql(s, n=110):
        s = (s or "").replace("\n", " ").strip()
        return s[:n] + ("…" if len(s) > n else "")

    def _rows_preview(rows_, n=2):
        if not rows_:
            return "[]"
        return "[" + "; ".join(str(r)[:40] for r in rows_[:n]) + (", …" if len(rows_) > n else "") + "]"

    # 按错误类型分组
    groups = {"生成失败/被拦截": [], "执行报错": [], "结果不一致": [], "标准答案不可执行": [], "其他": []}
    for r in failures:
        reason = _failure_reason(r, (gold_cache.get(r["id"]) or {}).get("rows"))
        kind = r.get("error_kind") or ""
        if kind in ("generate",) or kind.startswith("validation_") or kind == "guard":
            key = "生成失败/被拦截"
        elif kind in ("syntax", "timeout", "permission"):
            key = "执行报错"
        elif r.get("status") == "gold_error":
            key = "标准答案不可执行"
        elif "结果集与标准答案不一致" in reason:
            key = "结果不一致"
        else:
            key = "其他"
        groups[key].append((r, reason))

    print(f"\n{'=' * 60}\n{head}错误题目明细（共 {len(failures)} 题）\n{'=' * 60}")
    for key, items in groups.items():
        if not items:
            continue
        print(f"\n【{key}】{len(items)} 题")
        print("-" * 60)
        for r, reason in items:
            print(f"✗ [{r['database']}] {r['id']}")
            print(f"  问题: {r['question'][:100]}")
            print(f"  原因: {reason[:160]}")
            pred_sql = _sql(r.get("pred_sql"))
            gold_sql = _sql(r.get("gold_sql"))
            if pred_sql:
                print(f"  预测: {pred_sql}")
                if r.get("pred_rows") is not None and r.get("executed"):
                    print(f"        预测结果: {_rows_preview(r['pred_rows'])}")
            else:
                print("  预测: （无 SQL）")
            print(f"  标准: {gold_sql}")
            gold_rows = (gold_cache.get(r["id"]) or {}).get("rows")
            if gold_rows is not None:
                print(f"        标准结果: {_rows_preview(gold_rows)}")
    print(f"\n{'=' * 60}\n错误明细输出完毕（完整数据见 {out_dir}/detail_fail.jsonl）\n{'=' * 60}")


# ----------------------------------------------------------- 对比报告
def _newest_archive(retrieval: bool):
    """最新历史归档目录：retrieval=False 取 all-*（排除 all-retrieval-*），True 取 all-retrieval-*"""
    prefix = "all-retrieval-" if retrieval else "all-"
    ds = [d for d in ALL_HISTORY_DIR.glob(f"{prefix}*")
          if d.is_dir() and (d / "summary.json").exists()]
    if not retrieval:
        ds = [d for d in ds if not d.name.startswith("all-retrieval-")]
    return max(ds, key=lambda d: d.name) if ds else None


def write_report():
    d1 = LATEST_DIR if (LATEST_DIR / "summary.json").exists() else _newest_archive(False)
    d2 = _newest_archive(True)
    if not d1 or not d2:
        if d1 or d2:
            print("\n仅一轮结果，跳过对比报告（两轮都有结果时才会重写 report.md）")
        return
    s = {1: json.loads((d1 / "summary.json").read_text(encoding="utf-8")),
         2: json.loads((d2 / "summary.json").read_text(encoding="utf-8"))}
    lines = [
        "# Spider 测试集评测报告", "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 测试集：test_queries.csv（{max(x['total_questions'] for x in s.values())} 题 / 20 库）",
        f"- 数据库：Docker spiderman-mysql（MySQL 9.0.0，只读账号）",
        f"- 全量Schema轮目录：{d1.name}；检索轮目录：{d2.name}", "",
        "| 指标 | 第1轮 检索关闭 | 第2轮 检索开启(threshold=10) |", "|---|---|---|",
    ]
    def row(name, key, fmt=None):
        vals = []
        for rid in (1, 2):
            v = s.get(rid, {}).get(key)
            vals.append("—" if v is None else (fmt(v) if fmt else str(v)))
        lines.append(f"| {name} | {vals[0]} | {vals[1]} |")

    row("可评估题数（gold 可执行）", "evaluated")
    row("**执行准确率**", "execution_accuracy", lambda v: f"**{v:.2%}**")
    row("语法通过率", "syntax_pass_rate", lambda v: f"{v:.2%}")
    row("总耗时", "total_elapsed_s", lambda v: f"{v / 60:.1f} 分钟")
    row("平均每题", "avg_latency_s", lambda v: f"{v:.2f} s")
    row("token 消耗(prompt/completion/total)", "tokens",
        lambda v: f"{v['prompt']} / {v['completion']} / {v['total']}")
    row("纠错触发 / 挽回", "correction_rescued",
        lambda v: f"{s[list(s)[0]]['correction_triggered']} 次 / {v} 题"
        if False else v)
    lines += ["", "## 各库准确率", "", "| 库 | 检索路径 | 1轮准确率 | 2轮准确率 | Δ |",
              "|---|---|---|---|---|"]
    dbs = sorted(set().union(*[set(x.get("accuracy_by_db", {}).keys()) for x in s.values()]))
    for db in dbs:
        a1 = s.get(1, {}).get("accuracy_by_db", {}).get(db)
        a2 = s.get(2, {}).get("accuracy_by_db", {}).get(db)
        acc1 = f"{a1['acc']:.2%} ({a1['pass']}/{a1['n']})" if a1 else "—"
        acc2 = f"{a2['acc']:.2%} ({a2['pass']}/{a2['n']})" if a2 else "—"
        delta = "—" if not (a1 and a2) else f"{(a2['acc'] - a1['acc']) * 100:+.1f}pp"
        mode = (a2 or {}).get("tables_retrieval") or "—"
        lines.append(f"| {db} | {mode} | {acc1} | {acc2} | {delta} |")

    # 逐题翻转对比
    dd = {}
    for rid, d in ((1, d1), (2, d2)):
        dd[rid] = {r["id"]: r for r in load_detail(d)}
    common = set(dd[1]) & set(dd[2])
    flip_pos = [i for i in common if dd[1][i].get("status") == "fail" and dd[2][i].get("status") == "pass"]
    flip_neg = [i for i in common if dd[1][i].get("status") == "pass" and dd[2][i].get("status") == "fail"]
    lines += ["", "## 两轮逐题对比", "",
              f"- 第2轮答对、第1轮答错（检索带来收益）：{len(flip_pos)} 题",
              f"- 第2轮答错、第1轮答对（检索引入退化）：{len(flip_neg)} 题"]
    for tag, ids in (("收益", flip_pos[:10]), ("退化", flip_neg[:10])):
        for i in ids:
            lines.append(f"  - [{tag}] {i} ({dd[1][i]['database']}): {dd[1][i]['question'][:60]}")
    (RESULTS / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n对比报告已生成: {RESULTS / 'report.md'}")


def main():
    ap = argparse.ArgumentParser(description="Spider 测试集批量评测（默认只跑第 1 轮全量 Schema 加载）")
    ap.add_argument("--run", choices=["1", "2", "all"], default="1",
                    help="1=全量Schema加载(默认)；2=检索开启threshold=10；all=两轮+对比报告")
    ap.add_argument("--limit", type=int, help="每库只测前 N 题（冒烟用）")
    ap.add_argument("--db", help="只测指定库")
    ap.add_argument("--rerun", action="store_true",
                    help="全量跑时清空 latest/ 旧明细后重跑（默认断点续跑；中断后直接重跑命令即可续跑）")
    args = ap.parse_args()

    RESULTS.mkdir(exist_ok=True)
    ensure_latest_from_legacy()
    if args.run in ("1", "all"):
        run_round(1, args.db, args.limit, rerun=args.rerun)
    if args.run in ("2", "all"):
        run_round(2, args.db, args.limit, rerun=args.rerun)
    write_report()


if __name__ == "__main__":
    main()
