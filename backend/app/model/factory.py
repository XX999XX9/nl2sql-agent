# 模型工厂：根据配置创建 LLM/Embedding 客户端实例，屏蔽底层供应商差异，
# 统一返回 token usage 供统计模块使用（百炼 DashScope OpenAI 兼容模式）

from dataclasses import dataclass, field
from typing import List, Optional

from openai import OpenAI

from app.utils.config_handler import load_yaml
from app.utils.logger_handler import setup_logger
from app.utils.path_tool import BACKEND_ROOT

logger = setup_logger()


@dataclass
class LLMResult:
    """LLM 调用统一结果：正文 + token usage"""

    content: str = ""
    reasoning: str = ""                       # 推理模型的思考内容（无则为空）
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    model: str = ""
    finish_reason: str = ""                   # stop/length/tool_calls/content_filter…
    tool_calls: list = field(default_factory=list)
    # 原生 function calling：OpenAI 结构
    # [{"id":"call_xxx","type":"function","function":{"name":"...","arguments":"{...}"}}]，
    # 可直接作为 assistant 消息的 tool_calls 回拼

    @property
    def usage(self) -> dict:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


class LLMClient:
    """OpenAI 兼容 Chat Completions 客户端封装"""

    def __init__(self, api_key: str, base_url: str, model: str,
                 temperature: float = 0.0, max_tokens: int = 2048, timeout: int = 60):
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)

    def chat(self, messages: List[dict], temperature: Optional[float] = None,
             tools: Optional[list] = None, tool_choice=None) -> LLMResult:
        """同步调用；messages 为 OpenAI 格式 [{role, content(, tool_calls)}]。
        tools/tool_choice 为 OpenAI 原生 function calling 参数（None=不启用）。
        推理模型思考过长会耗尽 max_tokens 导致正文为空（finish_reason=length），
        此时按 4×/16× 两级扩容重试；tool_calls 响应 content 本就为空，不触发扩容"""
        result = self._to_result(
            self._create(messages, temperature, self.max_tokens, tools, tool_choice))
        for scale in (4, 16):
            if result.content or result.tool_calls:
                return result
            logger.warning(
                "LLM 返回空正文（finish_reason=%s, reasoning=%d字, completion_tokens=%d），%d× 扩容重试",
                result.finish_reason, len(result.reasoning), result.completion_tokens, scale,
            )
            result = self._to_result(
                self._create(messages, temperature, self.max_tokens * scale, tools, tool_choice))
        if not result.content:
            logger.error("扩容重试后仍为空正文（finish_reason=%s）", result.finish_reason)
        return result

    def _create(self, messages: List[dict], temperature: Optional[float], max_tokens: int,
                tools: Optional[list] = None, tool_choice=None):
        kwargs = dict(
            model=self.model,
            messages=messages,
            temperature=self.temperature if temperature is None else temperature,
            max_tokens=max_tokens,
        )
        if tools:                      # 仅在提供工具时携带，保持普通调用与此前完全一致
            kwargs["tools"] = tools
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice
        return self.client.chat.completions.create(**kwargs)

    def _to_result(self, resp) -> LLMResult:
        msg = resp.choices[0].message if resp.choices else None
        usage = resp.usage
        finish = resp.choices[0].finish_reason if resp.choices else ""
        tool_calls = []
        if msg is not None and getattr(msg, "tool_calls", None):
            for tc in msg.tool_calls:
                fn = getattr(tc, "function", None)
                tool_calls.append({
                    "id": getattr(tc, "id", ""),
                    "type": getattr(tc, "type", "function"),
                    "function": {
                        "name": getattr(fn, "name", ""),
                        "arguments": getattr(fn, "arguments", "") or "",
                    },
                })
        return LLMResult(
            content=(msg.content or "") if msg else "",
            reasoning=getattr(msg, "reasoning_content", None) or "" if msg else "",
            tool_calls=tool_calls,
            prompt_tokens=getattr(usage, "prompt_tokens", 0) if usage else 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) if usage else 0,
            total_tokens=getattr(usage, "total_tokens", 0) if usage else 0,
            model=self.model,
            finish_reason=finish or "",
        )


class EmbeddingClient:
    """OpenAI 兼容 Embeddings 客户端封装（百炼 text-embedding-v3 等）"""

    def __init__(self, api_key: str, base_url: str, model: str, timeout: int = 60):
        self.model = model
        self.client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)

    def embed(self, texts: List[str]) -> List[List[float]]:
        """批量向量化；texts 为空返回空列表"""
        if not texts:
            return []
        # 百炼单批最多 10 条文本，超出分批
        vectors = []
        for i in range(0, len(texts), 10):
            batch = texts[i:i + 10]
            resp = self.client.embeddings.create(model=self.model, input=batch)
            vectors.extend(d.embedding for d in resp.data)
        return vectors


class ModelFactory:
    """模型工厂：从 config/agent.yml 读取配置创建客户端，全局复用"""

    def __init__(self):
        self._config = load_yaml(BACKEND_ROOT / "config" / "agent.yml")

    def create_llm(self) -> LLMClient:
        cfg = self._config["llm"]
        return LLMClient(
            api_key=cfg["api_key"],
            base_url=cfg["base_url"],
            model=cfg["model"],
            temperature=cfg.get("temperature", 0.0),
            max_tokens=cfg.get("max_tokens", 4096),
            timeout=cfg.get("timeout", 60),
        )

    def create_embedding(self) -> EmbeddingClient:
        cfg = self._config["embedding"]
        return EmbeddingClient(
            api_key=cfg["api_key"],
            base_url=cfg["base_url"],
            model=cfg["model"],
            timeout=cfg.get("timeout", 60),
        )
