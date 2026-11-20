"""Common database connection, schema discovery and bounded snapshot reads."""

from __future__ import annotations

import csv
import io
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from datetime import time as datetime_time
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..config import PROJECT_ROOT, Settings
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

DEFAULT_PORTS = {
    "mysql": 3306,
    "mariadb": 3306,
    "postgresql": 5432,
    "sqlserver": 1433,
    "sqlite": None,
    "oracle": 1521,
}
DATABASE_LABELS = {
    "mysql": "MySQL",
    "mariadb": "MariaDB",
    "postgresql": "PostgreSQL",
    "sqlserver": "SQL Server",
    "sqlite": "SQLite",
    "oracle": "Oracle",
}
_TIME_TYPE_NAMES = {
    "date",
    "datetime",
    "datetime2",
    "smalldatetime",
    "timestamp",
    "timestamptz",
    "time",
}
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

    @property
    def display_name(self) -> str:
        label = DATABASE_LABELS.get(
            self.request.database_type,
            self.request.database_type,
        )
        name = self.request.connection_name or self.request.database
        return f"{label} · {name}"


def connect_and_discover(
    request: DatabaseConnectRequest,
    *,
    settings: Settings,
) -> DatabaseConnectionResponse:
    """Connect once, discover readable tables, and retain secrets in memory only."""
    _validate_request(request)
    kind = request.database_type
    try:
        with _open_connection(request, settings) as connection:
            table_rows, columns_by_table = _discover_schema(
                kind,
                connection,
                request,
            )
    except DatabaseSourceError:
        raise
    except Exception as exc:
        raise DatabaseSourceError(
            f"{DATABASE_LABELS.get(kind, kind)} 连接失败: {exc}"
        ) from exc

    tables: dict[tuple[str, str], DatabaseTableInfo] = {}
    for schema_name, table_name in table_rows:
        key = (schema_name, table_name)
        columns = columns_by_table.get(key, [])
        tables[key] = DatabaseTableInfo(
            schema_name=schema_name,
            table_name=table_name,
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
        database_type=kind,
        database_name=request.database,
        display_name=stored.display_name,
        tables=list(tables.values()),
        expires_at=expires_at,
    )


def get_connection_metadata(connection_id: str) -> dict[str, str]:
    stored = _get_stored(connection_id)
    return {
        "connection_id": connection_id,
        "database_type": stored.request.database_type,
        "database_name": stored.request.database,
        "display_name": stored.display_name,
    }


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
    """Read at most 500 rows from one table and return a CSV parser input."""
    if not 1 <= max_rows <= 500:
        raise DatabaseSourceError("最大行数必须在 1 到 500 之间。")

    stored = _get_stored(connection_id)
    kind = stored.request.database_type
    schema_name = selection.schema_name or stored.request.schema_name or stored.request.database
    table_info = stored.tables.get((schema_name, selection.table_name))
    if table_info is None:
        raise DatabaseSourceError(
            f"数据表不存在或未授权读取: {schema_name}.{selection.table_name}"
        )

    time_column = selection.time_column or table_info.default_time_column
    if time_column and time_column not in {column.name for column in table_info.columns}:
        raise DatabaseSourceError(f"时间字段不存在: {time_column}")
    if (selection.time_start or selection.time_end) and not time_column:
        raise DatabaseSourceError("设置时间范围时必须选择时间字段。")

    sql, parameters = _build_select(
        kind,
        schema_name,
        selection.table_name,
        time_column=time_column,
        time_start=selection.time_start,
        time_end=selection.time_end,
        max_rows=max_rows,
    )
    try:
        with (
            _open_connection(stored.request, settings) as connection,
            _cursor_scope(connection) as cursor,
        ):
            cursor.execute(sql, parameters)
            rows = _cursor_rows(cursor)
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


def _validate_request(request: DatabaseConnectRequest) -> None:
    if request.database_type == "sqlite":
        return
    missing = [
        label
        for label, value in (
            ("主机", request.host),
            ("用户名", request.username),
        )
        if value is None
    ]
    if request.password is None:
        missing.append("密码")
    if missing:
        raise DatabaseSourceError(f"请填写{', '.join(missing)}。")


@contextmanager
def _open_connection(
    request: DatabaseConnectRequest,
    settings: Settings,
) -> Iterator[Any]:
    kind = request.database_type
    connection: Any = None
    if kind == "sqlite":
        path = _sqlite_path(request.database)
        if not path.is_file():
            raise DatabaseSourceError(f"SQLite 数据库文件不存在: {path}")
        connection = sqlite3.connect(
            f"file:{path.as_posix()}?mode=ro",
            uri=True,
        )
    elif kind in {"mysql", "mariadb"}:
        try:
            import pymysql
            from pymysql.cursors import DictCursor
        except ImportError as exc:
            raise DatabaseSourceError("缺少 PyMySQL 依赖。") from exc
        connection = pymysql.connect(
            host=request.host,
            port=_port(request),
            user=request.username,
            password=_password(request),
            database=request.database,
            charset="utf8mb4",
            cursorclass=DictCursor,
            autocommit=True,
            connect_timeout=settings.database_connect_timeout_seconds,
            read_timeout=settings.database_query_timeout_seconds,
            write_timeout=settings.database_query_timeout_seconds,
        )
    elif kind == "postgresql":
        try:
            import psycopg
        except ImportError as exc:
            raise DatabaseSourceError("缺少 psycopg 依赖。") from exc
        connection = psycopg.connect(
            host=request.host,
            port=_port(request),
            dbname=request.database,
            user=request.username,
            password=_password(request),
            connect_timeout=settings.database_connect_timeout_seconds,
        )
    elif kind == "sqlserver":
        try:
            import pymssql
        except ImportError as exc:
            raise DatabaseSourceError("缺少 pymssql 依赖。") from exc
        connection = pymssql.connect(
            server=request.host,
            port=str(_port(request)),
            user=request.username,
            password=_password(request),
            database=request.database,
            login_timeout=settings.database_connect_timeout_seconds,
            timeout=settings.database_query_timeout_seconds,
            as_dict=True,
        )
    elif kind == "oracle":
        try:
            import oracledb
        except ImportError as exc:
            raise DatabaseSourceError("缺少 oracledb 依赖。") from exc
        dsn = oracledb.makedsn(
            request.host,
            _port(request),
            service_name=request.database,
        )
        connection = oracledb.connect(
            user=request.username,
            password=_password(request),
            dsn=dsn,
        )
    else:
        raise DatabaseSourceError(f"不支持的数据库类型: {kind}")

    try:
        yield connection
    finally:
        connection.close()


def _discover_schema(
    kind: str,
    connection: Any,
    request: DatabaseConnectRequest,
) -> tuple[
    list[tuple[str, str]],
    dict[tuple[str, str], list[DatabaseColumnInfo]],
]:
    if kind in {"mysql", "mariadb"}:
        return _discover_mysql(connection, request)
    if kind == "postgresql":
        return _discover_postgresql(connection, request)
    if kind == "sqlserver":
        return _discover_sqlserver(connection)
    if kind == "sqlite":
        return _discover_sqlite(connection)
    if kind == "oracle":
        return _discover_oracle(connection, request)
    raise DatabaseSourceError(f"不支持的数据库类型: {kind}")


def _discover_mysql(
    connection: Any,
    request: DatabaseConnectRequest,
) -> tuple[list[tuple[str, str]], dict[tuple[str, str], list[DatabaseColumnInfo]]]:
    with _cursor_scope(connection) as cursor:
        cursor.execute(
            """
            SELECT TABLE_SCHEMA, TABLE_NAME
            FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = %s AND TABLE_TYPE = 'BASE TABLE'
            ORDER BY TABLE_NAME
            """,
            (request.database,),
        )
        table_rows = [
            (str(row["TABLE_SCHEMA"]), str(row["TABLE_NAME"]))
            for row in _cursor_rows(cursor)
        ]
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
        columns = _columns_from_rows(
            _cursor_rows(cursor),
            primary_field="COLUMN_KEY",
            description_field="COLUMN_COMMENT",
        )
    return table_rows, columns


def _discover_postgresql(
    connection: Any,
    request: DatabaseConnectRequest,
) -> tuple[list[tuple[str, str]], dict[tuple[str, str], list[DatabaseColumnInfo]]]:
    with _cursor_scope(connection) as cursor:
        cursor.execute(
            """
            SELECT table_schema, table_name
            FROM information_schema.tables
            WHERE table_catalog = %s
              AND table_type = 'BASE TABLE'
              AND table_schema NOT IN ('pg_catalog', 'information_schema')
            ORDER BY table_schema, table_name
            """,
            (request.database,),
        )
        table_rows = [
            (str(row["table_schema"]), str(row["table_name"]))
            for row in _cursor_rows(cursor)
        ]
        cursor.execute(
            """
            SELECT table_schema, table_name, column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_catalog = %s
              AND table_schema NOT IN ('pg_catalog', 'information_schema')
            ORDER BY table_schema, table_name, ordinal_position
            """,
            (request.database,),
        )
        raw_columns = _cursor_rows(cursor)
        cursor.execute(
            """
            SELECT kcu.table_schema, kcu.table_name, kcu.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
             AND tc.table_catalog = kcu.table_catalog
            WHERE tc.constraint_type = 'PRIMARY KEY'
              AND tc.table_catalog = %s
            """,
            (request.database,),
        )
        primary_keys = {
            (
                str(row["table_schema"]),
                str(row["table_name"]),
                str(row["column_name"]),
            )
            for row in _cursor_rows(cursor)
        }
    return table_rows, _normalized_columns(raw_columns, primary_keys)


def _discover_sqlserver(
    connection: Any,
) -> tuple[list[tuple[str, str]], dict[tuple[str, str], list[DatabaseColumnInfo]]]:
    with _cursor_scope(connection) as cursor:
        cursor.execute(
            """
            SELECT TABLE_SCHEMA, TABLE_NAME
            FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_TYPE = 'BASE TABLE'
            ORDER BY TABLE_SCHEMA, TABLE_NAME
            """
        )
        table_rows = [
            (str(row["TABLE_SCHEMA"]), str(row["TABLE_NAME"]))
            for row in _cursor_rows(cursor)
        ]
        cursor.execute(
            """
            SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, DATA_TYPE, IS_NULLABLE
            FROM INFORMATION_SCHEMA.COLUMNS
            ORDER BY TABLE_SCHEMA, TABLE_NAME, ORDINAL_POSITION
            """
        )
        raw_columns = _cursor_rows(cursor)
        cursor.execute(
            """
            SELECT tc.TABLE_SCHEMA, tc.TABLE_NAME, kcu.COLUMN_NAME
            FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
            JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE kcu
              ON tc.CONSTRAINT_NAME = kcu.CONSTRAINT_NAME
             AND tc.TABLE_SCHEMA = kcu.TABLE_SCHEMA
            WHERE tc.CONSTRAINT_TYPE = 'PRIMARY KEY'
            """
        )
        primary_keys = {
            (str(row["TABLE_SCHEMA"]), str(row["TABLE_NAME"]), str(row["COLUMN_NAME"]))
            for row in _cursor_rows(cursor)
        }
    return table_rows, _normalized_columns(raw_columns, primary_keys)


def _discover_sqlite(
    connection: Any,
) -> tuple[list[tuple[str, str]], dict[tuple[str, str], list[DatabaseColumnInfo]]]:
    with _cursor_scope(connection) as cursor:
        cursor.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        )
        table_rows = [("main", str(row[0])) for row in cursor.fetchall()]
        columns: dict[tuple[str, str], list[DatabaseColumnInfo]] = {}
        for schema_name, table_name in table_rows:
            cursor.execute(f"PRAGMA table_info({_quote_identifier('sqlite', table_name)})")
            rows = cursor.fetchall()
            columns[(schema_name, table_name)] = [
                DatabaseColumnInfo(
                    name=str(row[1]),
                    data_type=str(row[2] or "text").lower(),
                    nullable=not bool(row[3]),
                    is_primary_key=bool(row[5]),
                )
                for row in rows
            ]
    return table_rows, columns


def _discover_oracle(
    connection: Any,
    request: DatabaseConnectRequest,
) -> tuple[list[tuple[str, str]], dict[tuple[str, str], list[DatabaseColumnInfo]]]:
    owner = (request.schema_name or request.username or "").upper()
    if not owner:
        raise DatabaseSourceError("Oracle 连接需要用户名或 Schema。")
    with _cursor_scope(connection) as cursor:
        cursor.execute(
            """
            SELECT owner, table_name
            FROM all_tables
            WHERE owner = :owner AND nested = 'NO'
            ORDER BY table_name
            """,
            owner=owner,
        )
        table_rows = [
            (str(row["OWNER"]), str(row["TABLE_NAME"]))
            for row in _cursor_rows(cursor)
        ]
        cursor.execute(
            """
            SELECT owner, table_name, column_name, data_type, nullable
            FROM all_tab_columns
            WHERE owner = :owner
            ORDER BY table_name, column_id
            """,
            owner=owner,
        )
        raw_columns = _cursor_rows(cursor)
        cursor.execute(
            """
            SELECT ac.owner, ac.table_name, acc.column_name
            FROM all_constraints ac
            JOIN all_cons_columns acc
              ON ac.owner = acc.owner AND ac.constraint_name = acc.constraint_name
            WHERE ac.constraint_type = 'P' AND ac.owner = :owner
            """,
            owner=owner,
        )
        primary_keys = {
            (str(row["OWNER"]), str(row["TABLE_NAME"]), str(row["COLUMN_NAME"]))
            for row in _cursor_rows(cursor)
        }
    return table_rows, _normalized_columns(raw_columns, primary_keys)


def _normalized_columns(
    raw_rows: list[dict[str, Any]],
    primary_keys: set[tuple[str, str, str]],
) -> dict[tuple[str, str], list[DatabaseColumnInfo]]:
    columns: dict[tuple[str, str], list[DatabaseColumnInfo]] = {}
    for row in raw_rows:
        schema_name = str(row.get("table_schema") or row.get("TABLE_SCHEMA"))
        table_name = str(row.get("table_name") or row.get("TABLE_NAME"))
        column_name = str(row.get("column_name") or row.get("COLUMN_NAME"))
        key = (schema_name, table_name)
        columns.setdefault(key, []).append(
            DatabaseColumnInfo(
                name=column_name,
                data_type=str(
                    row.get("data_type") or row.get("DATA_TYPE")
                ).lower(),
                nullable=str(
                    row.get("is_nullable")
                    or row.get("IS_NULLABLE")
                    or row.get("nullable")
                    or row.get("NULLABLE")
                ).upper() == "YES",
                is_primary_key=(schema_name, table_name, column_name) in primary_keys,
            )
        )
    return columns


def _columns_from_rows(
    rows: list[dict[str, Any]],
    *,
    primary_field: str,
    description_field: str,
) -> dict[tuple[str, str], list[DatabaseColumnInfo]]:
    columns: dict[tuple[str, str], list[DatabaseColumnInfo]] = {}
    for row in rows:
        key = (str(row["TABLE_SCHEMA"]), str(row["TABLE_NAME"]))
        columns.setdefault(key, []).append(
            DatabaseColumnInfo(
                name=str(row["COLUMN_NAME"]),
                data_type=str(row["DATA_TYPE"]).lower(),
                nullable=str(row["IS_NULLABLE"]).upper() == "YES",
                is_primary_key=(
                    primary_field == "COLUMN_KEY"
                    and str(row.get(primary_field) or "").upper() == "PRI"
                ),
                description=str(row.get(description_field) or "").strip() or None,
            )
        )
    return columns


def _build_select(
    kind: str,
    schema_name: str,
    table_name: str,
    *,
    time_column: str | None,
    time_start: datetime | None,
    time_end: datetime | None,
    max_rows: int,
) -> tuple[str, tuple[Any, ...]]:
    qualified = (
        f"{_quote_identifier(kind, schema_name)}."
        f"{_quote_identifier(kind, table_name)}"
    )
    placeholder = _placeholder(kind)
    where_parts: list[str] = []
    parameters: list[Any] = []
    if time_column:
        quoted_time = _quote_identifier(kind, time_column)
        if time_start:
            where_parts.append(f"{quoted_time} >= {placeholder}")
            parameters.append(_datetime_parameter(kind, time_start))
        if time_end:
            where_parts.append(f"{quoted_time} <= {placeholder}")
            parameters.append(_datetime_parameter(kind, time_end))
    where_sql = f" WHERE {' AND '.join(where_parts)}" if where_parts else ""
    order_sql = f" ORDER BY {_quote_identifier(kind, time_column)} DESC" if time_column else ""

    if kind == "sqlserver":
        sql = f"SELECT TOP ({max_rows}) * FROM {qualified}{where_sql}{order_sql}"
    elif kind == "oracle":
        base = f"SELECT * FROM {qualified}{where_sql}{order_sql}"
        sql = f"SELECT * FROM ({base}) WHERE ROWNUM <= {max_rows}"
    else:
        sql = f"SELECT * FROM {qualified}{where_sql}{order_sql} LIMIT {placeholder}"
        parameters.append(max_rows)
    return sql, tuple(parameters)


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


@contextmanager
def _cursor_scope(connection: Any) -> Iterator[Any]:
    cursor = connection.cursor()
    try:
        yield cursor
    finally:
        cursor.close()


def _cursor_rows(cursor: Any) -> list[dict[str, Any]]:
    rows = cursor.fetchall()
    if not rows:
        return []
    if isinstance(rows[0], dict):
        return [dict(row) for row in rows]
    names = [description[0] for description in (cursor.description or [])]
    return [
        {str(name): value for name, value in zip(names, row, strict=False)}
        for row in rows
    ]


def _guess_time_column(columns: list[DatabaseColumnInfo]) -> str | None:
    candidates: list[tuple[int, int, str]] = []
    for column in columns:
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
    pool = typed or [
        item for item in candidates if item[1] < len(_TIME_COLUMN_HINTS)
    ]
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
    if isinstance(value, datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, (date, datetime_time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    return value


def _datetime_parameter(kind: str, value: datetime) -> datetime | str:
    if kind == "sqlite":
        return value.isoformat(sep=" ")
    if kind in {"postgresql", "sqlserver", "oracle"}:
        return value
    if value.tzinfo is not None:
        value = value.replace(tzinfo=None)
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _quote_identifier(kind: str, value: str) -> str:
    if kind == "sqlserver":
        return f"[{value.replace(']', ']]')}]"
    if kind in {"mysql", "mariadb"}:
        return f"`{value.replace('`', '``')}`"
    return f'"{value.replace(chr(34), chr(34) * 2)}"'


def _placeholder(kind: str) -> str:
    return "?" if kind == "sqlite" else "%s"


def _port(request: DatabaseConnectRequest) -> int:
    return request.port or DEFAULT_PORTS[request.database_type] or 1


def _password(request: DatabaseConnectRequest) -> str:
    if request.password is None:
        raise DatabaseSourceError("请填写密码。")
    return request.password.get_secret_value()


def _sqlite_path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()
