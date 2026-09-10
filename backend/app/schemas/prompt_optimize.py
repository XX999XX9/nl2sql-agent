# 提示词优化模块的请求/响应模型

from typing import List, Optional

from pydantic import BaseModel, Field


class PromptOptimizeRequest(BaseModel):
    """POST /prompt_optimize 请求"""

    prompt: str = Field(..., min_length=1, max_length=20000, description="待优化提示词")
    strategy: str = Field("clarify", description="优化策略 key（/strategies 返回）")
    requirements: str = Field("", max_length=2000, description="额外要求（可选）")
    evaluate: bool = Field(False, description="是否执行优化前后对比评估（较慢）")


class EvalCase(BaseModel):
    """单个评估用例"""

    input: str
    original_output: str
    optimized_output: str
    winner: str                      # original / optimized / tie
    reason: str = ""


class EvaluationResult(BaseModel):
    """评估结果汇总"""

    num_cases: int
    original_score: int
    optimized_score: int
    tie: int
    conclusion: str
    cases: List[EvalCase] = []


class PromptOptimizeResponse(BaseModel):
    """优化（含可选评估）响应"""

    strategy: str
    strategy_name: str
    original: str
    optimized: str
    notes: str = ""
    usage: dict = {}
    evaluation: Optional[EvaluationResult] = None


class PromptApplyRequest(BaseModel):
    """POST /prompt_optimize/apply 请求"""

    filename: str = Field(..., description="目标提示词文件名（prompts/ 下）")
    content: str = Field(..., min_length=1)
