---
name: set_difference
title: 差集"有 A 无 B"
risk: low
when_to_use: 问"有 A 但没有 B""不使用/不包含/从未……的 X"，或"既 A 又 B"的交集语义
---
【差集"有 A 无 B"】"有 A 但没有 B""不使用/不包含/从未……的 X"是集合差运算（MySQL 不支持 EXCEPT/MINUS/INTERSECT 关键字，禁止写出）。首选写法是保持直接 JOIN 的外层结构，用 `NOT 主键/外键列 IN (SELECT 关联键 FROM … JOIN … WHERE B 条件)` 排除（子查询里的 SELECT 列必须是非空的主键/外键）；仅当无法保证子查询列非空（含 NULL 会使 NOT IN 结果整体为空）时，才退用 NOT EXISTS 相关子查询或 LEFT JOIN ... IS NULL：
- 严禁用"INNER JOIN 关联表后 WHERE 关联标志 = 否定值"来表达差集——那只能找到"有关联记录但标志为否"的行，会漏掉"完全没有关联记录"的行（差集恰恰必须包含它们）。
- "既有 A 又有 B"是交集：两次 JOIN（或两个 EXISTS 用 AND 相连）。

示例 — 差集"有 A 无 B"：外层保持直接 JOIN，用 NOT IN 子查询排除（子查询选非空外键）：
问题"找出借过《数据库系统概念》但从未借过《软件工程》的读者姓名"（reader(reader_id, reader_name)，book(book_id, title)，borrow_record(reader_id, book_id, borrow_date)）
```sql
SELECT r.reader_name
FROM reader r
JOIN borrow_record b1 ON r.reader_id = b1.reader_id
JOIN book k1 ON b1.book_id = k1.book_id AND k1.title = '数据库系统概念'
WHERE r.reader_id NOT IN (
    SELECT b2.reader_id
    FROM borrow_record b2
    JOIN book k2 ON b2.book_id = k2.book_id
    WHERE k2.title = '软件工程'
)
```
（"从未借过 B"必须用 NOT IN/NOT EXISTS 表达全集之差，不能 INNER JOIN borrow_record 后 WHERE title <> '软件工程'——那会漏掉没有任何 B 借阅记录的读者；NOT IN 子查询里只能选非空的主键/外键列）
