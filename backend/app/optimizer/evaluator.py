# 评估器：对优化前后的提示词构造测试用例做效果对比，输出评分与结论。
# 流程：LLM 生成 N 个测试输入 → 原版/优化版分别执行 → LLM 逐例判定胜负（位置交替防偏置）
# → 汇总评分与结论。结论本地生成，省一次调用。

import json
import re
from typing import List

from app.model.factory import LLMClient
from app.utils.logger_handler import setup_logger

logger = setup_logger()


class PromptEvaluator:
    """优化前后提示词 A/B 对比评估"""

    def __init__(self, llm: LLMClient, num_cases: int = 3):
        self.llm = llm
        self.num_cases = max(1, min(num_cases, 10))

    # ------------------------------------------------------------------ 入口
    def evaluate(self, original: str, optimized: str) -> dict:
        cases = self._gen_cases(original)
        results = []
        for idx, case in enumerate(cases):
            out_orig = self._run(original, case)
            out_opt = self._run(optimized, case)
            # 位置交替：偶数例 A=原版，奇数例 A=优化版，消除位置偏置
            if idx % 2 == 0:
                a, b, a_is_orig = out_orig, out_opt, True
            else:
                a, b, a_is_orig = out_opt, out_orig, False
            winner, reason = self._judge(original, case, a, b, a_is_orig)
            results.append({
                "input": case,
                "original_output": out_orig,
                "optimized_output": out_opt,
                "winner": winner,          # original / optimized / tie
                "reason": reason,
            })

        win_orig = sum(1 for r in results if r["winner"] == "original")
        win_opt = sum(1 for r in results if r["winner"] == "optimized")
        tie = len(results) - win_orig - win_opt
        return {
            "cases": results,
            "num_cases": len(results),
            "original_score": win_orig,
            "optimized_score": win_opt,
            "tie": tie,
            "conclusion": self._conclusion(win_orig, win_opt, tie),
        }

    # ------------------------------------------------------------------ 用例生成
    def _gen_cases(self, prompt: str) -> List[str]:
        meta = (
            f"以下是给 AI 模型使用的提示词。请针对该提示词要解决的任务，生成 {self.num_cases} 个"
            f"典型的测试输入（覆盖正常场景与边界场景，每个一行，格式为'1. 输入'）。"
            f"只输出编号列表，不要其他内容。\n\n【提示词】\n{prompt.strip()}"
        )
        result = self.llm.chat([{"role": "user", "content": meta}])
        cases = [
            re.sub(r"^\s*\d+[.、)]\s*", "", line).strip()
            for line in result.content.splitlines() if line.strip()
        ]
        # 过滤模型偶发的说明行（过短或过长的不像输入）
        cases = [c for c in cases if 2 <= len(c) <= 200][: self.num_cases]
        if not cases:
            cases = ["示例输入：请完成提示词描述的任务"]
        return cases

    # ------------------------------------------------------------------ 执行与判定
    def _run(self, prompt: str, case: str) -> str:
        result = self.llm.chat([
            {"role": "system", "content": prompt},
            {"role": "user", "content": case},
        ])
        return (result.content or "").strip()[:2000]

    def _judge(self, prompt: str, case: str, out_a: str, out_b: str, a_is_orig: bool) -> tuple:
        meta = (
            "你是提示词效果评委。同一个提示词的两个版本对同一输入产出了两份输出，"
            "请判断哪份输出更好地达成了提示词的任务目标。\n\n"
            f"【提示词】\n{prompt.strip()}\n\n【测试输入】\n{case}\n\n"
            f"【输出A】\n{out_a}\n\n【输出B】\n{out_b}\n\n"
            "判定标准：任务完成度、格式遵循、准确性、简洁性。"
            '只输出 JSON：{"winner": "A" 或 "B" 或 "tie", "reason": "一句话理由"}'
        )
        result = self.llm.chat([{"role": "user", "content": meta}])
        try:
            m = re.search(r"\{.*\}", result.content, re.DOTALL)
            verdict = json.loads(m.group(0))
            raw = verdict.get("winner", "tie")
            winner = {"A": "A", "B": "B"}.get(str(raw).strip().upper(), "tie")
            reason = str(verdict.get("reason", ""))[:300]
        except Exception:
            winner, reason = "tie", "评委输出解析失败，判定平局"
        # 归一化回 original/optimized
        if winner == "tie":
            return "tie", reason
        if (winner == "A") == a_is_orig:
            return "original", reason
        return "optimized", reason

    @staticmethod
    def _conclusion(win_orig: int, win_opt: int, tie: int) -> str:
        total = win_orig + win_opt + tie
        if win_opt > win_orig:
            return (
                f"优化版在 {total} 个用例中胜出 {win_opt} 次（原版 {win_orig} 次、平局 {tie} 次），"
                f"建议采纳优化结果。"
            )
        if win_orig > win_opt:
            return (
                f"原版在 {total} 个用例中胜出 {win_orig} 次（优化版 {win_opt} 次、平局 {tie} 次），"
                f"建议保留原版或更换策略重新优化。"
            )
        return f"双方各胜 {win_opt} 次（平局 {tie} 次），效果相当，可按 token 成本等其他因素决策。"
