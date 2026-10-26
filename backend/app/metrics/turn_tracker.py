# 单轮追踪器：记录从收到问题到最终回答的全链路——总耗时、每步
# （意图/上下文/生成/校验/守卫/执行/解释）耗时、token 分步与累计；
# finish() 后生成 TurnUsage 摘要，供 usage 事件推送。
#
# 声明为 dataclass：LangGraph 检查点用 msgpack 序列化状态，普通对象不可编码；
# dataclass 的字段可以被序列化器完整还原。

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class TurnTracker:
    """单轮全链路追踪器：pipeline 每步用 with tracker.step("名称") 埋点"""

    enabled: bool = True
    start: float = 0.0
    steps: List[dict] = field(default_factory=list)
    active: Optional[str] = None  # 当前进行中的步骤名（token 归属）

    def __post_init__(self):
        # 新建实例时记录起点；从检查点反序列化时 start 已有值，不覆盖
        if self.enabled and not self.start:
            self.start = time.perf_counter()

    @contextmanager
    def step(self, name: str):
        """步骤埋点：记录名称与耗时；步骤内产生的 token 调 add_tokens 归到该步"""
        if not self.enabled:
            yield
            return
        begin = time.perf_counter()
        prev_active, self.active = self.active, name
        try:
            yield
        finally:
            elapsed_ms = round((time.perf_counter() - begin) * 1000)
            self.steps.append({"name": name, "elapsed_ms": elapsed_ms})
            self.active = prev_active

    def add_tokens(self, prompt_tokens: int = 0, completion_tokens: int = 0, total_tokens: int = 0):
        """把一次 LLM 调用的 token 记到当前步骤（无活动步骤则记为"其他"）"""
        if not self.enabled:
            return
        name = self.active or "其他"
        for s in reversed(self.steps):
            if s["name"] == name:
                s["prompt_tokens"] = s.get("prompt_tokens", 0) + prompt_tokens
                s["completion_tokens"] = s.get("completion_tokens", 0) + completion_tokens
                s["total_tokens"] = s.get("total_tokens", 0) + total_tokens
                return
        # 步骤尚未结束（进行中）：暂存到临时记录，step() 结束时并入
        self.steps.append({
            "name": name, "elapsed_ms": 0,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "_pending": True,
        })

    def finish(self) -> dict:
        """生成 TurnUsage 摘要：总耗时 + 总 token + 分步明细；关闭时返回空统计"""
        if not self.enabled:
            return {
                "elapsed_ms": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "steps": [],
            }
        elapsed_ms = round((time.perf_counter() - self.start) * 1000)
        # 合并进行中步骤的 token 暂存记录（同名步骤合并）
        merged: List[dict] = []
        for s in self.steps:
            target = next((m for m in merged if m["name"] == s["name"]), None)
            if target is None:
                merged.append(dict(s))
            else:
                target["elapsed_ms"] += s["elapsed_ms"]
                for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    target[k] = target.get(k, 0) + s.get(k, 0)
        for m in merged:
            m.pop("_pending", None)
            m.setdefault("prompt_tokens", 0)
            m.setdefault("completion_tokens", 0)
            m.setdefault("total_tokens", 0)
        return {
            "elapsed_ms": elapsed_ms,
            "prompt_tokens": sum(m["prompt_tokens"] for m in merged),
            "completion_tokens": sum(m["completion_tokens"] for m in merged),
            "total_tokens": sum(m["total_tokens"] for m in merged),
            "steps": merged,
        }
