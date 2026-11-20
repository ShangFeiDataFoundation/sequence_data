"""Database source connection and table discovery API."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..config import get_settings
from ..schemas import DatabaseConnectionResponse, DatabaseConnectRequest, ErrorResponse
from ..services.database_source import (
    DatabaseSourceError,
    connect_and_discover,
)

router = APIRouter(prefix="/api/v1/database", tags=["database"])


@router.post(
    "/connections",
    response_model=DatabaseConnectionResponse,
    summary="连接数据库并读取可选数据表",
    responses={400: {"model": ErrorResponse}},
)
def connect_database(request: DatabaseConnectRequest) -> DatabaseConnectionResponse:
    try:
        return connect_and_discover(request, settings=get_settings())
    except DatabaseSourceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
