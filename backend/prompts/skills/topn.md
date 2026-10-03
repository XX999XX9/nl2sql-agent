---
name: topn
title: Top-N 与极值
risk: low
when_to_use: 问"最/最高/最低/最多/最少/最早/最晚/最常见的一个"或"前 N 个/N 强"，或要取极值行上的其他属性
---
【Top-N 与极值】"最……的一个"（最高/最低/最多/最少/最早/最晚/最常见）标准写法是 GROUP BY 维度 + ORDER BY 聚合表达式 DESC/ASC + LIMIT 1，直接取排序后首行；"前 N 个"同理 LIMIT N。禁止用 HAVING 聚合 = (SELECT MAX/MIN(聚合) …) 的嵌套写法取单个结果——那是"所有并列第一的全部 X"的语义，仅当题面明确要求"所有并列/全部达到最高值的"时才使用。若要取极值行上的其他属性（如"最早一年生产的商品及其厂商"），用 WHERE 排序列 = (SELECT MIN/MAX(排序列) …)。

示例 — Top-1：问"最……的一个"，分组排序后 LIMIT 1，禁止 HAVING 套 MAX 子查询：
问题"哪个门店的订单数最多？"（store(store_id, store_name)，sales_order(order_id, store_id, amount)）
```sql
SELECT s.store_name
FROM store s
JOIN sales_order o ON s.store_id = o.store_id
GROUP BY s.store_id, s.store_name
ORDER BY COUNT(o.order_id) DESC
LIMIT 1
```
（仅当问题改为"所有订单数并列最多的门店"时，才写 HAVING COUNT(o.order_id) = (SELECT MAX(c) FROM (SELECT COUNT(*) c FROM sales_order GROUP BY store_id) x)）
