"""MySQL 连接、表发现与受限制的数据快照读取。"""

from __future__ import annotations

import csv
import io
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from datetime import time as datetime_time
from decimal import Decimal
from typing import Any

from ..config import Settings
from ..schemas import (
    DatabaseColumnInfo,
    DatabaseConnectionResponse,
    DatabaseConnectRequest,
    DatabaseTableInfo,
    DatabaseTableSelection,
)
from ..storage import new_id, now

_LOCK = threading.RLock()
_CONNECTIONS: dict[str, StoredConnection] = {}

_TIME_TYPE_NAMES = {"date", "datetime", "timestamp", "time"}
_TIME_COLUMN_HINTS = (
    "timestamp",
    "event_time",
    "sample_time",
    "record_time",
    "created_at",
    "updated_at",
    "time",
    "date",
    "ts",
    "时间",
    "日期",
)


class DatabaseSourceError(RuntimeError):
    pass


class DatabaseConnectionExpired(DatabaseSourceError):
    pass


@dataclass
class StoredConnection:
    connection_id: str
    request: DatabaseConnectRequest
    tables: dict[tuple[str, str], DatabaseTableInfo]
    expires_at: datetime
    ttl_seconds: int
    last_used_monotonic: float = field(default_factory=time.monotonic)


def connect_and_discover(
    request: DatabaseConnectRequest,
    *,
    settings: Settings,
) -> DatabaseConnectionResponse:
    """建立只用于本次请求的连接并读取表结构，密码只保存在进程内存。"""
    try:
        connection = _open_connection(request, settings)
        with connection, connection.cursor() as cursor:
            cursor.execute(
                """
                    SELECT TABLE_SCHEMA, TABLE_NAME
                    FROM information_schema.TABLES
                    WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
                    ORDER BY TABLE_NAME
                    """,
                (request.database,),
            )
            table_rows = cursor.fetchall()
            cursor.execute(
                """
                    SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE,
                           IS_NULLABLE, COLUMN_KEY, COLUMN_COMMENT
                    FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = %s
                    ORDER BY TABLE_NAME, ORDINAL_POSITION
                    """,
                (request.database,),
            )
            column_rows = cursor.fetchall()
    except DatabaseSourceError:
        raise
    except Exception as exc:
        raise DatabaseSourceError(f"MySQL 连接失败: {exc}") from exc

    columns_by_table: dict[tuple[str, str], list[DatabaseColumnInfo]] = {}
    for row in column_rows:
        key = (str(row["TABLE_SCHEMA"]), str(row["TABLE_NAME"]))
        columns_by_table.setdefault(key, []).append(
            DatabaseColumnInfo(
                name=str(row["COLUMN_NAME"]),
                data_type=str(row["DATA_TYPE"]).lower(),
                nullable=str(row["IS_NULLABLE"]).upper() == "YES",
                is_primary_key=str(row["COLUMN_KEY"]).upper() == "PRI",
                description=str(row["COLUMN_COMMENT"] or "").strip() or None,
            )
        )

    tables: dict[tuple[str, str], DatabaseTableInfo] = {}
    for row in table_rows:
        key = (str(row["TABLE_SCHEMA"]), str(row["TABLE_NAME"]))
        columns = columns_by_table.get(key, [])
        tables[key] = DatabaseTableInfo(
            schema_name=key[0],
            table_name=key[1],
            columns=columns,
            default_time_column=_guess_time_column(columns),
        )

    connection_id = new_id("dbc")
    expires_at = now() + timedelta(seconds=settings.database_connection_ttl_seconds)
    stored = StoredConnection(
        connection_id=connection_id,
        request=request,
        tables=tables,
        expires_at=expires_at,
        ttl_seconds=settings.database_connection_ttl_seconds,
    )
    with _LOCK:
        _purge_expired(settings)
        _CONNECTIONS[connection_id] = stored

    return DatabaseConnectionResponse(
        connection_id=connection_id,
        database_name=request.database,
        tables=list(tables.values()),
        expires_at=expires_at,
    )


def get_connection_tables(connection_id: str) -> dict[tuple[str, str], DatabaseTableInfo]:
    stored = _get_stored(connection_id)
    return dict(stored.tables)


def fetch_table_csv(
    connection_id: str,
    selection: DatabaseTableSelection,
    *,
    max_rows: int,
    settings: Settings,
) -> tuple[bytes, DatabaseTableInfo]:
    """读取一张表最多 500 行，并转换为参与统一解析的 CSV 快照。"""
    if not 1 <= max_rows <= 500:
        raise DatabaseSourceError("最大行数必须在 1 到 500 之间。")

    stored = _get_stored(connection_id)
    schema_name = selection.schema_name or stored.request.database
    table_info = stored.tables.get((schema_name, selection.table_name))
    if table_info is None:
        raise DatabaseSourceError(f"数据表不存在或未授权读取: {schema_name}.{selection.table_name}")

    time_column = selection.time_column or table_info.default_time_column
    if time_column and time_column not in {column.name for column in table_info.columns}:
        raise DatabaseSourceError(f"时间字段不存在: {time_column}")
    if (selection.time_start or selection.time_end) and not time_column:
        raise DatabaseSourceError("设置时间范围时必须选择时间字段。")

    quoted_schema = _quote_identifier(schema_name)
    quoted_table = _quote_identifier(selection.table_name)
    where_parts: list[str] = []
    parameters: list[Any] = []
    if time_column:
        quoted_time = _quote_identifier(time_column)
        if selection.time_start:
            where_parts.append(f"{quoted_time} >= %s")
            parameters.append(_mysql_datetime(selection.time_start))
        if selection.time_end:
            where_parts.append(f"{quoted_time} <= %s")
            parameters.append(_mysql_datetime(selection.time_end))
    where_sql = f" WHERE {' AND '.join(where_parts)}" if where_parts else ""
    order_sql = f" ORDER BY {_quote_identifier(time_column)} DESC" if time_column else ""
    sql = (
        f"SELECT * FROM {quoted_schema}.{quoted_table}"
        f"{where_sql}{order_sql} LIMIT %s"
    )
    parameters.append(max_rows)

    try:
        connection = _open_connection(stored.request, settings)
        with connection, connection.cursor() as cursor:
            cursor.execute(sql, tuple(parameters))
            rows = cursor.fetchall()
    except DatabaseSourceError:
        raise
    except Exception as exc:
        raise DatabaseSourceError(
            f"读取数据表 {schema_name}.{selection.table_name} 失败: {exc}"
        ) from exc

    if time_column:
        rows = list(reversed(rows))
    return _rows_to_csv(rows, table_info.columns), table_info


def clear_connections() -> None:
    with _LOCK:
        _CONNECTIONS.clear()


def _get_stored(connection_id: str) -> StoredConnection:
    with _LOCK:
        _purge_expired(None)
        stored = _CONNECTIONS.get(connection_id)
        if stored is None:
            raise DatabaseConnectionExpired("数据库连接不存在或已过期，请重新连接。")
        stored.last_used_monotonic = time.monotonic()
        stored.expires_at = now() + timedelta(seconds=stored.ttl_seconds)
        return stored


def _purge_expired(settings: Settings | None = None) -> None:
    current = now()
    for connection_id, stored in list(_CONNECTIONS.items()):
        expired_by_time = current >= stored.expires_at
        idle_limit = (
            settings.database_connection_ttl_seconds
            if settings
            else stored.ttl_seconds
        )
        expired_by_idle = (
            time.monotonic() - stored.last_used_monotonic > idle_limit
        )
        if expired_by_time or expired_by_idle:
            _CONNECTIONS.pop(connection_id, None)


def _open_connection(request: DatabaseConnectRequest, settings: Settings):
    try:
        import pymysql
        from pymysql.cursors import DictCursor
    except ImportError as exc:  # pragma: no cover - 安装依赖后不会发生
        raise DatabaseSourceError("缺少 PyMySQL 依赖，无法连接数据库。") from exc

    try:
        return pymysql.connect(
            host=request.host,
            port=request.port,
            user=request.username,
            password=request.password.get_secret_value(),
            database=request.database,
            charset="utf8mb4",
            cursorclass=DictCursor,
            autocommit=True,
            connect_timeout=settings.database_connect_timeout_seconds,
            read_timeout=settings.database_query_timeout_seconds,
            write_timeout=settings.database_query_timeout_seconds,
        )
    except Exception as exc:
        raise DatabaseSourceError(f"MySQL 连接失败: {exc}") from exc


def _guess_time_column(columns: list[DatabaseColumnInfo]) -> str | None:
    candidates: list[tuple[int, int, str]] = []
    for index, column in enumerate(columns):
        lowered = column.name.lower()
        hint_rank = next(
            (
                rank
                for rank, hint in enumerate(_TIME_COLUMN_HINTS)
                if hint == lowered or hint in lowered
            ),
            len(_TIME_COLUMN_HINTS),
        )
        type_rank = 0 if column.data_type.lower() in _TIME_TYPE_NAMES else 1
        candidates.append((type_rank, hint_rank, column.name))
    typed = [item for item in candidates if item[0] == 0]
    pool = typed or [item for item in candidates if item[1] < len(_TIME_COLUMN_HINTS)]
    return min(pool, key=lambda item: (item[0], item[1], item[2]))[2] if pool else None


def _rows_to_csv(
    rows: list[dict[str, Any]],
    columns: list[DatabaseColumnInfo],
) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    fieldnames = [column.name for column in columns]
    writer.writerow(fieldnames)
    for row in rows:
        writer.writerow([_csv_value(row.get(name)) for name in fieldnames])
    return buffer.getvalue().encode("utf-8")


def _csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (datetime, date, datetime_time)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    return value


def _mysql_datetime(value: datetime) -> str:
    if value.tzinfo is not None:
        value = value.replace(tzinfo=None)
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _quote_identifier(value: str) -> str:
    return f"`{value.replace('`', '``')}`"
