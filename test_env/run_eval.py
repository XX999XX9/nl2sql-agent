# ============================================================
# Spider 测试集批量评测脚本
# 用法：
#   python test_env/run_eval.py --run 1            # 第一轮：schema_retriever 关闭（默认配置）
#   python test_env/run_eval.py --run 2            # 第二轮：schema_retriever 开启，threshold=10
#   python test_env/run_eval.py --run all          # 顺序跑两轮并输出对比报告
#   python test_env/run_eval.py --run 1 --limit 5  # 每库只测前 N 题（冒烟验证）
#   python test_env/run_eval.py --run 1 --db car_1 # 只测指定库
# 输出：test_env/results/
#   detail_run{1,2}.jsonl   逐题明细（追加式，支持断点续跑）
#   gold_cache.json         标准答案执行结果缓存（两轮共享）
#   summary_run{1,2}.json   每轮统计汇总
#   report.md               最终对比报告
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

import argparse
import csv
import json
import os
import re
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

MAX_GOLD_ROWS = 5000      # gold/预测结果集比对上限（Spider 题目结果集普遍很小）
GOLD_TIMEOUT_MS = 30_000  # gold SQL 执行超时


# ----------------------------------------------------------- gold 执行与比对
def _norm_val(v):
    """值规范化：Decimal→float、datetime→ISO、float 控精度，保证可比较"""
    if v is None:
        return None
    if isinstance(v, Decimal):
        v = float(v)
    if isinstance(v, float):
        return round(v, 6)
    if isinstance(v, (datetime, date, dtime)):
        return v.isoformat()
    if isinstance(v, (int, str)):
        return v
    return str(v)


def _norm_rows(rows):
    """行集合规范化：每行值序保留，行间无序（multiset）"""
    return sorted(repr(tuple(_norm_val(c) for c in r)) for r in rows)


def run_gold_sql(sql: str, db: str, db_cfg: dict) -> dict:
    """直连执行标准 SQL（带超时与行数上限）；返回 {ok, rows, error}"""
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
            rows = cur.fetchmany(MAX_GOLD_ROWS)
        conn.close()
        return {"ok": True, "rows": [list(r) for r in rows], "error": None}
    except Exception as e:
        return {"ok": False, "rows": None, "error": f"{type(e).__name__}: {e}"[:200]}


def match_results(gold_rows, pred_rows) -> bool:
    """Spider 口径：无序 multiset 比较（列名无关、行序无关、行内值序保留）"""
    if len(gold_rows) != len(pred_rows):
        return False
    return _norm_rows(gold_rows) == _norm_rows(pred_rows)


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
    并把索引目录 patch 到 test_env/ 下按库隔离（项目 data/ 零污染）"""
    os.environ["DB_NAME"] = db
    from app.nl2sql.pipeline import NL2SQLPipeline

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
    gold_cache[qid] = res
    tmp = GOLD_CACHE_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(gold_cache, f, ensure_ascii=False, default=str)
    tmp.replace(GOLD_CACHE_FILE)
    return res


def run_round(run_id: int, db_filter=None, limit=None):
    """执行一轮评测（run_id=1 检索关闭；run_id=2 检索开启 threshold=10）"""
    from app.api.deps import get_conversation_manager, get_result_cache
    from app.utils.config_handler import load_yaml
    from app.utils.path_tool import BACKEND_ROOT

    db_cfg = load_yaml(BACKEND_ROOT / "config" / "database.yml")["database"]
    retriever_on = run_id == 2
    detail_file = RESULTS / f"detail_run{run_id}.jsonl"

    gold_cache = {}
    if GOLD_CACHE_FILE.exists():
        try:
            gold_cache = json.loads(GOLD_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            gold_cache = {}   # 半截/损坏缓存回退为空，重新执行 gold
    done_ids = set()
    if detail_file.exists():      # 断点续跑：跳过已完成题
        with open(detail_file, encoding="utf-8") as f:
            for line in f:
                try:
                    done_ids.add(json.loads(line)["id"])
                except Exception:
                    pass

    by_db = load_queries(db_filter, limit)
    total = sum(len(v) for v in by_db.values())
    pending = sum(1 for qs in by_db.values() for q in qs if q["id"] not in done_ids)
    print(f"\n{'=' * 60}\n第 {run_id} 轮（schema_retriever={'开启, threshold=10' if retriever_on else '关闭'}）"
          f"\n题目 {total} 条（已完成跳过 {total - pending}，待测 {pending}），库 {len(by_db)} 个\n{'=' * 60}")

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
            with open(detail_file, "a", encoding="utf-8") as f:
                for q in db_todo:
                    f.write(json.dumps({"id": q["id"], "database": db, "status": "pipeline_error",
                                        "error": str(e)[:200]}, ensure_ascii=False) + "\n")
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
                r = pipeline.run(q["question"], session_id=None)
                row.update({
                    "intent": r.intent, "pred_sql": r.sql, "executed": r.executed,
                    "pred_rows": r.rows, "error": r.error, "error_kind": r.error_kind,
                    "corrections": len(r.correction_trail), "usage": r.usage,
                })
            except Exception as e:
                row.update({"status": "crash", "error": f"{type(e).__name__}: {e}"[:200]})
                with open(detail_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
                continue

            # gold 执行与比对
            gold = load_gold(db_cfg, db, q["id"], q["sql"], gold_cache)
            row["gold_ok"] = gold["ok"]
            if not gold["ok"]:
                row["status"] = "gold_error"       # 标准答案在 MySQL 不可执行，剔除出分母
                row["gold_error"] = gold["error"]
            else:
                row["status"] = "pass" if (
                    r.executed and match_results(gold["rows"], r.rows)
                ) else "fail"
            with open(detail_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
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
    return summarize(run_id, elapsed)


# ----------------------------------------------------------- 统计汇总
def summarize(run_id: int, elapsed_s: float):
    detail_file = RESULTS / f"detail_run{run_id}.jsonl"
    rows = [json.loads(l) for l in open(detail_file, encoding="utf-8") if l.strip()]

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
        "schema_retriever": "on(threshold=10)" if run_id == 2 else "off",
        "total_questions": len(rows),
        "evaluated": n_eval,                     # gold 可执行的题数（准确率分母）
        "gold_unexecutable": len(gold_err),      # 标准 SQL 在 MySQL 9.0 执行失败，剔除
        "crashed": len(crashed),
        "execution_accuracy": round(n_pass / n_eval, 4) if n_eval else None,
        "syntax_pass_rate": round(n_exec_ok / n_eval, 4) if n_eval else None,
        "total_elapsed_s": round(elapsed_s, 1),
        "avg_latency_s": round(sum(latencies) / len(latencies) / 1000, 2) if latencies else None,
        "tokens": tokens,
        "intent_dist": dict(Counter(r.get("intent") for r in rows if r.get("intent"))),
        "error_kinds": dict(Counter(r["error_kind"] for r in rows if r.get("error_kind"))),
        "correction_triggered": len(corrected),
        "correction_rescued": sum(1 for r in corrected if r["status"] == "pass"),
        "accuracy_by_db": by_db_acc,
    }
    with open(RESULTS / f"summary_run{run_id}.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(json.dumps({k: v for k, v in summary.items() if k != "accuracy_by_db"},
                     ensure_ascii=False, indent=2))
    return summary


# ----------------------------------------------------------- 对比报告
def write_report():
    s = {}
    for rid in (1, 2):
        p = RESULTS / f"summary_run{rid}.json"
        if p.exists():
            s[rid] = json.loads(p.read_text(encoding="utf-8"))
    if not s:
        return
    lines = [
        "# Spider 测试集评测报告", "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 测试集：test_queries.csv（{max(x['total_questions'] for x in s.values())} 题 / 20 库）",
        f"- 数据库：Docker spiderman-mysql（MySQL 9.0.0，只读账号）", "",
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
    lines += ["", "## 各库准确率", "", "| 库 | 表数 | 检索路径 | 1轮准确率 | 2轮准确率 | Δ |", "|---|---|---|---|---|---|"]
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
    if 1 in s and 2 in s:
        d1 = {json.loads(l)["id"]: json.loads(l)
              for l in open(RESULTS / "detail_run1.jsonl", encoding="utf-8") if l.strip()}
        d2 = {json.loads(l)["id"]: json.loads(l)
              for l in open(RESULTS / "detail_run2.jsonl", encoding="utf-8") if l.strip()}
        common = set(d1) & set(d2)
        flip_pos = [i for i in common if d1[i].get("status") == "fail" and d2[i].get("status") == "pass"]
        flip_neg = [i for i in common if d1[i].get("status") == "pass" and d2[i].get("status") == "fail"]
        lines += ["", "## 两轮逐题对比", "",
                  f"- 第2轮答对、第1轮答错（检索带来收益）：{len(flip_pos)} 题",
                  f"- 第2轮答错、第1轮答对（检索引入退化）：{len(flip_neg)} 题"]
        for tag, ids in (("收益", flip_pos[:10]), ("退化", flip_neg[:10])):
            for i in ids:
                lines.append(f"  - [{tag}] {i} ({d1[i]['database']}): {d1[i]['question'][:60]}")
    (RESULTS / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"\n对比报告已生成: {RESULTS / 'report.md'}")


def main():
    ap = argparse.ArgumentParser(description="Spider 测试集批量评测")
    ap.add_argument("--run", choices=["1", "2", "all"], default="all")
    ap.add_argument("--limit", type=int, help="每库只测前 N 题（冒烟用）")
    ap.add_argument("--db", help="只测指定库")
    args = ap.parse_args()

    RESULTS.mkdir(exist_ok=True)
    if args.run in ("1", "all"):
        run_round(1, args.db, args.limit)
    if args.run in ("2", "all"):
        run_round(2, args.db, args.limit)
    write_report()


if __name__ == "__main__":
    main()
