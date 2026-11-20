"""SQL、JSON、CSV、Excel 表结构文件解析。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import sqlglot
from sqlglot import exp

from ...schemas import AgentSchemaColumn, AgentSchemaTable
from .. import profiler


def parse_schema_file(path: Path, *, dialect: str = "mysql") -> list[AgentSchemaTable]:
    suffix = path.suffix.lower()
    if suffix in {".sql", ".ddl"}:
        return _parse_sql(path.read_text(encoding="utf-8", errors="replace"), dialect, path.name)
    if suffix == ".json":
        return _parse_json(json.loads(path.read_text(encoding="utf-8")), path.name)
    if suffix in {".csv", ".xlsx", ".xls"}:
        return _parse_tabular(path)
    raise ValueError(f"不支持的表结构文件格式: {suffix}")


def _parse_sql(text: str, dialect: str, source_file: str) -> list[AgentSchemaTable]:
    tables: list[AgentSchemaTable] = []
    try:
        statements = sqlglot.parse(text, read=dialect)
    except Exception:  # noqa: BLE001
        statements = []
    for statement in statements:
        create = statement if isinstance(statement, exp.Create) else statement.find(exp.Create)
        if create is None:
            continue
        schema_node = create.this
        if isinstance(schema_node, exp.Schema):
            table_node = schema_node.this
            column_nodes = schema_node.expressions
        else:
            table_node = schema_node
            column_nodes = list(create.find_all(exp.ColumnDef))
        table_name = table_node.sql(dialect=dialect).strip("`\"[]")
        columns = [
            _column_from_sql(node, dialect)
            for node in column_nodes
            if isinstance(node, exp.ColumnDef)
        ]
        if columns:
            tables.append(
                AgentSchemaTable(
                    table_name=table_name,
                    columns=columns,
                    source_file=source_file,
                )
            )
    if tables:
        return tables
    return _parse_sql_fallback(text, source_file)


def _column_from_sql(node: exp.Expression, dialect: str) -> AgentSchemaColumn:
    if not isinstance(node, exp.ColumnDef):
        raise TypeError("不是字段定义")
    definition = node.sql(dialect=dialect)
    kind = node.args.get("kind")
    constraints = [constraint.sql(dialect=dialect).upper() for constraint in node.constraints]
    return AgentSchemaColumn(
        name=node.name,
        data_type=kind.sql(dialect=dialect) if kind is not None else None,
        nullable="NOT NULL" not in " ".join(constraints),
        description=None,
        is_primary_key="PRIMARY KEY" in definition.upper(),
    )


def _parse_sql_fallback(text: str, source_file: str) -> list[AgentSchemaTable]:
    pattern = re.compile(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([`\"\w.]+)\s*\((.*?)\)\s*;",
        re.IGNORECASE | re.DOTALL,
    )
    tables: list[AgentSchemaTable] = []
    for match in pattern.finditer(text):
        table_name = match.group(1).strip("`\"")
        columns: list[AgentSchemaColumn] = []
        for raw_line in match.group(2).splitlines():
            line = raw_line.strip().rstrip(",")
            if not line or line.upper().startswith(
                ("PRIMARY KEY", "FOREIGN KEY", "UNIQUE", "KEY ", "INDEX ", "CONSTRAINT ")
            ):
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            columns.append(
                AgentSchemaColumn(
                    name=parts[0].strip("`\""),
                    data_type=parts[1],
                    nullable="NOT NULL" not in line.upper(),
                    is_primary_key="PRIMARY KEY" in line.upper(),
                )
            )
        if columns:
            tables.append(
                AgentSchemaTable(
                    table_name=table_name,
                    columns=columns,
                    source_file=source_file,
                )
            )
    return tables


def _parse_json(payload: Any, source_file: str) -> list[AgentSchemaTable]:
    if isinstance(payload, dict) and isinstance(payload.get("tables"), list):
        entries = payload["tables"]
    elif isinstance(payload, list):
        entries = payload
    elif isinstance(payload, dict):
        entries = [
            {"table_name": table_name, "columns": columns}
            for table_name, columns in payload.items()
        ]
    else:
        raise TypeError("JSON 表结构必须是对象或数组。")
    tables: list[AgentSchemaTable] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        table_name = str(
            entry.get("table_name")
            or entry.get("table")
            or entry.get("name")
            or "unnamed"
        )
        raw_columns = entry.get("columns") or []
        columns: list[AgentSchemaColumn] = []
        if isinstance(raw_columns, dict):
            raw_columns = [
                {"name": name, **(value if isinstance(value, dict) else {"data_type": value})}
                for name, value in raw_columns.items()
            ]
        for raw in raw_columns:
            if isinstance(raw, str):
                columns.append(AgentSchemaColumn(name=raw))
            elif isinstance(raw, dict):
                columns.append(
                    AgentSchemaColumn(
                        name=str(raw.get("name") or raw.get("column_name") or "unnamed"),
                        data_type=raw.get("data_type") or raw.get("type"),
                        nullable=raw.get("nullable"),
                        description=raw.get("description"),
                        is_primary_key=bool(raw.get("is_primary_key", False)),
                    )
                )
        tables.append(
            AgentSchemaTable(
                schema_name=entry.get("schema") or entry.get("schema_name"),
                table_name=table_name,
                columns=columns,
                source_file=source_file,
            )
        )
    return tables


def _parse_tabular(path: Path) -> list[AgentSchemaTable]:
    frame = profiler.load_dataframe(path)
    aliases = {
        "table": {"table", "table_name", "表", "表名"},
        "column": {"column", "column_name", "field", "field_name", "字段", "字段名"},
        "type": {"type", "data_type", "datatype", "类型", "字段类型"},
        "nullable": {"nullable", "is_nullable", "是否可空"},
        "description": {"description", "comment", "描述", "注释"},
        "primary": {"is_primary_key", "primary_key", "pk", "主键"},
    }
    columns = {str(column).strip().lower(): str(column) for column in frame.columns}
    resolved = {
        key: next((columns[item] for item in candidates if item in columns), None)
        for key, candidates in aliases.items()
    }
    if resolved["column"] is None:
        raise ValueError("CSV/Excel 表结构需要 column_name 字段。")
    tables: dict[str, AgentSchemaTable] = {}
    for _, row in frame.iterrows():
        table_name = (
            str(row[resolved["table"]]).strip()
            if resolved["table"]
            else path.stem
        )
        nullable_value = row[resolved["nullable"]] if resolved["nullable"] else None
        nullable = None
        if nullable_value is not None and str(nullable_value).strip().lower() not in {
            "",
            "nan",
            "none",
        }:
            nullable = str(nullable_value).strip().lower() in {"1", "true", "yes", "y"}
        table = tables.setdefault(
            table_name,
            AgentSchemaTable(
                table_name=table_name,
                columns=[],
                source_file=path.name,
            ),
        )
        table.columns.append(
            AgentSchemaColumn(
                name=str(row[resolved["column"]]).strip(),
                data_type=(
                    str(row[resolved["type"]]).strip()
                    if resolved["type"] and row[resolved["type"]] is not None
                    else None
                ),
                nullable=nullable,
                description=(
                    str(row[resolved["description"]]).strip()
                    if resolved["description"] and row[resolved["description"]] is not None
                    else None
                ),
                is_primary_key=(
                    str(row[resolved["primary"]]).strip().lower()
                    in {"1", "true", "yes", "y"}
                    if resolved["primary"]
                    else False
                ),
            )
        )
    return list(tables.values())
