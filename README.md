# 数据资源描述解析平台

把 SQL 文件及其对应真实数据解析为固定结构的数据资源描述。本期实现
[技术实现方案](数据资源描述解析平台技术实现方案.md) 中的「上传数据、执行解析、读取解析结果」三个环节。

后端为 FastAPI 服务，并内置一个无需 Node 构建的单页工作台；同时保留
`/docs` 与 curl 验收方式。

在原有确定性解析流水线之上，项目新增了增强型 Agent 运行模式：Agent 负责
选择语义提供方、编排 SQL 与数据解析、记录执行事件并汇总人工审查项。字段集合、
统计结果和最终 JSON 仍由本地确定性工具产出。

## 三层解析

| 层 | 做什么 | 是否调用模型 |
| --- | --- | --- |
| AST 层 | SQLGlot 解析输出字段、来源表、关联、过滤、聚合、CTE 血缘 | 否 |
| 数据画像层 | Pandas 统计物理类型、空值率、分布、枚举、标识字段、格式模式 | 否 |
| 语义层 | 补全显示名、业务类型、标签、单位 | 是（可降级为规则） |

只有语义层可调用小模型，且只接收字段名、SQL 表达式、来源表、类型、统计摘要、
**脱敏样例**与人工描述；原始明细不外出。

## 目录结构

```
app/
  main.py                     # 应用入口、CORS、异常兜底
  config.py                   # 全部配置项，环境变量可覆盖
  schemas.py                  # 固定 JSON 结构（前端唯一契约）与接口模型
  storage.py                  # 批次落盘：batch.json / uploads / results
  api/batches.py              # 5 个接口
  static/                     # 单页前端工作台
    index.html
    styles.css
    app.js
  services/
    pairing.py                # 按主文件名自动配对
    sql_parser.py             # AST 层
    profiler.py               # 数据画像层
    masking.py                # 样例脱敏
    pipeline.py               # 三层合并，组装固定 JSON
    semantic/
      base.py                 # SemanticProvider 抽象与模型入参边界
      rule_based.py           # RuleBasedProvider（离线）
      openai_compatible.py    # OpenAICompatibleProvider（小模型 API）
    agent/
      orchestrator.py         # 增强型 Agent 状态机与执行编排
      review.py               # 低置信度、字段错配等审查项
samples/                      # 演示数据与生成脚本
tests/                        # pytest 用例 + acceptance.sh 端到端验收
```

## 环境准备

依赖装在项目内的 `.venv`，与系统 Python 及其他项目隔离。

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt      # Windows
# source .venv/bin/activate && pip install -r requirements.txt   # Linux/macOS
```

## 启动

```bash
.venv/Scripts/python.exe -m uvicorn app.main:app --reload
```

- Web 工作台：<http://127.0.0.1:8000/>
- 接口文档：<http://127.0.0.1:8000/docs>
- 健康检查：<http://127.0.0.1:8000/health>

工作台覆盖文件拖放、批次载入、配对修正、资源/字段描述、标准解析、增强 Agent、
实时状态、执行事件、待确认项和结构化结果查看。

## 接口

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| `POST` | `/api/v1/batches` | 上传 SQL 与数据文件，自动配对并返回字段预览 |
| `POST` | `/api/v1/batches/{batchId}/start` | 提交 SQL/字段描述并启动完整解析 |
| `GET` | `/api/v1/batches/{batchId}` | 查询解析进度和每份 SQL 的解析状态 |
| `GET` | `/api/v1/batches/{batchId}/results` | 获取全部固定 JSON 解析结果 |
| `GET` | `/api/v1/batches/{batchId}/results/{resourceCode}` | 获取指定 SQL 的固定 JSON |
| `POST` | `/api/v1/batches/{batchId}/agent-runs` | 启动增强型 Agent |
| `GET` | `/api/v1/batches/{batchId}/agent-runs/latest` | 查询最近一次 Agent 状态和审查项 |
| `GET` | `/api/v1/batches/{batchId}/agent-runs/latest/events` | 查询最近一次 Agent 执行事件 |

任务状态：`UPLOADED` → `PREVIEW_READY` → `PARSING` → `COMPLETED` / `FAILED`。
前端在 `PREVIEW_READY` 展示配对与描述区，`COMPLETED` 后读结果。

### 上传

`multipart/form-data`，字段为 `sql_dialect`、`sql_files[]`、`data_files[]`、
可选的 `mappings`（JSON 字符串，用于修正自动配对）。

```bash
curl -X POST http://127.0.0.1:8000/api/v1/batches \
  -F "sql_dialect=mysql" \
  -F "sql_files=@samples/customer_profile.sql" \
  -F "data_files=@samples/customer_profile.csv"
```

返回 `batch_id`、自动配对结果与每份 SQL 的输出字段。SQL 语法错误会在这一步
以 `parse_error` 暴露，不会等到解析阶段。

### 启动解析

```bash
curl -X POST http://127.0.0.1:8000/api/v1/batches/{batchId}/start \
  -H 'Content-Type: application/json' \
  -d '{"resource_descriptions":{"customer_profile.sql":"客户画像汇总。"},
       "field_descriptions":{"customer_profile.sql":{"customer_id":"客户唯一编号。"}}}'
```

> Windows 提示：`curl` 是原生程序，命令行里的非 ASCII 字符会被系统码页破坏。
> 含中文的请求体请先写成 UTF-8 文件再 `--data-binary @body.json` 发送。

### 启动增强型 Agent

先按下一节配置云端 API。增强型 Agent 默认允许在 API 不可用时降级为规则实现；
如需确保结果一定来自云端模型，将 `AGENT_REQUIRE_API=true`，或在请求中传入
`require_api: true`。

```bash
curl -X POST http://127.0.0.1:8000/api/v1/batches/{batchId}/agent-runs \
  -H 'Content-Type: application/json' \
  -d '{"resource_descriptions":{"customer_profile.sql":"客户画像汇总。"},
       "field_descriptions":{"customer_profile.sql":{"customer_id":"客户唯一编号。"}},
       "require_api":true}'
```

```bash
curl http://127.0.0.1:8000/api/v1/batches/{batchId}/agent-runs/latest
curl http://127.0.0.1:8000/api/v1/batches/{batchId}/agent-runs/latest/events
```

Agent 状态包括 `QUEUED`、`RUNNING`、`REVIEW_REQUIRED`、`COMPLETED` 和
`FAILED`。`REVIEW_REQUIRED` 表示已产出结果，但存在低置信度字段、字段错配、
解析警告或其他需要人工关注的问题。

## 配置

复制 `.env.example` 为 `.env`。关键项：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `SEMANTIC_API_BASE` / `SEMANTIC_API_KEY` / `SEMANTIC_MODEL` | 空 | 三项齐全才启用小模型 |
| `SEMANTIC_ENABLE_API` | `true` | 置 `false` 强制走规则实现 |
| `SEMANTIC_CONFIDENCE_THRESHOLD` | `0.85` | 低于该值或存在多候选时字段置为待确认 |
| `AGENT_REQUIRE_API` | `false` | 为 `true` 时 Agent 必须使用云端 API，否则失败 |
| `AGENT_MAX_REVIEW_ITEMS` | `500` | 单次 Agent 运行最多保留的审查项 |
| `DATA_DIR` | `./data/batches` | 批次落盘根目录 |
| `PROFILE_MAX_ROWS` | `100000` | 画像统计最多读取行数 |
| `PROFILE_MAX_EXAMPLES` | `20` | 脱敏样例最多保留条数 |
| `SAMPLE_SIZE_WARNING_THRESHOLD` | `1000` | 少于该行数标记 `sample_size_warning` |

未配置小模型时自动使用 `RuleBasedProvider`，可完全离线运行。

## 语义提供方

`SemanticProvider` 抽象接口有两个实现，由配置自动选择：

- **`RuleBasedProvider`**：字段名词典 + 类型/表达式规则。置信度按
  「整体名精确匹配 1.0 > 词元匹配 0.88 > 子串匹配 0.72」递减，
  两条规则分差小于 0.08 时判为多候选，不武断取名。
- **`OpenAICompatibleProvider`**：调用兼容 OpenAI 协议的 `/chat/completions`，
  要求返回固定 JSON。契约不满足、网络异常、5xx 均降级为「无补全」，
  4xx 不重试；同批次内相同字段只调用一次。

### 合并优先级

实现方案 §4.4 的约束集中在 `pipeline._build_fields`：

1. 人工填写的资源描述与字段属性描述**永远优先**，`description_source` 标记来源；
2. 模型只补充缺失的显示名、标签、单位，不覆盖人工描述；
3. 字段集合**完全由 SQL 输出决定**（`SELECT *` 时退回数据文件表头），模型无法增删；
4. 人工描述已明确含义时，歧义视为消解，不再标记待确认。

## 固定 JSON 结构

每个 SQL 产出 `results/{resource_code}.json`，结构见方案 §5，由
`app/schemas.py` 的 `ResourceDescriptor` 定义。

在方案原文之外，为满足 §4.1/§4.2 的其余要求，结果中**新增了若干字段**
（均不改变原有字段的含义，前端可按原文忽略）：

- `sql_summary` 增补 `source_tables`（含库/模式/表/别名）、`group_by`、
  `aggregate_functions`、`window_functions`、`time_range_filters`、
  `partition_filters`、`ctes`、`subqueries`、`parse_warnings`；
- `profile` 增补 `data_file`、`column_count`、`unmatched_columns`、`missing_columns`；
- `fields[]` 增补 `semantic_status`（`CONFIRMED` / `PENDING`）、`business_type`、
  `tags`、`unit`、`description_source`；
- 顶层增补 `sql_file`、`generated_at`、`semantic_provider`。

`semantic_status` 承载方案 §4.3「置信度低于 0.85 或有多个候选时保留待确认状态」
的落地结果。

## 演示数据

```bash
.venv/Scripts/python.exe samples/gen_sample_data.py
```

只用标准库，固定随机种子，重复运行结果一致。产出两份可直接上传的资源：

| 资源 | 行数 | 覆盖的结构 |
| --- | --- | --- |
| `customer_profile` | 1232 | CTE + LEFT JOIN + GROUP BY + 分区过滤，含空值、枚举、格式编码 |
| `order_daily` | 1930 | 条件聚合 + 多列 GROUP BY + 日期字段 |

两份都满足方案 §3.3 的「每份资源不少于 1000 行」。

## 测试

```bash
.venv/Scripts/python.exe -m pip install -r requirements-dev.txt
.venv/Scripts/python.exe -m pytest tests/ -v          # 43 个用例
```

对运行中的服务做一次完整验收：

```bash
.venv/Scripts/python.exe -m uvicorn app.main:app --port 8021
bash tests/acceptance.sh http://127.0.0.1:8021
```

验收脚本覆盖：上传 → 预览 → 填描述 → 轮询 → 读全部/单份结果 → 脱敏检查 → 异常分支。

## 运行产物

```
data/batches/{batch_id}/
    batch.json             # 状态、配对、描述
    uploads/               # 上传原件
    results/{code}.json    # 固定 JSON 结果
```

`data/batches/` 已在 `.gitignore` 中排除。
