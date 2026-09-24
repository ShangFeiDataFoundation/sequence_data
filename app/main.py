"""数据资源描述解析平台 —— 应用入口。

启动：
    .venv/Scripts/python.exe -m uvicorn app.main:app --reload
接口文档：
    http://127.0.0.1:8000/docs
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import batches_router
from .config import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

DESCRIPTION = """
将 SQL 文件及其对应真实数据解析为固定结构的数据资源描述。

本期覆盖「上传数据、执行解析、读取解析结果」三个环节。

- **AST 层**：SQLGlot 解析 SQL 结构、字段血缘，不调用模型
- **数据画像层**：Pandas 计算物理类型、分布、枚举与标识字段，不调用模型
- **语义层**：小模型 API 或规则实现补全业务语义，人工描述始终优先
"""

app = FastAPI(
    title="数据资源描述解析平台",
    description=DESCRIPTION,
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# 后端不托管页面，预留 CORS 供独立前端联调。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(batches_router)

STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health", tags=["meta"], summary="健康检查")
def health() -> dict:
    settings = get_settings()
    return {
        "status": "ok",
        "semantic_provider": "openai_compatible" if settings.use_api_provider else "rule_based",
        "data_dir": str(settings.data_dir),
    }


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """兜底异常处理：记录堆栈，同时返回结构化错误体。"""
    logger.exception("未处理异常: %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": f"服务内部错误: {exc}"})
