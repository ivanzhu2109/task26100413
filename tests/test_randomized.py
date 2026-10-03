"""Randomized multi-level tree checked against a naive reference.

The reference expands the joined detail rows in Python, applies the
filters, groups, and de-duplicates each metric by its entity primary key.
"""

import random
import sqlite3
import unittest

from semantic_query import Dimension, Entity, Filter, Metric, Model, Query, execute
from semantic_query.compiler import dimension_labels, metric_labels


def build_model():
    return Model([
        Entity("r", "r", "id", {"region": "region", "amount": "amount"}),
        Entity("a", "a", "id", {"kind": "kind", "dim": "dim_a", "val": "val_a"},
               parent="r", fk="rid"),
        Entity("b", "b", "id", {"dim": "dim_b", "val": "val_b"},
               parent="r", fk="rid"),
        Entity("c", "c", "id", {"flag": "flag", "val": "val_c"},
               parent="a", fk="aid"),
        Entity("d", "d", "id", {"val": "val_d"}, parent="b", fk="bid"),
    ])


def build_conn(seed=20261004):
    rng = random.Random(seed)
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE r(id INTEGER PRIMARY KEY, region TEXT, amount INTEGER);
        CREATE TABLE a(id INTEGER PRIMARY KEY, rid INTEGER NOT NULL,
                       kind TEXT, dim_a TEXT, val_a INTEGER);
        CREATE TABLE b(id INTEGER PRIMARY KEY, rid INTEGER NOT NULL,
                       dim_b TEXT, val_b INTEGER);
        CREATE TABLE c(id INTEGER PRIMARY KEY, aid INTEGER NOT NULL,
                       flag INTEGER, val_c INTEGER);
        CREATE TABLE d(id INTEGER PRIMARY KEY, bid INTEGER NOT NULL,
                       val_d INTEGER);
        """
    )

    def maybe_null(value, p=0.25):
        return None if rng.random() < p else value

    a_id = b_id = c_id = d_id = 0
    for r_id in range(3):
        region = rng.choice(["n", "s", "n"])
        amount = maybe_null(rng.randint(1, 500))
        conn.execute("INSERT INTO r VALUES (?,?,?)", (r_id, region, amount))
        for _ in range(rng.choice([0, 1, 2, 3])):  # some roots have no a
            a_id += 1
            conn.execute(
                "INSERT INTO a VALUES (?,?,?,?,?)",
                (a_id, r_id, rng.choice(["x", "y"]),
                 maybe_null(rng.choice(["d1", "d2"])),
                 maybe_null(rng.randint(1, 99))),
            )
            for _ in range(rng.choice([0, 1, 2])):  # some a have no c
                c_id += 1
                conn.execute(
                    "INSERT INTO c VALUES (?,?,?,?)",
                    (c_id, a_id, rng.choice([0, 1]),
                     maybe_null(rng.randint(1, 50))),
                )
        for _ in range(rng.choice([0, 1, 2, 3])):  # some roots have no b
            b_id += 1
            conn.execute(
                "INSERT INTO b VALUES (?,?,?,?)",
                (b_id, r_id, maybe_null(rng.choice(["e1", "e2"])),
                 maybe_null(rng.randint(1, 99))),
            )
            for _ in range(rng.choice([0, 1, 2])):  # some b have no d
                d_id += 1
                conn.execute(
                    "INSERT INTO d VALUES (?,?,?)",
                    (d_id, b_id, maybe_null(rng.randint(1, 50))),
                )
    return conn


def load_tables(conn, model):
    tables = {}
    for name, e in model.entities.items():
        cur = conn.execute(f'SELECT * FROM "{e.table}"')
        cols = [d[0] for d in cur.description]
        tables[name] = [dict(zip(cols, row)) for row in cur.fetchall()]
    return tables


def naive_execute(model, tables, query):
    """Baseline semantics: join, filter, group, dedupe by pk, aggregate."""
    root = model.root

    def expand(entity, parent_rec):
        e = model.entity(entity)
        if parent_rec is None and entity != root:
            candidates = [None]
        else:
            if entity == root:
                candidates = tables[entity]
            else:
                parent_pk = model.entity(e.parent).pk
                candidates = [r for r in tables[entity]
                              if r[e.fk] == parent_rec[parent_pk]]
            if not candidates:
                candidates = [None]
        rows = []
        for rec in candidates:
            partials = [{entity: rec}]
            for child in model.children[entity]:
                child_rows = expand(child, rec)
                partials = [dict(p, **c) for p in partials for c in child_rows]
            rows.extend(partials)
        return rows

    def passes(row):
        for f in query.filters:
            rec = row[f.entity]
            if rec is None:
                return False
            if rec[model.entity(f.entity).fields[f.field]] != f.value:
                return False
        return True

    rows = [r for r in expand(root, None) if passes(r)]

    dims = query.dimensions
    groups = {}
    if dims:
        for row in rows:
            key = tuple(
                None if row[d.entity] is None
                else row[d.entity][model.entity(d.entity).fields[d.field]]
                for d in dims
            )
            groups.setdefault(key, []).append(row)
    else:
        groups[()] = rows

    out = []
    for key, group_rows in groups.items():
        result = {}
        for i, label in enumerate(dimension_labels(query)):
            result[label] = key[i]
        for label, m in zip(metric_labels(query), query.metrics):
            e = model.entity(m.entity)
            by_pk = {}
            for row in group_rows:
                rec = row[m.entity]
                if rec is None:
                    continue
                by_pk[rec[e.pk]] = rec[e.fields[m.field]] if m.field else None
            if m.op == "count":
                result[label] = len(by_pk)
                continue
            nums = [v for v in by_pk.values() if v is not None]
            if not nums:
                result[label] = None
            elif m.op == "sum":
                result[label] = sum(nums)
            else:
                result[label] = sum(nums) / len(nums)
        out.append(result)
    return out


def normalize(rows):
    def norm_value(v):
        if isinstance(v, float):
            v = round(v, 6)
        return (v is None, str(type(v).__name__), repr(v))

    return sorted(tuple(sorted((k, norm_value(v)) for k, v in row.items()))
                  for row in rows)


DIM_POOL = [
    Dimension("a", "dim"),
    Dimension("b", "dim"),
    Dimension("r", "region"),
    Dimension("c", "flag"),
    Dimension("c", "val"),
    Dimension("d", "val"),
]

FILTER_POOL = [
    Filter("r", "region", "n"),
    Filter("r", "region", "s"),
    Filter("a", "kind", "x"),
    Filter("a", "kind", "y"),
    Filter("c", "flag", 1),
    Filter("c", "flag", 0),
    Filter("b", "dim", "e1"),
    Filter("b", "dim", "e2"),
    Filter("d", "val", 7),
]

METRIC_POOL = [
    Metric("sum", "r", "amount"),
    Metric("count", "r"),
    Metric("sum", "a", "val"),
    Metric("avg", "a", "val"),
    Metric("count", "a"),
    Metric("sum", "b", "val"),
    Metric("avg", "b", "val"),
    Metric("count", "b"),
    Metric("sum", "c", "val"),
    Metric("avg", "c", "val"),
    Metric("count", "c"),
    Metric("sum", "d", "val"),
    Metric("avg", "d", "val"),
    Metric("count", "d"),
]


class RandomizedTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model = build_model()
        cls.conn = build_conn()
        cls.tables = load_tables(cls.conn, cls.model)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def check(self, query):
        got = execute(self.conn, self.model, query)
        want = naive_execute(self.model, self.tables, query)
        self.assertEqual(normalize(got), normalize(want),
                         f"query={query!r}")

    def test_explicit_multi_filter_same_path(self):
        # several filters along one path r -> a -> c, dims on two branches
        query = Query(
            dimensions=[Dimension("a", "dim"), Dimension("b", "dim")],
            filters=[Filter("r", "region", "n"),
                     Filter("a", "kind", "x"),
                     Filter("c", "flag", 1)],
            metrics=[Metric("sum", "r", "amount"),
                     Metric("sum", "c", "val"),
                     Metric("avg", "d", "val"),
                     Metric("count", "b")],
        )
        self.check(query)

    def test_random_queries(self):
        rng = random.Random(42)
        for trial in range(200):
            dims = rng.sample(DIM_POOL, rng.randint(0, 2))
            filters = rng.sample(FILTER_POOL, rng.randint(0, 3))
            metrics = rng.sample(METRIC_POOL, rng.randint(1, 4))
            self.check(Query(dimensions=dims, filters=filters, metrics=metrics))


class RandomizedAltSeedTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model = build_model()
        cls.conn = build_conn(seed=777)
        cls.tables = load_tables(cls.conn, cls.model)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_random_queries(self):
        rng = random.Random(1234)
        for trial in range(200):
            dims = rng.sample(DIM_POOL, rng.randint(0, 2))
            filters = rng.sample(FILTER_POOL, rng.randint(0, 3))
            metrics = rng.sample(METRIC_POOL, rng.randint(1, 4))
            got = execute(self.conn, self.model,
                          Query(dimensions=dims, filters=filters, metrics=metrics))
            want = naive_execute(self.model, self.tables,
                                 Query(dimensions=dims, filters=filters, metrics=metrics))
            self.assertEqual(normalize(got), normalize(want),
                             f"dims={dims} filters={filters} metrics={metrics}")


if __name__ == "__main__":
    unittest.main()
