# 依赖注入：提供全局单例（NL2SQLPipeline、会话管理器、结果缓存、模型工厂），供各路由通过 Depends 使用

from functools import lru_cache

from app.model.factory import ModelFactory
from app.nl2sql.conversation import ConversationManager
from app.nl2sql.pipeline import NL2SQLPipeline
from app.nl2sql.result_cache import ResultCache


@lru_cache
def get_model_factory() -> ModelFactory:
    return ModelFactory()


@lru_cache
def get_conversation_manager() -> ConversationManager:
    return ConversationManager()


@lru_cache
def get_result_cache() -> ResultCache:
    return ResultCache()


@lru_cache
def get_pipeline() -> NL2SQLPipeline:
    # 注入同一会话管理器与结果缓存实例，保证路由与流水线共享状态
    return NL2SQLPipeline(
        conversation_manager=get_conversation_manager(),
        result_cache=get_result_cache(),
    )


@lru_cache
def get_prompt_optimizer():
    # 提示词优化器单例（LLM 客户端复用）
    from app.optimizer.prompt_optimizer import PromptOptimizer
    return PromptOptimizer()
