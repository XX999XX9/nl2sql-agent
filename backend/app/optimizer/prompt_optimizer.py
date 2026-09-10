# 优化器：调用 LLM 按选定策略对提示词做改写/精简/结构化，输出优化版与修改说明；
# 支持将优化结果应用回 prompts/ 提示词文件（受 optimizer.yml allow_write_back 控制，
# 写回前自动备份 .bak）

import re
from typing import List

from app.model.factory import LLMClient, ModelFactory
from app.optimizer.strategies import STRATEGIES, Strategy, get_strategy
from app.utils.config_handler import load_yaml
from app.utils.file_handler import read_text
from app.utils.logger_handler import setup_logger
from app.utils.path_tool import BACKEND_ROOT

logger = setup_logger()

PROMPTS_DIR = BACKEND_ROOT / "prompts"
# 元提示词自身不允许被覆盖（会破坏优化器）
_PROTECTED = {"prompt_optimize_prompt.txt"}

_OPTIMIZED_RE = re.compile(r"<optimized>(.*?)</optimized>", re.DOTALL)
_NOTES_RE = re.compile(r"<notes>(.*?)</notes>", re.DOTALL)


class PromptOptimizer:
    """提示词优化器：LLM 改写 + 应用回写"""

    def __init__(self, llm: LLMClient = None):
        self.llm = llm or ModelFactory().create_llm()
        cfg = load_yaml(BACKEND_ROOT / "config" / "optimizer.yml")
        self.allow_write_back = bool(cfg.get("allow_write_back", True))
        self._meta_prompt = read_text(PROMPTS_DIR / "prompt_optimize_prompt.txt")

    # ------------------------------------------------------------------ 优化
    def optimize(self, prompt: str, strategy_key: str, requirements: str = "") -> dict:
        """按策略优化提示词，返回 {strategy, optimized, notes, usage}"""
        strategy: Strategy = get_strategy(strategy_key)
        requirements_block = (
            f"【额外要求】\n{requirements.strip()}\n" if requirements.strip() else ""
        )
        meta = self._meta_prompt.format(
            strategy_name=strategy.name,
            strategy_description=strategy.description,
            strategy_guidance=strategy.guidance,
            requirements_block=requirements_block,
            prompt=prompt.strip(),
        )
        result = self.llm.chat([
            {"role": "system", "content": "你是指令遵循严格的提示词工程助手。"},
            {"role": "user", "content": meta},
        ])

        optimized = (m := _OPTIMIZED_RE.search(result.content)) and m.group(1).strip()
        notes = (m := _NOTES_RE.search(result.content)) and m.group(1).strip()
        if not optimized:
            # 标记缺失时兜底：整体输出视为优化结果
            logger.warning("优化输出缺少 <optimized> 标记，使用原始输出兜底")
            optimized = result.content.strip()
            notes = notes or "（模型未按格式输出修改说明）"
        return {
            "strategy": strategy_key,
            "strategy_name": strategy.name,
            "optimized": optimized,
            "notes": notes or "",
            "usage": result.usage,
        }

    # ------------------------------------------------------------------ 文件
    @staticmethod
    def list_prompt_files() -> List[str]:
        """prompts/ 下可加载/可写回的提示词文件（排除元提示词自身）"""
        return sorted(
            f.name for f in PROMPTS_DIR.glob("*.txt")
            if f.name not in _PROTECTED
        )

    @staticmethod
    def _safe_path(filename: str):
        """校验文件名合法且在 prompts/ 内（防路径穿越），返回 Path 或抛 ValueError"""
        if not filename or "/" in filename or "\\" in filename or ".." in filename:
            raise ValueError("非法文件名")
        if not filename.endswith(".txt") or filename in _PROTECTED:
            raise ValueError("目标文件不可写")
        path = PROMPTS_DIR / filename
        if PROMPTS_DIR.resolve() not in path.resolve().parents:
            raise ValueError("非法文件名")
        return path

    @classmethod
    def read_prompt_file(cls, filename: str) -> str:
        path = cls._safe_path(filename)
        if not path.exists():
            raise FileNotFoundError(filename)
        return read_text(path)

    def apply(self, filename: str, content: str) -> str:
        """将优化结果写回提示词文件（自动备份 .bak）；返回写入路径"""
        if not self.allow_write_back:
            raise PermissionError("配置已禁止写回（optimizer.yml allow_write_back=false）")
        path = self._safe_path(filename)
        if not path.exists():
            raise FileNotFoundError(filename)
        backup = path.with_suffix(path.suffix + ".bak")
        backup.write_text(read_text(path), encoding="utf-8")
        path.write_text(content, encoding="utf-8")
        logger.info("提示词写回：%s（备份 %s）", path.name, backup.name)
        return path.name
