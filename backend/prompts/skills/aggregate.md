---
name: aggregate
title: 聚合层级、GROUP BY 与 HAVING
risk: low
when_to_use: 需要 COUNT/SUM/AVG 等聚合，"至少/超过/恰好 N 个"，按组筛选，或"每个/per/各"分组统计
---
【聚合层级与 HAVING】
① WHERE 中禁止出现聚合函数：对"组"的筛选（组成员数、组内平均/总和/最大满足条件）一律用 GROUP BY + HAVING；对"与全体统计量比较"（高于全表平均/超过最大值的一半）用标量子查询，如 WHERE col > (SELECT AVG(col) FROM t)。
② "至少/超过/恰好 N 个关联记录"直接 JOIN 后 GROUP BY 驱动表主键再 HAVING COUNT(*) op N，不要先在子查询里预聚合再 JOIN（徒增层级且易错）。
③ 分组维度列（each/per/每个 后的列、年/月等时间维度）只进 GROUP BY 和 SELECT，禁止再套一层聚合函数；只有题面要求聚合的指标才用 SUM/AVG/COUNT 包裹。
④ 嵌套聚合最多一层，动笔前先分层：明细表 → GROUP BY 分组 → 组间比较。

示例 — 组条件用 HAVING；与全体平均值比较才用标量子查询：
问题"平均订单金额超过 500 元的会员及其平均金额"（member(member_id, member_name)，sales_order(member_id, amount)）
```sql
SELECT m.member_name, AVG(o.amount) AS avg_amount
FROM member m
JOIN sales_order o ON m.member_id = o.member_id
GROUP BY m.member_id, m.member_name
HAVING AVG(o.amount) > 500
```
（对比：问题若是"金额高于全体订单平均金额的订单"，则是行级过滤，写 WHERE o.amount > (SELECT AVG(amount) FROM sales_order)，不写 HAVING）
