# ============================================================
# 最新版失败题专项复测脚本
# 用途：只重跑 results/latest/detail_fail.jsonl 中的题目，验证修复后能否挽回
#
# 强约束：本脚本【只允许全量 Schema 加载模式】（schema_retriever 强制关闭，
#         不构造任何检索器，不涉及大库 top-k 检索），与 run_eval.py --run 1 同模式
#
# 用法：
#   python test_env/run_fail.py                # 重跑 latest/detail_fail.jsonl 全部失败题
#   python test_env/run_fail.py --db world_1   # 只复测其中某个库
#   python test_env/run_fail.py --limit 5      # 每个库只取前 5 题（冒烟）
#   python test_env/run_fail.py --include-gold # 连同 gold_error（标准答案不可执行）也重跑
#
# 输出：test_env/results/latest/fail_test/fail-YYYYmmdd-HHMMSS/
#   detail_pass.jsonl   本次复测通过的题（即被挽回的失败题）
#   detail_fail.jsonl   本次复测仍未通过的题
#   summary.json        统计汇总（含挽回率）
# ============================================================

import os as _os
# 评测默认关闭 SQL 业务解读节点（省 token、与执行准确率无关）；
# 如需在评测中验证解读/裁判标签，显式 export NL2SQL_SQL_EXPLAINER=on 即可覆盖
_os.environ.setdefault("NL2SQL_SQL_EXPLAINER", "off")
_os.environ.setdefault("NL2SQL_EXPLAIN_GATE", "off")     # 评测库索引未经生产级维护：关闭 EXPLAIN 门禁，避免无意义的优化重写
_os.environ.setdefault("NL2SQL_CLARIFICATION", "off")  # 评测无人值守：关闭表筛选反问，避免空等
_os.environ.setdefault("NL2SQL_FORCED_LIMIT", "0")        # 评测关闭强制 LIMIT（详见 run_eval.py 同名设置）
_os.environ.setdefault("NL2SQL_RESULT_ROW_LIMIT", "0")    # 评测关闭结果行数截断
_os.environ.setdefault("NL2SQL_CHECKPOINTING", "off")     # 批量复测不写检查点（无需 Trace）

import argparse
import json
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import run_eval as ev


def main():
    ap = argparse.ArgumentParser(
        description="latest/detail_fail.jsonl 专项复测（强制全量 Schema 加载，不使用检索器）")
    ap.add_argument("--db", help="只复测指定库的失败题")
    ap.add_argument("--limit", type=int, help="每个库只取前 N 题（冒烟用）")
    ap.add_argument("--include-gold", action="store_true",
                    help="连 gold_error（标准答案本身不可执行）也一起重跑（默认跳过）")
    args = ap.parse_args()

    ev.RESULTS.mkdir(exist_ok=True)
    ev.ensure_latest_from_legacy()

    src_file = ev.LATEST_DIR / "detail_fail.jsonl"
    if not src_file.exists():
        raise SystemExit(f"找不到 {src_file}，请先执行一次全量评测（python test_env/run_eval.py）")
    src_rows = [json.loads(l) for l in open(src_file, encoding="utf-8") if l.strip()]
    skipped_gold = sum(1 for r in src_rows if r.get("status") == "gold_error")
    if not args.include_gold:
        src_rows = [r for r in src_rows if r.get("status") != "gold_error"]
    if args.db:
        src_rows = [r for r in src_rows if r.get("database") == args.db]

    # 按库分组并应用 --limit
    by_db = defaultdict(list)
    for r in src_rows:
        by_db[r["database"]].append(r)
    if args.limit:
        by_db = {db: rs[: args.limit] for db, rs in by_db.items()}
    targets = [r for db in sorted(by_db) for r in by_db[db]]
    if not targets:
        raise SystemExit("detail_fail 中没有符合条件的题目（可能已全部通过）")

    # 错误集复测统一收在 latest/fail_test/ 下（数据源是 latest/detail_fail.jsonl）
    out_dir = ev.LATEST_DIR / "fail_test" / f"fail-{ev.now_ts()}"
    out_dir.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    from app.api.deps import get_conversation_manager, get_result_cache
    from app.utils.config_handler import load_yaml
    from app.utils.path_tool import BACKEND_ROOT

    db_cfg = load_yaml(BACKEND_ROOT / "config" / "database.yml")["database"]
    gold_cache = {}
    if ev.GOLD_CACHE_FILE.exists():
        try:
            gold_cache = json.loads(ev.GOLD_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            gold_cache = {}

    prev_status = dict(Counter(r.get("status") for r in targets))
    print(f"\n{'=' * 60}\n失败题专项复测（schema_retriever 强制关闭·全量 Schema 加载）"
          f"\n来源：{src_file.relative_to(ev.TEST_ENV)}"
          f"\n题目 {len(targets)} 条，库 {len(by_db)} 个；上次状态分布 {prev_status}"
          + (f"；已跳过 gold_error {skipped_gold} 条（--include-gold 可纳入）"
             if skipped_gold and not args.include_gold else "")
          + f"\n输出目录：{out_dir}\n{'=' * 60}")

    conversation = get_conversation_manager()
    cache = get_result_cache()
    t0 = time.time()

    for db_idx, (db, src_qs) in enumerate(sorted(by_db.items()), 1):
        print(f"[{db_idx}/{len(by_db)}] {db}: {len(src_qs)} 题复测 ...", flush=True)
        # 关键约束：retriever_on 固定 False —— 全量加载表结构，不启用大库检索
        pipeline = ev.build_pipeline(db, conversation, cache, retriever_on=False)
        # 显式置空，即使 agent.yml 里 features.schema_retriever 被打开也强制全量加载
        pipeline.schema_retriever = None
        assert pipeline.schema_retriever is None, "失败题复测不允许启用 schema_retriever"

        for src in src_qs:
            q = {"id": src["id"], "database": db,
                 "question": src["question"], "sql": src.get("gold_sql", "")}
            row = {
                "id": q["id"], "database": db, "question": q["question"],
                "gold_sql": q["sql"], "retrieval_mode": None,
                "previous_status": src.get("status"),
            }
            try:
                r = pipeline.run(q["question"], session_id=None, user_id=ev.EVAL_USER)
                row.update({
                    "intent": r.intent, "pred_sql": r.sql, "executed": r.executed,
                    "pred_rows": r.rows, "error": r.error, "error_kind": r.error_kind,
                    "corrections": len(r.correction_trail), "usage": r.usage,
                })
            except Exception as e:
                row.update({"status": "crash", "error": f"{type(e).__name__}: {e}"[:200]})
                ev.write_detail_row(out_dir, row)
                print(f"  ? {q['id'][:8]} crash", flush=True)
                continue

            gold = ev.load_gold(db_cfg, db, q["id"], q["sql"], gold_cache)
            row["gold_ok"] = gold["ok"]
            if not gold["ok"]:
                row["status"] = "gold_error"
                row["gold_error"] = gold["error"]
            else:
                matched, col_reordered = (
                    ev.compare_results(gold["rows"], r.rows) if r.executed else (False, False)
                )
                row["status"] = "pass" if matched else "fail"
                row["col_reordered"] = col_reordered
            ev.write_detail_row(out_dir, row)
            mark = {"pass": "✓ 挽回", "fail": "✗ 仍失败", "gold_error": "⊘"}.get(row["status"], "?")
            print(f"  {mark} {q['id'][:8]} {row['status']}"
                  + (f" ({r.error_kind})" if r.error_kind else ""), flush=True)

        try:
            pipeline.executor.dispose()
        except Exception:
            pass

    elapsed = time.time() - t0
    rows = ev.load_detail(out_dir)
    evaluated = [r for r in rows if r.get("status") in ("pass", "fail")]
    rescued = sum(1 for r in evaluated if r["status"] == "pass")
    still_fail = len(evaluated) - rescued
    print(f"\n复测执行完毕，耗时 {elapsed / 60:.1f} 分钟，汇总中 ...")
    ev.summarize(
        out_dir, elapsed, run_id=1, started_at=started_at,
        extra={
            "mode": "full-schema",
            "rerun_source": str(src_file.relative_to(ev.TEST_ENV)),
            "source_total": len(targets),
            "rescued": rescued,
            "still_fail": still_fail,
            "rescue_rate": round(rescued / len(evaluated), 4) if evaluated else None,
        },
    )
    ev.print_failures(out_dir, title="失败题复测")
    print(f"\n{'=' * 60}\n挽回 {rescued} / {len(evaluated)} 题"
          f"（{rescued / len(evaluated) * 100:.1f}%），仍失败 {still_fail} 题"
          f"\n结果目录：{out_dir}\n{'=' * 60}")


if __name__ == "__main__":
    main()
