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
                 temperature: float = 0.0, max_tokens: int = 2048, timeout: int = 60,
                 fallback_model: str = ""):
        self.model = model
        self.fallback_model = fallback_model or ""
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)

    def chat(self, messages: List[dict], temperature: Optional[float] = None,
             tools: Optional[list] = None, tool_choice=None) -> LLMResult:
        """同步调用；messages 为 OpenAI 格式 [{role, content(, tool_calls)}]。
        tools/tool_choice 为 OpenAI 原生 function calling 参数（None=不启用）。
        主模型（.env 的 BAILIAN_MODEL）优先；调用抛异常或扩容后仍空响应时，
        自动用 fallback_model（默认 qwen-plus）整体重试一次，再失败则抛出原异常。"""
        try:
            return self._chat_with_model(
                self.model, messages, temperature, tools, tool_choice)
        except Exception as primary_err:
            if self.fallback_model and self.fallback_model != self.model:
                logger.warning(
                    "主模型 %s 调用失败（%s），降级到 %s 重试一次",
                    self.model, str(primary_err).splitlines()[0][:200], self.fallback_model)
                return self._chat_with_model(
                    self.fallback_model, messages, temperature, tools, tool_choice,
                    last_err=primary_err)
            raise

    def _chat_with_model(self, model: str, messages: List[dict],
                         temperature: Optional[float], tools: Optional[list],
                         tool_choice, last_err: Optional[Exception] = None) -> LLMResult:
        """指定模型的一次完整尝试（含空正文 4×/16× 扩容重试）；
        扩容后仍为空时，若上层已有主模型异常则抛出，否则抛 RuntimeError 交由 chat() 降级"""
        result = self._to_result(
            self._create(model, messages, temperature, self.max_tokens, tools, tool_choice))
        for scale in (4, 16):
            if result.content or result.tool_calls:
                return result
            logger.warning(
                "模型 %s 返回空正文（finish_reason=%s, reasoning=%d字, completion_tokens=%d），%d× 扩容重试",
                model, result.finish_reason, len(result.reasoning), result.completion_tokens, scale,
            )
            result = self._to_result(
                self._create(model, messages, temperature, self.max_tokens * scale,
                             tools, tool_choice))
        if not result.content and not result.tool_calls:
            logger.error("模型 %s 扩容重试后仍为空响应（finish_reason=%s）", model, result.finish_reason)
            raise RuntimeError(f"模型 {model} 多次扩容后仍返回空响应")
        return result

    def _create(self, model: str, messages: List[dict], temperature: Optional[float],
                max_tokens: int, tools: Optional[list] = None, tool_choice=None):
        kwargs = dict(
            model=model,
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
        model = (cfg.get("model") or "").strip()
        if not model or "${" in model:
            # 不静默回退默认模型：主模型必须由 .env/环境变量显式配置
            raise RuntimeError(
                "主模型未配置：请在 backend/.env 设置 BAILIAN_MODEL（占位符未被替换）")
        return LLMClient(
            api_key=cfg["api_key"],
            base_url=cfg["base_url"],
            model=model,
            temperature=cfg.get("temperature", 0.0),
            max_tokens=cfg.get("max_tokens", 4096),
            timeout=cfg.get("timeout", 60),
            fallback_model=(cfg.get("fallback_model") or "").strip(),
        )

    def create_embedding(self) -> EmbeddingClient:
        cfg = self._config["embedding"]
        return EmbeddingClient(
            api_key=cfg["api_key"],
            base_url=cfg["base_url"],
            model=cfg["model"],
            timeout=cfg.get("timeout", 60),
        )
