# task26100413

## 语义指标查询组件

把"业务实体 + 维度 + 指标"的结构化查询编译成一条 SQLite SQL。
一对多连接不会重复计算指标：每个指标在独立的子查询里先按所属实体的
主键去重再聚合；维度组合由预去重的维度子查询展开，不会对兄弟分支的
原始明细做乘积连接。仅依赖 Python 标准库（`sqlite3`）。

### 调用方式

```python
import sqlite3
from semantic_query import (
    Dimension, Entity, Filter, Metric, Model, Query, compile, execute,
)

# 1. 描述模型：实体 -> 物理表，字段 -> 物理列，父子关系构成有根树
model = Model([
    Entity("order",   "orders",      "id", {"amount": "amt"}),
    Entity("line",    "order_lines", "id", {"sku": "sku", "qty": "qty"},
           parent="order", fk="order_id"),
    Entity("payment", "payments",    "id", {"amount": "amt"},
           parent="order", fk="order_id"),
])

# 2. 构造结构化查询：0-2 个分组维度、等值过滤（AND）、SUM/COUNT/AVG 指标
query = Query(
    dimensions=[Dimension("line", "sku")],
    filters=[Filter("payment", "amount", 7)],
    metrics=[
        Metric("sum", "order", "amount"),   # 按 order 主键去重后求和
        Metric("count", "line"),            # 每组 line 记录数
        Metric("avg", "payment", "amount"),
    ],
)

# 3a. 只编译：得到一条 SQL 与绑定参数
sql, params = compile(model, query)

# 3b. 或直接执行：返回 list[dict]，键为 "实体.字段" 维度名与指标名
conn = sqlite3.connect(":memory:")
rows = execute(conn, model, query)
# -> [{"line.sku": "A", "sum_order_amount": 100, "count_line": 2, ...}, ...]
```

### 语义要点

- 基准语义：根到所有被引用实体 LEFT JOIN，过滤后按维度分组；每个指标
  在组内先按所属实体主键去重再聚合（同值不同主键分别贡献）。
- 全局总计（无分组维度）每条源记录只贡献一次，不是分组结果相加；
  过滤后无记录也返回一行（COUNT 为 0，SUM/AVG 为 NULL）。
- 缺失子记录形成 NULL 维度组，但不贡献该实体的指标；SUM/AVG 忽略
  NULL 值；NULL 维度遵循 SQLite 分组语义。
- 指标标签默认自动生成（如 `sum_order_amount`、`count_line`），也可用
  `Metric(..., label="...")` 指定；重复标签会自动加后缀去重。

### 运行测试

```sh
python -m unittest discover -s tests
```

覆盖：扇出去重场景（父 100 / 子 AB / 另一子表两条 7）、固定种子多层树
与朴素参照逐组核对（含空分支、NULL 值、同路径多过滤）、以及单根三万行
三分支 30 秒超时的性能测试。
