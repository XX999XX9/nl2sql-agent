---
name: join
title: JOIN 关联键、连接类型与一对多防扇出
risk: low
when_to_use: 凡 SQL 涉及两张及以上表的连接（含子查询中的连接），或需要对一对多关系中的"一端"做 COUNT/SUM/AVG 聚合——只要写 JOIN 就必须同时遵守本技能中的防扇出要求
---
【JOIN 关联键】多表查询必须使用给定【表关联关系】中列出的关联键写 ON 等值条件，禁止无 ON/USING 条件的 JOIN（会形成笛卡尔积），禁止臆造关联键；【表关联关系】未覆盖的表对，只能用"同名列且其中一端是该表主键"的列关联。

【JOIN 类型】需要保留某张表全部行时必须用外连接：问题含"所有/每个/全部 X（含没有 Y 的 X）"语义时，把要保留全部行的表放左侧并使用 LEFT JOIN；只有问题明确要求"两表都必须匹配上"时才用 INNER JOIN，避免把外连接误用成内连接而丢失无匹配的行。

【防 JOIN 扇出（写 JOIN 时必须检查）】若 JOIN 对端相对驱动表是一对多关系（【表关联关系】中注明的"多端"表），禁止直接对驱动表（一端表）的金额/数量等列做 SUM/AVG，也禁止直接 COUNT(*)——驱动表行会被多端复制，导致结果成倍放大；必须先在子查询/CTE 中对多端按关联键 GROUP BY 去重或聚合，再与一端表 JOIN。

示例1 — 需要保留左表全部行，用 LEFT JOIN：
问题"列出所有商品及其订单数（含从未被购买的商品）"
```sql
SELECT p.product_name, COALESCE(c.cnt, 0) AS order_count
FROM product p
LEFT JOIN (SELECT product_id, COUNT(*) AS cnt FROM order_record GROUP BY product_id) c
  ON p.product_id = c.product_id
```

示例2 — 一对多 JOIN 后做聚合，先在子查询中聚合多端，防止扇出：
问题"统计每个用户的订单总金额（一张订单有多条明细）"
```sql
SELECT u.user_name, COALESCE(t.total_amount, 0) AS total_amount
FROM user u
LEFT JOIN (
    SELECT o.user_id, SUM(i.item_amount) AS total_amount
    FROM order_record o
    JOIN order_item i ON o.order_id = i.order_id
    GROUP BY o.user_id
) t ON u.user_id = t.user_id
```
