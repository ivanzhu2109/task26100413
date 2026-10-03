# task26100413

语义指标查询组件：把业务实体、维度和指标编译成一条 SQLite SQL，
保证一对多连接（扇出）不会重复计算指标。仅依赖 Python 标准库。

## 模型与查询定义

```python
from semantic_query import Model, Entity, Dimension, Filter, Metric, Query

model = Model(
    entities={
        "order":   Entity("order", "orders", "id",
                          {"amount": "amount"}),
        "line":    Entity("line", "order_lines", "id",
                          {"sku": "sku", "qty": "qty"},
                          parent="order", fk="order_id"),
        "payment": Entity("payment", "payments", "id",
                          {"method": "method", "paid": "paid"},
                          parent="order", fk="order_id"),
    },
    root="order",
)

query = Query(
    dimensions=(Dimension("line", "sku"),),                 # 0-2 个分组维度
    filters=(Filter("payment", "method", "card"),),         # 等值过滤，AND 组合
    metrics=(
        Metric("order", "sum", "amount"),                   # 按 order 主键去重后求和
        Metric("line", "count"),                            # 统计 line 记录数
        Metric("payment", "avg", "paid", alias="avg_paid"),
    ),
)
```

- 实体关系构成有根树：子实体通过非空外键 `fk` 指向父实体主键。
- `fields` 把业务字段名映射到物理列名。
- 维度、过滤、指标可来自任意实体（包括不同兄弟分支）。

## 两个操作

```python
from semantic_query import compile, execute

sql, params = compile(model, query)   # 一条可直接执行的 SQL + 绑定参数
rows = conn.execute(sql, params).fetchall()

rows = execute(conn, model, query)    # 编译并执行，返回 list[dict]
# -> [{"line.sku": "A", "sum(order.amount)": 100, "count(line)": 2, "avg_paid": 7.0}, ...]
```

结果列名：维度为 `实体.字段`，指标为 `sum(实体.字段)` / `count(实体)` 等，
也可用 `alias=` 自定义。

## 语义

等价于：沿根到所有被引用实体的路径 LEFT JOIN，应用全部过滤后按维度分组；
每个指标在组内先按所属实体主键去重再聚合。无分组维度时始终返回一行全局
结果；SUM/AVG 无可聚合值时为 NULL，COUNT 为 0。

实现上不读取原始表到 Python，也不做兄弟分支明细乘积连接：维度组合由按
`(根主键, 维度值)` 去重后的派生表展开，每个指标在各自的子查询中按主键去重
聚合，最后按维度值（NULL 安全的 `IS`）连接。

## 测试

```
python -m unittest
```

覆盖：父子扇出固定用例、固定随机种子下与朴素 Python 参照实现的逐组对比
（含空分支、NULL 值、同路径多过滤）、以及 1 根记录 × 3 兄弟分支 × 10000 行
的 30 秒超时性能正确性测试。
