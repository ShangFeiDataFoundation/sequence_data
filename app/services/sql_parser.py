"""AST 层：用 SQLGlot 做确定性的结构解析（方案 §4.1）。

这一层不调用任何模型，只输出 SQL 中真实存在的字段、表达式和血缘。
解析失败不抛异常，而是降级为带 parse_warnings 的结果，让上层决定如何处理。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.lineage import lineage

from ..schemas import JoinInfo, SourceTableInfo, SqlSummary

# 用户传入的方言名与 sqlglot 方言名的映射。
_DIALECT_ALIASES = {
    "postgresql": "postgres",
    "pg": "postgres",
    "mssql": "tsql",
    "sqlserver": "tsql",
    "spark": "spark",
    "sparksql": "spark",
    "hive": "hive",
    "mysql": "mysql",
    "clickhouse": "clickhouse",
    "oracle": "oracle",
    "duckdb": "duckdb",
    "sqlite": "sqlite",
    "bigquery": "bigquery",
    "snowflake": "snowflake",
}

# 分区字段命名习惯，用于识别分区条件。
_PARTITION_HINTS = re.compile(r"(^|_)(dt|ds|pt|partition|part|bizdate|stat_date)($|_)", re.I)
# 时间字段命名习惯，用于识别时间范围条件。
_TIME_HINTS = re.compile(r"(date|time|dt$|ts$|_at$|timestamp|day|month|year)", re.I)


SUPPORTED_DIALECTS = frozenset(_DIALECT_ALIASES)


def normalize_dialect(dialect: str) -> str:
    """把用户传入的方言名归一化为 sqlglot 认识的名字。"""
    key = (dialect or "mysql").strip().lower()
    return _DIALECT_ALIASES.get(key, key)


def is_supported_dialect(dialect: str) -> bool:
    return (dialect or "").strip().lower() in SUPPORTED_DIALECTS


@dataclass
class OutputField:
    """SQL 输出字段的结构信息。"""

    name: str
    expression_sql: str
    source_fields: list[str] = field(default_factory=list)
    is_star: bool = False
    has_explicit_alias: bool = True


@dataclass
class SqlParseResult:
    output_fields: list[OutputField] = field(default_factory=list)
    summary: SqlSummary | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class SqlParseError(ValueError):
    """SQL 不满足平台约束（如包含多条语句、非查询语句）。"""


# ---------------------------------------------------------------- 入口


def parse_sql(sql: str, dialect: str = "mysql") -> SqlParseResult:
    """解析单条 SELECT / WITH ... SELECT。

    语法错误或违反约束时返回 error 字段而非抛异常，调用方据此产出预览错误。
    """
    normalized = normalize_dialect(dialect)
    text = (sql or "").strip().rstrip(";").strip()
    if not text:
        return SqlParseResult(error="SQL 文件为空。")

    try:
        statements = sqlglot.parse(text, read=normalized)
    except SqlglotError as exc:
        return SqlParseResult(error=f"SQL 语法解析失败: {exc}")

    statements = [s for s in statements if s is not None]
    if not statements:
        return SqlParseResult(error="SQL 文件为空。")
    if len(statements) > 1:
        return SqlParseResult(
            error=f"仅支持单条 SELECT 查询，当前文件包含 {len(statements)} 条语句。"
        )

    root = statements[0]
    if not isinstance(root, (exp.Select, exp.Union, exp.Subquery, exp.With)):
        return SqlParseResult(
            error=f"仅支持 SELECT 或 WITH ... SELECT 查询，当前为 {type(root).__name__.upper()}。"
        )

    try:
        return _analyze(root, text, normalized)
    except Exception as exc:  # noqa: BLE001 - 结构解析失败降级为警告，不应中断批次
        return SqlParseResult(error=f"SQL 结构解析失败: {exc}")


def parse_output_fields(sql: str, dialect: str = "mysql") -> tuple[list[str], str | None]:
    """上传预览用：只取字段名，尽量轻量。"""
    result = parse_sql(sql, dialect)
    if not result.ok:
        return [], result.error
    return [f.name for f in result.output_fields], None


# ---------------------------------------------------------------- 结构分析


def _analyze(root: exp.Expression, sql_text: str, dialect: str) -> SqlParseResult:
    warnings: list[str] = []

    main_select = _final_select(root)
    if main_select is None:
        return SqlParseResult(error="未在 SQL 中找到最终的 SELECT 投影。")

    if isinstance(root, exp.Union):
        warnings.append("检测到 UNION，字段列表以最左侧查询为准。")
    if isinstance(root, exp.Subquery):
        warnings.append("检测到外层子查询包装。")

    # --- 输出字段 ---
    output_fields = _extract_output_fields(main_select, root, sql_text, dialect, warnings)

    # --- 来源表 ---
    source_tables = _extract_source_tables(root)

    # --- 关联 ---
    joins = _extract_joins(root)

    # --- 过滤条件 ---
    filters, time_filters, partition_filters = _extract_filters(main_select, root)

    # --- 聚合与窗口 ---
    group_by = _extract_group_by(main_select, dialect)
    agg_funcs = sorted({type(node).__name__.upper() for node in root.find_all(exp.AggFunc)})
    window_funcs = sorted(
        {
            type(node.this).__name__.upper() if isinstance(node.this, exp.Func) else str(node.this)
            for node in root.find_all(exp.Window)
            if node.this is not None
        }
    )
    has_aggregation = bool(group_by or agg_funcs or window_funcs)

    ctes = [
        cte.alias for cte in root.find_all(exp.CTE) if cte.alias
    ]
    subquery_count = len(list(root.find_all(exp.Subquery)))

    summary = SqlSummary(
        dialect=dialect,
        has_aggregation=has_aggregation,
        filters=filters,
        joins=joins,
        source_tables=source_tables,
        group_by=group_by,
        aggregate_functions=agg_funcs,
        window_functions=window_funcs,
        time_range_filters=time_filters,
        partition_filters=partition_filters,
        ctes=ctes,
        subqueries=subquery_count,
        parse_warnings=warnings,
    )
    return SqlParseResult(output_fields=output_fields, summary=summary)


def _final_select(root: exp.Expression) -> exp.Select | None:
    """取出决定最终输出字段的那个 SELECT。"""
    if isinstance(root, exp.Select):
        return root
    if isinstance(root, exp.Union):
        # 联合查询的输出列取最左侧分支。
        left = root.left
        if isinstance(left, exp.Subquery):
            left = left.this
        return left if isinstance(left, exp.Select) else None
    if isinstance(root, exp.Subquery):
        inner = root.this
        return inner if isinstance(inner, exp.Select) else None
    if isinstance(root, exp.With):
        return root.this if isinstance(root.this, exp.Select) else None
    return None


def _extract_output_fields(
    main_select: exp.Select,
    root: exp.Expression,
    sql_text: str,
    dialect: str,
    warnings: list[str],
) -> list[OutputField]:
    fields: list[OutputField] = []
    used_names: set[str] = set()

    for index, projection in enumerate(main_select.expressions):
        if isinstance(projection, exp.Star) or (
            isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star)
        ):
            warnings.append(
                "检测到 SELECT *，无法静态确定输出字段名，请改用显式字段或字段别名。"
            )
            fields.append(
                OutputField(name="*", expression_sql="*", is_star=True, has_explicit_alias=False)
            )
            continue

        if isinstance(projection, exp.Alias):
            name = projection.alias
            expression = projection.this
            explicit = True
        else:
            expression = projection
            name = _implicit_name(projection, index)
            explicit = False
            warnings.append(
                f"第 {index + 1} 个输出字段未使用别名，推断字段名为 '{name}'，"
                "请确认与数据文件表头一致。"
            )

        # 同名去重，避免结果里出现重复字段。
        if name in used_names:
            suffix = 2
            while f"{name}_{suffix}" in used_names:
                suffix += 1
            warnings.append(f"输出字段名 '{name}' 重复，已重命名为 '{name}_{suffix}'。")
            name = f"{name}_{suffix}"
        used_names.add(name)

        fields.append(
            OutputField(
                name=name,
                expression_sql=expression.sql(dialect=dialect),
                source_fields=_resolve_lineage(name, expression, sql_text, dialect, root),
                has_explicit_alias=explicit,
            )
        )
    return fields


def _implicit_name(projection: exp.Expression, index: int) -> str:
    """无别名时的字段名推断：裸列取列名，其余用表达式原文兜底。"""
    if isinstance(projection, exp.Column):
        return projection.name
    text = projection.sql()
    # 表达式原文通常不是合法列名，退化为序号占位。
    return text if len(text) <= 64 and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", text) else f"expr_{index + 1}"


def _extract_source_tables(root: exp.Expression) -> list[SourceTableInfo]:
    """提取真实来源表。

    CTE 只是查询内部的中间结果，不是数据来源，因此同名且无库/模式限定的引用要排除；
    CTE 定义体内部的真实表（如 dwd.order）仍会被收集。
    """
    cte_names = {cte.alias for cte in root.find_all(exp.CTE) if cte.alias}

    tables: list[SourceTableInfo] = []
    seen: set[str] = set()
    for table in root.find_all(exp.Table):
        name = table.name
        if not name:
            continue
        if name in cte_names and not table.db and not table.catalog:
            continue  # 指向 CTE 的引用，非真实表

        schema = table.db or None
        catalog = table.catalog or None
        qualified = ".".join(part for part in (catalog, schema, name) if part)
        # 同一张表可能被多个别名引用，按「限定名 + 别名」去重。
        key = f"{qualified}|{table.alias or ''}"
        if key in seen:
            continue
        seen.add(key)
        tables.append(
            SourceTableInfo(
                catalog=catalog,
                schema=schema,
                table=name,
                alias=table.alias or None,
                qualified_name=qualified,
            )
        )
    return tables


def _extract_joins(root: exp.Expression) -> list[JoinInfo]:
    joins: list[JoinInfo] = []
    for join in root.find_all(exp.Join):
        condition = join.args.get("on")
        join_type = _join_type(join)
        left_field, right_field = _split_condition(condition)
        joins.append(
            JoinInfo(
                type=join_type,
                left_field=left_field,
                right_field=right_field,
                condition=condition.sql() if condition is not None else None,
            )
        )
    return joins


def _join_type(join: exp.Join) -> str:
    """归一化 JOIN 类型为 LEFT / LEFT OUTER / INNER / CROSS / SEMI 等。"""
    side = (join.side or "").upper()
    kind = (join.kind or "").upper()
    if kind == "CROSS":
        return "CROSS"
    if side:
        return f"{side} OUTER" if kind == "OUTER" else side
    return kind or "INNER"


def _split_condition(condition: exp.Expression | None) -> tuple[str | None, str | None]:
    """从 ON 条件里取第一组等值关联字段。"""
    if condition is None:
        return None, None
    for eq in condition.find_all(exp.EQ):
        left, right = eq.left, eq.right
        if isinstance(left, exp.Column) and isinstance(right, exp.Column):
            return left.sql(), right.sql()
    return None, None


def _extract_filters(
    main_select: exp.Select, root: exp.Expression
) -> tuple[list[str], list[str], list[str]]:
    """收集过滤条件。

    filters 只取主查询自身的 WHERE / HAVING（对应方案 §5 的示例）；
    时间范围与分区条件则扫描整棵语法树——分区裁剪通常写在 CTE 或子查询里。
    """
    filters: list[str] = []
    for key in ("where", "having"):
        node = main_select.args.get(key)
        if isinstance(node, (exp.Where, exp.Having)) and node.this is not None:
            filters.append(node.this.sql())

    time_filters: list[str] = []
    partition_filters: list[str] = []
    seen: set[str] = set()
    for where in root.find_all(exp.Where):
        if where.this is None:
            continue
        for part in re.split(r"\s+AND\s+", where.this.sql(), flags=re.I):
            part = part.strip().strip("()")
            if not part or part in seen:
                continue
            seen.add(part)
            columns = _column_names_in(part)
            if any(_PARTITION_HINTS.search(col) for col in columns):
                partition_filters.append(part)
            elif any(_TIME_HINTS.search(col) for col in columns):
                time_filters.append(part)
    return filters, time_filters, partition_filters


_IDENTIFIER = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\b")
_SQL_KEYWORDS = {
    "and", "or", "not", "in", "is", "null", "between", "like", "case", "when",
    "then", "else", "end", "true", "false", "date", "interval", "current_date",
}


def _column_names_in(fragment: str) -> list[str]:
    """从条件片段中取出疑似列名。

    片段是 WHERE 原文的子串（已按 AND 切分），不一定能被 sqlglot 单独解析，
    因此优先走 AST，失败则退回标识符正则。
    """
    try:
        return [c.name for c in sqlglot.parse_one(fragment).find_all(exp.Column)]
    except Exception:  # noqa: BLE001 - 片段解析失败是常态，正则兜底即可
        # 先剔除字符串字面量，避免 'active' 之类的内容被当成列名。
        stripped = re.sub(r"'[^']*'|\"[^\"]*\"", " ", fragment)
        return [t for t in _IDENTIFIER.findall(stripped) if t.lower() not in _SQL_KEYWORDS]


def _extract_group_by(main_select: exp.Select, dialect: str) -> list[str]:
    group = main_select.args.get("group")
    if not isinstance(group, exp.Group):
        return []
    return [e.sql(dialect=dialect) for e in group.expressions]


# ---------------------------------------------------------------- 血缘


def _resolve_lineage(
    field_name: str,
    expression: exp.Expression,
    sql_text: str,
    dialect: str,
    root: exp.Expression,
) -> list[str]:
    """解析输出字段的上游字段，优先用 sqlglot 血缘图，失败则退回直接列引用。"""
    resolved = _lineage_via_graph(field_name, sql_text, dialect)
    if resolved:
        return resolved
    return _lineage_via_columns(expression, root)


def _lineage_via_graph(field_name: str, sql_text: str, dialect: str) -> list[str]:
    """用 sqlglot.lineage 穿透 CTE 与子查询，得到上游字段限定名。

    血缘图中只有叶子节点的 source 才是真实表；中间节点的 source 是 Select
    （CTE 或子查询体），据此天然跳过，无需额外判断 CTE 名。
    叶子节点的 name 形如 `o.customer_id`（用别名限定），真实表名取自 source。
    """
    if field_name.startswith("expr_"):
        return []
    try:
        node = lineage(field_name, sql_text, dialect=dialect)
    except Exception:  # noqa: BLE001 - 血缘解析是增强信息，失败不应影响主流程
        return []

    sources: list[str] = []
    seen: set[str] = set()
    for sub in node.walk():
        if sub is node:
            continue
        table = sub.source
        if not isinstance(table, exp.Table) or not table.name:
            continue
        column_name = sub.name.rsplit(".", 1)[-1].strip()
        if not column_name or column_name == "*":
            continue
        qualified = ".".join(
            part for part in (table.catalog or None, table.db or None, table.name, column_name) if part
        )
        if qualified not in seen:
            seen.add(qualified)
            sources.append(qualified)
    return sources


def _lineage_via_columns(expression: exp.Expression, root: exp.Expression) -> list[str]:
    """血缘兜底：把表达式里的列引用按别名映射回真实表名。"""
    alias_map: dict[str, str] = {}
    for table in root.find_all(exp.Table):
        if not table.name:
            continue
        qualified = ".".join(part for part in (table.db or None, table.name) if part)
        alias_map[table.alias or table.name] = qualified

    sources: list[str] = []
    seen: set[str] = set()
    for column in expression.find_all(exp.Column):
        table_ref = column.table
        column_name = column.name
        if table_ref and table_ref in alias_map:
            qualified = f"{alias_map[table_ref]}.{column_name}"
        elif not table_ref:
            qualified = column_name  # 无法确定归属表时只保留列名
        else:
            qualified = f"{table_ref}.{column_name}"
        if qualified not in seen:
            seen.add(qualified)
            sources.append(qualified)
    return sources
