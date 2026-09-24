-- order_daily.sql
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
