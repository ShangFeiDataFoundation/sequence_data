-- quick_orders.sql
-- 快速成功解析：基础字段、枚举、金额、时间和过滤条件
SELECT
    o.order_id       AS order_id,
    o.customer_name  AS customer_name,
    o.city           AS city,
    o.channel        AS channel,
    o.amount         AS amount,
    o.order_status   AS order_status,
    o.created_at     AS created_at
FROM ods.orders o
WHERE o.order_status = 'paid'
;
