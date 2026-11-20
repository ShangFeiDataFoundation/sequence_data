"""Shared runtime model settings API."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..config import get_settings
from ..schemas import (
    ErrorResponse,
    ModelConnectionRequest,
    ModelConnectionResponse,
    ModelSettingsResponse,
)
from ..services.runtime_model_config import (
    MASKED_API_KEY,
    ModelConfigurationError,
    get_runtime_model_config,
    test_model_connection,
)

router = APIRouter(prefix="/api/v1/settings", tags=["settings"])


def _public_settings() -> ModelSettingsResponse:
    credentials = get_runtime_model_config().effective(get_settings())
    return ModelSettingsResponse(
        base_url=credentials.base_url,
        model=credentials.model,
        configured=credentials.configured,
        key_masked=MASKED_API_KEY if credentials.configured else None,
        source=credentials.source,
        expires_on=credentials.expires_on,
    )


def _candidate_key(api_key: str | None) -> str:
    clean = (api_key or "").strip()
    if clean and clean != MASKED_API_KEY:
        return clean
    return get_runtime_model_config().effective(get_settings()).api_key


@router.get(
    "/model",
    response_model=ModelSettingsResponse,
    summary="读取当前模型配置状态",
)
def get_model_settings() -> ModelSettingsResponse:
    return _public_settings()


@router.post(
    "/model/test",
    response_model=ModelConnectionResponse,
    summary="测试模型连接和 JSON 模式",
    responses={400: {"model": ErrorResponse}},
)
def test_model_settings(
    request: ModelConnectionRequest,
) -> ModelConnectionResponse:
    try:
        message = test_model_connection(
            request.base_url,
            request.model,
            _candidate_key(request.api_key),
        )
    except ModelConfigurationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ModelConnectionResponse(success=True, message=message)


@router.post(
    "/model/save",
    response_model=ModelSettingsResponse,
    summary="测试并保存当天模型配置",
    responses={400: {"model": ErrorResponse}},
)
def save_model_settings(
    request: ModelConnectionRequest,
) -> ModelSettingsResponse:
    api_key = _candidate_key(request.api_key)
    try:
        test_model_connection(request.base_url, request.model, api_key)
        get_runtime_model_config().save_verified(
            request.base_url,
            request.model,
            api_key,
        )
    except ModelConfigurationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _public_settings()
