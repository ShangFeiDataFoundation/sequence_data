"""生成演示用的合成数据（方案 §3.3：每份资源不少于 1000 行）。

只用标准库，不依赖项目虚拟环境，可独立运行：

    python samples/gen_sample_data.py

产出（与 SQL 输出字段一一对应，可直接上传）：

    customer_profile.sql + customer_profile.csv    客户画像汇总，1200 行
    order_daily.sql      + order_daily.csv         订单日汇总，2920 行

数据刻意保留少量空值、枚举字段与固定格式编码，用于验证画像与标签效果。
固定随机种子，重复运行结果一致。
"""

from __future__ import annotations

import csv
import random
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

SEED = 20240914
OUTPUT_DIR = Path(__file__).resolve().parent

# 约 12% 客户为 inactive，会被 SQL 过滤掉，因此留出余量确保结果仍远超 1000 行。
CUSTOMER_COUNT = 1400
ORDER_START = date(2023, 1, 1)
ORDER_END = date(2024, 12, 31)

SURNAMES = "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜"
GIVEN_NAMES = ["伟", "芳", "娜", "秀英", "敏", "静", "丽", "强", "磊", "洋",
               "艳", "勇", "军", "杰", "娟", "涛", "明", "超", "秀兰", "霞",
               "平", "刚", "桂英", "文", "辉", "玲", "健", "帅", "婷", "浩"]

CITIES = ["北京", "上海", "广州", "深圳", "杭州", "成都", "武汉", "西安",
          "南京", "重庆", "苏州", "天津", "长沙", "青岛", "宁波"]
CITY_WEIGHTS = [14, 13, 11, 10, 9, 8, 7, 6, 6, 5, 4, 3, 2, 3, 2]

CHANNELS = ["app", "web", "wechat", "offline"]
CHANNEL_WEIGHTS = [45, 25, 22, 8]

PRODUCT_COUNT = 200


# ---------------------------------------------------------------- SQL

CUSTOMER_PROFILE_SQL = """-- customer_profile.sql
-- 客户画像汇总：客户基础信息 + 已支付订单的消费汇总
WITH paid_order AS (
    SELECT
        o.customer_id,
        o.order_id,
        o.amount,
        o.created_at
    FROM dwd.order o
    WHERE o.status = 'paid'
      AND o.dt >= '2023-01-01'
      AND o.dt < '2025-01-01'
)
SELECT
    c.id              AS customer_id,
    c.cust_no         AS cust_no,
    c.customer_name   AS customer_name,
    c.city            AS city,
    c.gender          AS gender,
    c.age             AS age,
    COUNT(p.order_id) AS order_count,
    SUM(p.amount)     AS total_amount,
    MAX(p.created_at) AS last_order_at
FROM dwd.customer c
LEFT JOIN paid_order p
       ON c.id = p.customer_id
WHERE c.status = 'active'
GROUP BY
    c.id,
    c.cust_no,
    c.customer_name,
    c.city,
    c.gender,
    c.age
;
"""

ORDER_DAILY_SQL = """-- order_daily.sql
-- 订单日汇总：按日期与渠道统计下单量与金额
SELECT
    o.dt                                                   AS stat_date,
    o.channel                                              AS channel,
    COUNT(o.order_id)                                      AS order_count,
    SUM(o.amount)                                          AS total_amount,
    AVG(o.amount)                                          AS avg_amount,
    SUM(CASE WHEN o.status = 'refunded' THEN 1 ELSE 0 END) AS refund_count
FROM dwd.order o
WHERE o.dt >= '2023-01-01'
  AND o.dt < '2025-01-01'
GROUP BY
    o.dt,
    o.channel
;
"""


# ---------------------------------------------------------------- 生成


def build_customers(rng: random.Random) -> list[dict]:
    """生成客户基础信息。id 为自增主键，cust_no 为带格式的业务编码。"""
    customers = []
    for index in range(1, CUSTOMER_COUNT + 1):
        # 约 8% 的客户没有填写年龄，用于验证空值率统计。
        age = None if rng.random() < 0.08 else rng.randint(18, 75)
        customers.append(
            {
                "id": index,
                "cust_no": f"C{index:07d}",
                "customer_name": rng.choice(SURNAMES) + rng.choice(GIVEN_NAMES),
                "city": rng.choices(CITIES, weights=CITY_WEIGHTS, k=1)[0],
                "gender": rng.choices(["M", "F"], weights=[52, 48], k=1)[0],
                "age": age,
                # 约 12% 为非活跃客户，用于验证 SQL 过滤后的行数变化。
                "status": "active" if rng.random() > 0.12 else "inactive",
                "register_date": (ORDER_START - timedelta(days=rng.randint(1, 900))).isoformat(),
            }
        )
    return customers


def build_orders(rng: random.Random, customers: list[dict]) -> list[dict]:
    """生成订单明细。活跃客户下单更频繁，金额服从长尾分布。"""
    active = [c for c in customers if c["status"] == "active"]
    total_days = (ORDER_END - ORDER_START).days + 1
    orders: list[dict] = []
    order_id = 0

    for customer in active:
        # 复购次数：多数客户下单较少，少数客户贡献大量订单。
        order_count = rng.choices([0, 1, 2, 3, 5, 9, 15], weights=[12, 26, 22, 15, 12, 8, 5], k=1)[0]
        for _ in range(order_count):
            order_id += 1
            order_day = ORDER_START + timedelta(days=rng.randint(0, total_days - 1))
            amount = round(rng.lognormvariate(4.2, 1.1), 2)
            # 少量订单退款，用于验证条件聚合。
            status = "refunded" if rng.random() < 0.04 else "paid"
            created = datetime(
                order_day.year, order_day.month, order_day.day,
                rng.randint(0, 23), rng.randint(0, 59), rng.randint(0, 59),
            )
            orders.append(
                {
                    "order_id": f"SO{order_id:09d}",
                    "customer_id": customer["id"],
                    "product_id": f"P{rng.randint(1, PRODUCT_COUNT):05d}",
                    "channel": rng.choices(CHANNELS, weights=CHANNEL_WEIGHTS, k=1)[0],
                    "amount": amount,
                    "status": status,
                    "dt": order_day.isoformat(),
                    "created_at": created.strftime("%Y-%m-%d %H:%M:%S"),
                }
            )
    return orders


def aggregate_customer_profile(customers: list[dict], orders: list[dict]) -> list[dict]:
    """复现 customer_profile.sql 的聚合结果。"""
    paid: dict[int, list[dict]] = defaultdict(list)
    for order in orders:
        if order["status"] == "paid" and "2023-01-01" <= order["dt"] < "2025-01-01":
            paid[order["customer_id"]].append(order)

    rows = []
    for customer in customers:
        if customer["status"] != "active":
            continue  # 对应 SQL 中的 WHERE c.status = 'active'
        customer_orders = paid.get(customer["id"], [])
        total = round(sum(o["amount"] for o in customer_orders), 2) if customer_orders else 0.0
        rows.append(
            {
                "customer_id": customer["id"],
                "cust_no": customer["cust_no"],
                "customer_name": customer["customer_name"],
                "city": customer["city"],
                "gender": customer["gender"],
                "age": "" if customer["age"] is None else customer["age"],
                "order_count": len(customer_orders),
                "total_amount": f"{total:.2f}",
                "last_order_at": max((o["created_at"] for o in customer_orders), default=""),
            }
        )
    return rows


def aggregate_order_daily(orders: list[dict]) -> list[dict]:
    """复现 order_daily.sql 的聚合结果。"""
    buckets: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for order in orders:
        if "2023-01-01" <= order["dt"] < "2025-01-01":
            buckets[(order["dt"], order["channel"])].append(order)

    rows = []
    for (stat_date, channel) in sorted(buckets):
        group = buckets[(stat_date, channel)]
        amounts = [o["amount"] for o in group]
        rows.append(
            {
                "stat_date": stat_date,
                "channel": channel,
                "order_count": len(group),
                "total_amount": f"{sum(amounts):.2f}",
                "avg_amount": f"{sum(amounts) / len(amounts):.2f}",
                "refund_count": sum(1 for o in group if o["status"] == "refunded"),
            }
        )
    return rows


# ---------------------------------------------------------------- 输出


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        raise SystemExit(f"没有可写入的数据: {path.name}")
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    rng = random.Random(SEED)

    customers = build_customers(rng)
    orders = build_orders(rng, customers)

    write_csv(OUTPUT_DIR / "customer_profile.csv", aggregate_customer_profile(customers, orders))
    write_csv(OUTPUT_DIR / "order_daily.csv", aggregate_order_daily(orders))

    (OUTPUT_DIR / "customer_profile.sql").write_text(CUSTOMER_PROFILE_SQL, encoding="utf-8")
    (OUTPUT_DIR / "order_daily.sql").write_text(ORDER_DAILY_SQL, encoding="utf-8")

    print(f"客户 {len(customers)} 条，订单 {len(orders)} 条")
    for name in ("customer_profile.csv", "order_daily.csv", "customer_profile.sql", "order_daily.sql"):
        size = (OUTPUT_DIR / name).stat().st_size
        print(f"  {name:26} {size:>9,} 字节")


if __name__ == "__main__":
    main()
