-- customer_profile.sql
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
