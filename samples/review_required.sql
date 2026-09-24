-- review_required.sql
-- 语义待确认：前三个字段无法通过常规规则判断业务含义
SELECT
    q.alpha AS alpha,
    q.beta  AS beta,
    q.gamma AS gamma,
    q.amt   AS amt
FROM raw.review_queue q
;
