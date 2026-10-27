# 提示词优化接口：提交提示词，返回优化版本与对比评估；支持策略列表/文件加载/写回

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import get_prompt_optimizer
from app.api.eval_access import require_eval_access
from app.optimizer.prompt_optimizer import PromptOptimizer
from app.optimizer.strategies import STRATEGIES
from app.schemas.common import ApiResponse
from app.schemas.prompt_optimize import (
    PromptApplyRequest,
    PromptOptimizeRequest,
    PromptOptimizeResponse,
)
from app.utils.config_handler import load_yaml
from app.utils.logger_handler import setup_logger
from app.utils.path_tool import BACKEND_ROOT

logger = setup_logger()
# 提示词优化属于内部工具，不对外开放：整个路由统一要求评测口令签发的令牌
router = APIRouter(dependencies=[Depends(require_eval_access)])


@router.get("/strategies")
def list_strategies():
    """可用优化策略列表"""
    return ApiResponse(data=[
        {"key": s.key, "name": s.name, "description": s.description}
        for s in STRATEGIES.values()
    ])


@router.get("/files")
def list_files(optimizer: PromptOptimizer = Depends(get_prompt_optimizer)):
    """可加载/可写回的提示词文件列表"""
    return ApiResponse(data={"files": optimizer.list_prompt_files(),
                             "allow_write_back": optimizer.allow_write_back})


@router.get("/file")
def read_file(name: str, optimizer: PromptOptimizer = Depends(get_prompt_optimizer)):
    """读取指定提示词文件内容（编辑器加载用）"""
    try:
        content = optimizer.read_prompt_file(name)
    except ValueError:
        raise HTTPException(400, "非法文件名")
    except FileNotFoundError:
        raise HTTPException(404, "文件不存在")
    return ApiResponse(data={"name": name, "content": content})


@router.post("", response_model=ApiResponse)
def optimize(req: PromptOptimizeRequest,
             optimizer: PromptOptimizer = Depends(get_prompt_optimizer)):
    """优化提示词；evaluate=true 时附加优化前后对比评估（多轮 LLM 调用，较慢）"""
    if req.strategy not in STRATEGIES:
        raise HTTPException(400, f"未知策略: {req.strategy}")
    try:
        result = optimizer.optimize(req.prompt, req.strategy, req.requirements)
    except Exception as e:
        logger.error("提示词优化失败: %s", e, exc_info=e)
        raise HTTPException(500, f"优化失败：{e}")

    evaluation = None
    if req.evaluate:
        try:
            from app.optimizer.evaluator import PromptEvaluator
            cfg = load_yaml(BACKEND_ROOT / "config" / "optimizer.yml")
            evaluator = PromptEvaluator(optimizer.llm, cfg.get("eval_cases", 3))
            evaluation = evaluator.evaluate(req.prompt, result["optimized"])
        except Exception as e:
            logger.error("提示词评估失败: %s", e, exc_info=e)
            evaluation = None    # 评估失败不阻断优化结果返回

    data = PromptOptimizeResponse(
        strategy=result["strategy"],
        strategy_name=result["strategy_name"],
        original=req.prompt,
        optimized=result["optimized"],
        notes=result["notes"],
        usage=result["usage"],
        evaluation=evaluation,
    )
    return ApiResponse(data=data.model_dump())


@router.post("/apply")
def apply(req: PromptApplyRequest,
          optimizer: PromptOptimizer = Depends(get_prompt_optimizer)):
    """将优化结果写回提示词文件（自动备份 .bak）"""
    try:
        name = optimizer.apply(req.filename, req.content)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError:
        raise HTTPException(400, "非法文件名")
    except FileNotFoundError:
        raise HTTPException(404, "文件不存在")
    except Exception as e:
        logger.error("提示词写回失败: %s", e, exc_info=e)
        raise HTTPException(500, f"写回失败：{e}")
    return ApiResponse(data={"filename": name, "message": "已写回（原文件备份为 .bak）"})
