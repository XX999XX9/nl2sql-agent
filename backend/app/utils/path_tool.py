# 路径工具：项目根目录定位、路径拼接与规范化

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = PROJECT_ROOT / "backend"


def project_path(*parts) -> Path:
    return PROJECT_ROOT.joinpath(*parts).resolve()