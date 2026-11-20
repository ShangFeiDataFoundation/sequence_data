"""Agent 统一资源解析平台入口。

启动：
    .venv/Scripts/python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8031
接口文档：
    http://127.0.0.1:8031/docs
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import agent_router, database_router, settings_router
from .config import get_settings
from .services.database_source import DATABASE_LABELS
from .services.runtime_model_config import get_runtime_model_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

DESCRIPTION = """
递归扫描文件夹或连接 MySQL，由 Agent 为每个文件、每张数据表分别执行解析、
确定性特征提取和 LLM 语义理解，生成统一的资源 JSON。
"""

app = FastAPI(
    title="Agent 统一资源解析平台",
    description=DESCRIPTION,
    version="0.3.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

# 保留 CORS 供独立前端联调，同时托管内置工作台。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def disable_frontend_cache(request: Request, call_next):
    """页面和静态脚本始终重新校验，避免 HTML/JS 版本错配。"""
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


app.include_router(agent_router)
app.include_router(database_router)
app.include_router(settings_router)

STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health", tags=["meta"], summary="健康检查")
def health() -> dict:
    settings = get_settings()
    credentials = get_runtime_model_config().effective(settings)
    return {
        "status": "ok",
        "agent_runs_dir": str(settings.agent_runs_dir),
        "statistical_features": True,
        "database_sources": list(DATABASE_LABELS),
        "llm_configured": credentials.configured,
        "llm_source": credentials.source,
    }


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """兜底异常处理：记录堆栈，同时返回结构化错误体。"""
    logger.exception("未处理异常: %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": f"服务内部错误: {exc}"})
