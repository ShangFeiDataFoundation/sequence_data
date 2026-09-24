#!/usr/bin/env bash
# 对运行中的服务做一次完整验收（方案 §2 的三个环节）。
#
#   .venv/Scripts/python.exe -m uvicorn app.main:app --port 8021
#   bash tests/acceptance.sh http://127.0.0.1:8021
#
# 依赖 curl，以及用于格式化输出的项目虚拟环境 python。

set -euo pipefail

# 让 Python 以 UTF-8 输出，避免中文在 Windows 控制台变成乱码。
export PYTHONIOENCODING=utf-8

BASE="${1:-http://127.0.0.1:8021}"
API="$BASE/api/v1/batches"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"   # 让 curl 的 -F 使用相对路径，避免 MSYS 与原生路径混用

PY="$ROOT/.venv/Scripts/python.exe"
# 原生 Windows Python 读不懂 MSYS 的 /d/... 路径，交给 cygpath 转换。
win() { cygpath -w "$1"; }

TMP="$ROOT/.tmp-acceptance"
rm -rf "$TMP" && mkdir -p "$TMP"
TMPW="$(win "$TMP")"
trap 'rm -rf "$TMP"' EXIT

hr() { printf '\n\033[36m=== %s ===\033[0m\n' "$1"; }

hr "0. 健康检查"
curl -sS "$BASE/health"; echo

hr "1. 上传 SQL 与数据文件（POST /api/v1/batches）"
curl -sS -X POST "$API" \
  -F "sql_dialect=mysql" \
  -F "sql_files=@samples/customer_profile.sql" \
  -F "sql_files=@samples/order_daily.sql" \
  -F "data_files=@samples/customer_profile.csv" \
  -F "data_files=@samples/order_daily.csv" \
  -o "$TMP/created.json" -w "HTTP %{http_code}\n"

BATCH=$("$PY" -c "import json;print(json.load(open(r'$TMPW\created.json',encoding='utf-8'))['batch_id'])")
"$PY" -c "
import json
d = json.load(open(r'$TMPW\created.json', encoding='utf-8'))
print('batch_id  :', d['batch_id'])
print('status    :', d['status'])
for r in d['resources']:
    print(f\"  {r['resource_code']:18} <- {r['data_file']:22} [{r['matched_by']}] {len(r['output_fields'])} 字段\")
    print(f\"      {r['output_fields']}\")
print('未配对 SQL :', d['unmatched_sql_files'])
print('未配对数据 :', d['unmatched_data_files'])
"

hr "2. 填写描述并启动解析（POST /api/v1/batches/{id}/start）"
# 请求体先由 Python 写成 UTF-8 文件再发送：Windows 下 curl 是原生程序，
# 命令行参数经系统码页转换会破坏非 ASCII 字符，--data-binary @文件 可绕开。
"$PY" -c "
import json, pathlib
body = {
    'resource_descriptions': {
        'customer_profile.sql': '面向营销分析的客户基础信息与交易行为汇总。',
        'order_daily.sql': '按日期与渠道统计的订单量与金额日报。',
    },
    'field_descriptions': {
        'customer_profile.sql': {
            'customer_id': '客户的业务唯一编号。',
            'total_amount': '客户累计支付金额，单位为元。',
            'cust_no': '客户在业务系统中的编码。',
        },
        'order_daily.sql': {
            'refund_count': '当日该渠道的退款订单数。',
        },
    },
}
pathlib.Path(r'$TMPW\start-request.json').write_text(
    json.dumps(body, ensure_ascii=False), encoding='utf-8'
)
"
curl -sS -X POST "$API/$BATCH/start" \
  -H 'Content-Type: application/json; charset=utf-8' \
  --data-binary "@$TMP/start-request.json" \
  -o "$TMP/started.json" -w "HTTP %{http_code}\n"
"$PY" -c "
import json; d = json.load(open(r'$TMPW\started.json', encoding='utf-8'))
print('status:', d['status'], '| 资源数:', d['total_resources'])
"

hr "3. 轮询任务状态（GET /api/v1/batches/{id}）"
for _ in $(seq 1 30); do
  curl -sS "$API/$BATCH" -o "$TMP/status.json"
  STATUS=$("$PY" -c "import json;print(json.load(open(r'$TMPW\status.json',encoding='utf-8'))['status'])")
  printf '  status=%s\n' "$STATUS"
  [ "$STATUS" = "COMPLETED" ] || [ "$STATUS" = "FAILED" ] && break
  sleep 1
done
"$PY" -c "
import json; d = json.load(open(r'$TMPW\status.json', encoding='utf-8'))
print(f\"  完成 {d['completed_resources']}/{d['total_resources']}，失败 {d['failed_resources']}\")
for r in d['resources']:
    print(f\"    {r['resource_code']:18} {r['status']:10} 字段 {r['field_count']}  {r['error'] or ''}\")
"

hr "4. 读取全部结果（GET /api/v1/batches/{id}/results）"
curl -sS "$API/$BATCH/results" -o "$TMP/results.json" -w "HTTP %{http_code}\n"
"$PY" -c "
import json
d = json.load(open(r'$TMPW\results.json', encoding='utf-8'))
for r in d['results']:
    p = r['profile']
    print(f\"--- {r['resource_code']} | {r['name']} | provider={r['semantic_provider']}\")
    print(f\"    来源表 : {r['source_tables']}\")
    print(f\"    标签   : {r['tags']}\")
    print(f\"    描述   : {r['description']}\")
    print(f\"    画像   : {p['row_count']} 行 / 采样 {p['sampled_row_count']} / 小样本告警 {p['sample_size_warning']}\")
    s = r['sql_summary']
    print(f\"    SQL    : 聚合={s['has_aggregation']} 关联={[j['type'] for j in s['joins']]} CTE={s['ctes']}\")
    print(f\"             过滤={s['filters']}\")
    print(f\"             分区={s['partition_filters']}\")
    print(f\"    {'字段':16}{'显示名':14}{'类型':10}{'置信':>6}  {'状态':9} 血缘\")
    for f in r['fields']:
        conf = f['semantic_confidence']
        conf = f'{conf:.2f}' if conf is not None else '  - '
        src = ','.join(f['source_fields']) or '-'
        print(f\"    {f['name']:16}{str(f['display_name']):14}{str(f['data_type']):10}{conf:>6}  {f['semantic_status']:9} {src}\")
    print()
"

hr "5. 读取单份结果（GET /api/v1/batches/{id}/results/{code}）"
curl -sS "$API/$BATCH/results/customer_profile" -o "$TMP/one.json" -w "HTTP %{http_code}\n"

hr "6. 结果中字段样例的脱敏检查"
"$PY" -c "
import json
d = json.load(open(r'$TMPW\one.json', encoding='utf-8'))
for f in d['fields']:
    if f['examples']:
        print(f\"  {f['name']:16} {f['examples'][:4]}\")
"

hr "7. 异常分支"
printf '  不存在的批次   : HTTP %s\n' "$(curl -sS -o /dev/null -w '%{http_code}' "$API/bat_nope")"
printf '  非法方言       : HTTP %s\n' "$(curl -sS -o /dev/null -w '%{http_code}' -X POST "$API" -F 'sql_dialect=oracle9i' -F "sql_files=@samples/customer_profile.sql" -F "data_files=@samples/customer_profile.csv")"

hr "验收完成"
printf 'batch_id = %s\n' "$BATCH"
printf '落盘目录 = %s\n' "$ROOT/data/batches/$BATCH"
