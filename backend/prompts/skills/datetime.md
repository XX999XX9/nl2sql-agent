---
name: datetime
title: 日期与时间条件
risk: low
when_to_use: 条件或分组涉及日期/时间/年份月份、按年统计、时间区间、相对时间（近 N 天等）
---
【日期与时间条件】先按给定【表结构】判断列类型再选写法：
- INT/YEAR 等数值型年份列：用裸数字比较且不加引号，如 birth_year = 1995、year_col BETWEEN 2010 AND 2012。
- DATE/DATETIME/TIMESTAMP 列：用字符串字面量比较，如 order_date = '2023-04-01'；按年/月统计用 YEAR(col)/MONTH(col) 提取后 GROUP BY；时间范围优先左闭右开 col >= '2023-01-01' AND col < '2024-01-01'，用 BETWEEN 时含两端、DATETIME 列需补到 '23:59:59'。
- 间隔/相对时间用 DATEDIFF(a, b)（天数差）、col >= DATE_SUB(CURDATE(), INTERVAL N DAY/MONTH/YEAR)；字符串存储的日期必须先 STR_TO_DATE 再比较，禁止字符串与日期列直接比大小。

示例 — DATETIME 列按年过滤用左闭右开区间，时间维度只 GROUP BY 不聚合：
问题"统计 2023 年每个月的订单数"（sales_order(order_id, order_time DATETIME)）
```sql
SELECT MONTH(o.order_time) AS month_no, COUNT(*) AS order_cnt
FROM sales_order o
WHERE o.order_time >= '2023-01-01' AND o.order_time < '2024-01-01'
GROUP BY MONTH(o.order_time)
```
（对比：若年份列是 INT 型 birth_year，则直接 WHERE birth_year BETWEEN 1990 AND 1999，数字不加引号）
