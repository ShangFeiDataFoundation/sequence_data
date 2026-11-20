# 商飞统一资源解析 Agent

项目从文件夹递归扫描数据文件，或连接 MySQL 并选择一个或多个数据表。Agent 自动识别
时序数据、普通结构化表和表结构定义，为每个文件、每张数据库表分别生成一份统一 JSON。

```text
文件夹 / MySQL 数据表
→ 递归扫描和自动分类
→ 时序统计特征或结构化字段画像
→ 每个资源单独调用 LLM
→ 一个资源生成一份 unified_resource_v1 JSON
→ 四步工作台查看解析、语义、趋势与导出结果
```

项目不使用 LSTM。时序分析由确定性统计计算完成，LLM 只负责在统计证据基础上生成
业务语义属性和摘要。

## 启动

```powershell
cd D:\program_code\shangfei
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8031
```

访问：

- 工作台：<http://127.0.0.1:8031/>
- 健康检查：<http://127.0.0.1:8031/health>
- API 文档：<http://127.0.0.1:8031/docs>

如果依赖尚未安装：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 页面操作

前端采用四步流程：数据接入、解析概览、语义分析、可视化。

### 1. 数据接入

#### 文件夹输入

1. 选择“选择文件夹”。
2. 选择一个目录，浏览器会递归扫描其中的文件。
3. 支持的格式为 CSV、TSV、TXT、Parquet、XLSX、XLS、JSON、JSONL、SQL 和 DDL。
4. 不支持的格式会忽略；允许任意文件组合，不要求同时存在时序文件和表结构文件。
5. 点击“启动解析”。

浏览器不会提供本地文件夹的绝对路径，只会上传文件内容及其相对路径。

#### MySQL 输入

1. 选择“连接 MySQL”。
2. 填写主机、端口、数据库、用户名和密码。
3. 点击“连接并读取表”，勾选一个或多个数据表。
4. 为每张表选择时间字段；无时间字段的表按普通结构化表解析。
5. 可设置公共时间范围，最大读取行数固定不超过 500。
6. 点击“启动解析”。

密码只保存在后端进程内存中，连接空闲 30 分钟后失效，进程重启后立即清除。密码不会
写入运行结果、日志或前端页面。时间范围按数据库中的本地日期时间值进行比较。

最近运行记录显示在数据接入页下方，可以恢复已完成结果，也可以只隐藏记录而不删除
原始文件和解析结果。

### 2. 解析概览

左侧列出本次运行的全部资源，默认优先选择时序资源。右侧展示识别结论、字段结构、
时间轴、数据质量和完整的 `unified_resource_v1` JSON。

### 3. 语义分析

左侧切换资源，右侧展示模型生成的摘要、结构化属性、置信度、证据引用和建议。模型
在解析阶段随资源一起执行，语义分析页不重复发起模型请求。

### 4. 可视化

页面上方显示批量资源汇总，下方显示当前资源的指标趋势曲线。可以切换实体与指标序列，
导出汇总 CSV、当前资源 JSON 或本次运行的全部结果 JSON。

## 统一 JSON

每个文件或数据库表对应一份 `unified_resource_v1` JSON，固定包含：

```text
schema_version
report_id
resource_id
resource_type
resource_name
display_name
source
classification
schema_profile
time_axis
entity_fields
metric_fields
quality
statistics
attributes
summary
overall_status
recommendations
relationships
processing
generated_at
```

- `classification.data_kind`：`time_series`、`structured_table` 或 `schema`。
- `schema_profile`：行数、字段数、字段类型和主键候选。
- `time_axis`：时间字段、起止时间、粒度和数据质量。
- `statistics`：字段画像、趋势、周期、异常、变化点和相关性等确定性证据。
- `attributes`：当前资源单独调用 LLM 后生成的结构化业务属性，最多保留 5 条；
  超出部分会合并为一条“其他补充观察”。
- `statistics.chart_series`：最多保留 8 条图表序列，每条序列最多 120 个采样点；
  用于前端趋势图，不包含完整原始数据行。

每份资源的完整 JSON 同时保存在：

```text
data/agent_runs/{run_id}/resources/{resource_id}.json
```

## 接口

### 文件夹

```text
POST /api/v1/agent/runs
```

请求类型为 `multipart/form-data`：

```text
files            文件内容，可重复
relative_paths   与 files 顺序一致的文件夹相对路径，可重复
sql_dialect      SQL DDL 方言，默认 mysql
```

### MySQL

```text
POST /api/v1/database/connections
POST /api/v1/agent/runs/from-database
```

连接请求示例：

```json
{
  "host": "127.0.0.1",
  "port": 3306,
  "username": "reader",
  "password": "***",
  "database": "aircraft_monitor"
}
```

启动数据库解析示例：

```json
{
  "connection_id": "dbc_xxx",
  "max_rows": 500,
  "tables": [
    {
      "schema_name": "aircraft_monitor",
      "table_name": "engine_readings",
      "time_column": "timestamp",
      "time_start": "2026-03-01T00:00:00+08:00",
      "time_end": "2026-03-05T00:00:00+08:00"
    }
  ]
}
```

### 结果查询

```text
GET  /api/v1/agent/runs
GET /api/v1/agent/runs/{run_id}
GET /api/v1/agent/runs/{run_id}/result
GET /api/v1/agent/runs/{run_id}/resources/{resource_id}
POST /api/v1/agent/runs/{run_id}/hide
```

已完成的结果可以通过下列地址重新打开：

```text
http://127.0.0.1:8031/?run_id={run_id}
```

## LLM 配置

LLM 使用 OpenAI 兼容的 `/chat/completions` 接口。每个资源单独调用一次。

页面右上角的“模型设置”可以测试并保存当天使用的 API 地址、模型和密钥。密钥在
Windows 下使用当前用户 DPAPI 加密，翌日自动失效；密钥不会进入资源 JSON、日志、
历史记录或浏览器存储。

```dotenv
LLM_API_BASE=https://api.example.com/v1
LLM_API_KEY=your-api-key
LLM_MODEL=your-model
LLM_ENABLE_API=true
LLM_REQUIRED=true
LLM_TIMEOUT_SECONDS=60
```

API Key 也可以放在本地且已被 Git 忽略的 `app/local_secrets.py` 中。未配置且
`LLM_REQUIRED=true` 时，Agent 会明确失败。

模型设置接口：

```text
GET  /api/v1/settings/model
POST /api/v1/settings/model/test
POST /api/v1/settings/model/save
```

## 样例

```powershell
.\.venv\Scripts\python.exe samples\gen_comac_demo_samples.py
```

`samples/comac_demo/` 中包含 5 个合成样例文件。所有飞机编号和传感器数据均为测试数据。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check app tests
node --check app\static\app.js
```
