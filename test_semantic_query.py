import sqlite3
import unittest

from semantic_query import (
    Dimension, Entity, Filter, Metric, Model, Query, compile, execute,
)


def make_fanout_env():
    """One parent (amount 100), child1 rows A/B, child2 two rows of 7."""
    model = Model(
        entities={
            "p": Entity("p", "parent_t", "id", {"amt": "amount"}),
            "c1": Entity("c1", "child1_t", "id", {"label": "label"},
                         parent="p", fk="pid"),
            "c2": Entity("c2", "child2_t", "id", {"amt": "amount"},
                         parent="p", fk="pid"),
        },
        root="p",
    )
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
    CREATE TABLE parent_t(id INTEGER PRIMARY KEY, amount INTEGER);
    CREATE TABLE child1_t(id INTEGER PRIMARY KEY, pid INTEGER NOT NULL, label TEXT);
    CREATE TABLE child2_t(id INTEGER PRIMARY KEY, pid INTEGER NOT NULL, amount INTEGER);
    INSERT INTO parent_t VALUES (1, 100);
    INSERT INTO child1_t VALUES (1, 1, 'A'), (2, 1, 'B');
    INSERT INTO child2_t VALUES (1, 1, 7), (2, 1, 7);
    """)
    return model, conn


class FanoutTest(unittest.TestCase):
    def setUp(self):
        self.model, self.conn = make_fanout_env()

    def tearDown(self):
        self.conn.close()

    def test_grouped_by_first_child(self):
        q = Query(
            dimensions=(Dimension("c1", "label"),),
            metrics=(Metric("p", "sum", "amt"), Metric("c2", "sum", "amt")),
        )
        rows = execute(self.conn, self.model, q)
        got = {r["c1.label"]: (r["sum(p.amt)"], r["sum(c2.amt)"]) for r in rows}
        self.assertEqual(got, {"A": (100, 14), "B": (100, 14)})

    def test_global_total_not_sum_of_groups(self):
        q = Query(metrics=(Metric("p", "sum", "amt"), Metric("c2", "sum", "amt")))
        rows = execute(self.conn, self.model, q)
        self.assertEqual(rows, [{"sum(p.amt)": 100, "sum(c2.amt)": 14}])

    def test_count_and_avg(self):
        q = Query(
            metrics=(
                Metric("p", "count"),
                Metric("c1", "count"),
                Metric("c2", "avg", "amt"),
            )
        )
        rows = execute(self.conn, self.model, q)
        self.assertEqual(rows, [{"count(p)": 1, "count(c1)": 2, "avg(c2.amt)": 7.0}])

    def test_compile_returns_bound_params(self):
        q = Query(
            filters=(Filter("c1", "label", "A"),),
            metrics=(Metric("c2", "sum", "amt"),),
        )
        sql, params = compile(self.model, q)
        self.assertIsInstance(sql, str)
        self.assertEqual(params, ["A"])
        rows = execute(self.conn, self.model, q)
        self.assertEqual(rows, [{"sum(c2.amt)": 14}])


# ---------------------------------------------------------------------------
# Randomized differential test against a naive in-Python reference
# ---------------------------------------------------------------------------

import random


def reference_execute(model, data, query):
    """Naive baseline: expand the LEFT JOIN product in Python, filter, group,
    dedupe each metric by its entity primary key, then aggregate."""
    referenced = set()
    for d in query.dimensions:
        referenced.add(d.entity)
    for f in query.filters:
        referenced.add(f.entity)
    for m in query.metrics:
        referenced.add(m.entity)
    entities = set()
    for e in referenced:
        cur = e
        while cur is not None:
            entities.add(cur)
            cur = model.entities[cur].parent
    def depth_of(e):
        d = 0
        cur = e
        while model.entities[cur].parent is not None:
            cur = model.entities[cur].parent
            d += 1
        return d

    order = sorted(entities, key=depth_of)
    root = model.root
    combos = [{root: row} for row in data[root]]
    for e in order:
        if e == root:
            continue
        parent = model.entities[e].parent
        fk = model.entities[e].fk
        pk = model.entities[parent].pk
        new_combos = []
        for combo in combos:
            prow = combo[parent]
            matched = [r for r in data[e]
                       if prow is not None and r[fk] == prow[pk]]
            if matched:
                for r in matched:
                    c2 = dict(combo)
                    c2[e] = r
                    new_combos.append(c2)
            else:
                c2 = dict(combo)
                c2[e] = None
                new_combos.append(c2)
        combos = new_combos

    def passes(combo):
        for f in query.filters:
            rec = combo[f.entity]
            col = model.entities[f.entity].fields[f.field]
            if rec is None or rec[col] != f.value:
                return False
        return True

    combos = [c for c in combos if passes(c)]

    groups = {}
    if not query.dimensions:
        groups[()] = combos
    else:
        for c in combos:
            key = []
            for d in query.dimensions:
                rec = c[d.entity]
                col = model.entities[d.entity].fields[d.field]
                key.append(rec[col] if rec is not None else None)
            groups.setdefault(tuple(key), []).append(c)

    results = {}
    for key, rows in groups.items():
        out = {}
        for m in query.metrics:
            name = m.alias or (
                f"count({m.entity})" if m.agg == "count"
                else f"{m.agg}({m.entity}.{m.field})")
            pk = model.entities[m.entity].pk
            seen = {}
            for c in rows:
                rec = c[m.entity]
                if rec is not None:
                    seen[rec[pk]] = rec
            if m.agg == "count":
                out[name] = len(seen)
            else:
                col = model.entities[m.entity].fields[m.field]
                vals = [r[col] for r in seen.values() if r[col] is not None]
                if not vals:
                    out[name] = None
                elif m.agg == "sum":
                    out[name] = sum(vals)
                else:
                    out[name] = sum(vals) / len(vals)
        results[key] = out
    return results


def build_random_case(rng):
    """Random tree model (depth <= 3, <= 5 entities) + data + query."""
    n_entities = rng.randint(2, 5)
    names = [f"e{i}" for i in range(n_entities)]
    entities = {
        "e0": Entity("e0", "t_e0", "id", {"g": "g", "v": "v"}),
    }
    for i in range(1, n_entities):
        # parent: any earlier entity within depth limit 3
        candidates = []
        for j in range(i):
            d = 0
            cur = f"e{j}"
            while entities[cur].parent is not None:
                cur = entities[cur].parent
                d += 1
            if d < 3:
                candidates.append(f"e{j}")
        parent = rng.choice(candidates)
        entities[f"e{i}"] = Entity(
            f"e{i}", f"t_e{i}", "id", {"g": "g", "v": "v"},
            parent=parent, fk="pid",
        )
    model = Model(entities=entities, root="e0")

    data = {}
    pk_counter = {n: 0 for n in names}

    def gen_rows(entity, parent_rows):
        rows = []
        for prow in parent_rows:
            for _ in range(rng.randint(0, 3)):  # empty branches included
                pk_counter[entity] += 1
                rows.append({
                    "id": pk_counter[entity],
                    "pid": prow["id"],
                    "g": rng.choice([None, "a", "b"]),
                    "v": rng.choice([None, 1, 2, 3, 5, 7]),
                })
        return rows

    data["e0"] = []
    for _ in range(rng.randint(1, 3)):
        pk_counter["e0"] += 1
        data["e0"].append({
            "id": pk_counter["e0"],
            "g": rng.choice([None, "a", "b"]),
            "v": rng.choice([None, 1, 2, 3, 5, 7]),
        })
    for i in range(1, n_entities):
        e = f"e{i}"
        data[e] = gen_rows(e, data[entities[e].parent])

    # dimensions: 0-2 distinct entities, field g
    n_dims = rng.randint(0, 2)
    dim_entities = rng.sample(names, n_dims)
    dimensions = tuple(Dimension(e, "g") for e in dim_entities)

    # filters: two on the same root-to-leaf path, plus optional extra
    leaf = rng.choice(names)
    path = []
    cur = leaf
    while cur is not None:
        path.append(cur)
        cur = entities[cur].parent
    picks = rng.sample(path, min(2, len(path)))
    filters = []
    for e in picks:
        if rng.random() < 0.5:
            filters.append(Filter(e, "g", rng.choice(["a", "b"])))
        else:
            filters.append(Filter(e, "v", rng.choice([1, 2, 3, 5, 7])))
    if rng.random() < 0.5:
        e = rng.choice(names)
        filters.append(Filter(e, "g", rng.choice(["a", "b"])))
    filters = tuple(filters)

    metrics = []
    for _ in range(rng.randint(1, 3)):
        agg = rng.choice(["sum", "count", "avg"])
        e = rng.choice(names)
        if agg == "count":
            metrics.append(Metric(e, "count"))
        else:
            metrics.append(Metric(e, agg, "v"))
    metrics = tuple(metrics)

    return model, data, Query(dimensions=dimensions, filters=filters,
                              metrics=metrics)


class RandomizedDifferentialTest(unittest.TestCase):
    SEEDS = 60

    def test_against_naive_reference(self):
        for seed in range(self.SEEDS):
            rng = random.Random(1000 + seed)
            model, data, query = build_random_case(rng)
            conn = sqlite3.connect(":memory:")
            try:
                for name, ent in model.entities.items():
                    cols = "id INTEGER PRIMARY KEY, g TEXT, v INTEGER"
                    if ent.parent is not None:
                        cols = ("id INTEGER PRIMARY KEY, pid INTEGER NOT NULL, "
                                "g TEXT, v INTEGER")
                    conn.execute(f"CREATE TABLE {ent.table} ({cols})")
                    for row in data[name]:
                        if ent.parent is not None:
                            conn.execute(
                                f"INSERT INTO {ent.table} VALUES (?,?,?,?)",
                                (row["id"], row["pid"], row["g"], row["v"]))
                        else:
                            conn.execute(
                                f"INSERT INTO {ent.table} VALUES (?,?,?)",
                                (row["id"], row["g"], row["v"]))
                got_rows = execute(conn, model, query)
            finally:
                conn.close()
            expected = reference_execute(model, data, query)

            dim_names = [d.alias or f"{d.entity}.{d.field}"
                         for d in query.dimensions]
            got = {}
            for row in got_rows:
                key = tuple(row[n] for n in dim_names)
                got[key] = {k: v for k, v in row.items() if k not in dim_names}
            ctx = f"seed={seed} query={query}"
            self.assertEqual(set(got), set(expected), ctx)
            for key, exp_metrics in expected.items():
                for mname, exp_val in exp_metrics.items():
                    got_val = got[key][mname]
                    if exp_val is None:
                        self.assertIsNone(got_val, f"{ctx} key={key} {mname}")
                    elif isinstance(exp_val, float):
                        self.assertAlmostEqual(got_val, exp_val, places=9,
                                               msg=f"{ctx} key={key} {mname}")
                    else:
                        self.assertEqual(got_val, exp_val,
                                         f"{ctx} key={key} {mname}")


class SiblingFanoutPerfTest(unittest.TestCase):
    """One root, three sibling branches x 10000 rows, two dim values each.
    Group by the first two branches, aggregate all three. Must stay far
    below the 30s budget and return exactly 4 correct groups."""

    N = 10000
    TIMEOUT = 30.0

    def test_three_sibling_branches(self):
        import time

        model = Model(
            entities={
                "r": Entity("r", "t_r", "id", {}),
                "b1": Entity("b1", "t_b1", "id", {"g": "g", "v": "v"},
                             parent="r", fk="pid"),
                "b2": Entity("b2", "t_b2", "id", {"g": "g", "v": "v"},
                             parent="r", fk="pid"),
                "b3": Entity("b3", "t_b3", "id", {"g": "g", "v": "v"},
                             parent="r", fk="pid"),
            },
            root="r",
        )
        conn = sqlite3.connect(":memory:")
        conn.executescript("""
        CREATE TABLE t_r(id INTEGER PRIMARY KEY);
        CREATE TABLE t_b1(id INTEGER PRIMARY KEY, pid INTEGER NOT NULL, g TEXT, v INTEGER);
        CREATE TABLE t_b2(id INTEGER PRIMARY KEY, pid INTEGER NOT NULL, g TEXT, v INTEGER);
        CREATE TABLE t_b3(id INTEGER PRIMARY KEY, pid INTEGER NOT NULL, g TEXT, v INTEGER);
        INSERT INTO t_r VALUES (1);
        """)
        for branch in ("t_b1", "t_b2", "t_b3"):
            conn.executemany(
                f"INSERT INTO {branch} VALUES (?,?,?,?)",
                ((i, 1, "x" if i % 2 == 0 else "y", i % 97)
                 for i in range(1, self.N + 1)),
            )

        query = Query(
            dimensions=(Dimension("b1", "g"), Dimension("b2", "g")),
            metrics=(
                Metric("b1", "sum", "v"),
                Metric("b2", "sum", "v"),
                Metric("b3", "sum", "v"),
                Metric("b3", "count"),
                Metric("b3", "avg", "v"),
            ),
        )
        start = time.monotonic()
        rows = execute(conn, model, query)
        elapsed = time.monotonic() - start
        conn.close()

        self.assertLess(elapsed, self.TIMEOUT,
                        f"query took {elapsed:.1f}s")
        self.assertEqual(len(rows), 4)

        def branch_stats(table_rows, gval):
            vals = [v for g, v in table_rows if g == gval]
            return sum(vals), len(vals)

        b1_rows = [("x" if i % 2 == 0 else "y", i % 97)
                   for i in range(1, self.N + 1)]
        b2_rows = list(b1_rows)
        b3_rows = list(b1_rows)
        b3_total = sum(v for _, v in b3_rows)
        b3_avg = b3_total / self.N

        got = {(r["b1.g"], r["b2.g"]): r for r in rows}
        for g1 in ("x", "y"):
            for g2 in ("x", "y"):
                row = got[(g1, g2)]
                s1, _ = branch_stats(b1_rows, g1)
                s2, _ = branch_stats(b2_rows, g2)
                self.assertEqual(row["sum(b1.v)"], s1, (g1, g2))
                self.assertEqual(row["sum(b2.v)"], s2, (g1, g2))
                self.assertEqual(row["sum(b3.v)"], b3_total, (g1, g2))
                self.assertEqual(row["count(b3)"], self.N, (g1, g2))
                self.assertAlmostEqual(row["avg(b3.v)"], b3_avg, places=9)


class EdgeCaseTest(unittest.TestCase):
    def setUp(self):
        self.model, self.conn = make_fanout_env()

    def tearDown(self):
        self.conn.close()

    def test_empty_filter_result_still_returns_global_row(self):
        q = Query(
            filters=(Filter("c1", "label", "NOPE"),),
            metrics=(
                Metric("p", "count"),
                Metric("p", "sum", "amt"),
                Metric("c2", "avg", "amt"),
            ),
        )
        rows = execute(self.conn, self.model, q)
        self.assertEqual(rows, [{
            "count(p)": 0, "sum(p.amt)": None, "avg(c2.amt)": None,
        }])

    def test_missing_child_forms_null_dimension(self):
        self.conn.execute("INSERT INTO parent_t VALUES (2, 50)")
        self.conn.execute("INSERT INTO child2_t VALUES (3, 2, 3)")
        q = Query(
            dimensions=(Dimension("c1", "label"),),
            metrics=(Metric("p", "sum", "amt"), Metric("c2", "sum", "amt")),
        )
        rows = execute(self.conn, self.model, q)
        got = {r["c1.label"]: (r["sum(p.amt)"], r["sum(c2.amt)"]) for r in rows}
        self.assertEqual(got, {"A": (100, 14), "B": (100, 14), None: (50, 3)})

    def test_null_dimension_value_groups_together(self):
        self.conn.execute("INSERT INTO child1_t VALUES (3, 1, NULL)")
        q = Query(
            dimensions=(Dimension("c1", "label"),),
            metrics=(Metric("c1", "count"),),
        )
        rows = execute(self.conn, self.model, q)
        got = {r["c1.label"]: r["count(c1)"] for r in rows}
        self.assertEqual(got, {"A": 1, "B": 1, None: 1})

    def test_same_value_different_pk_counted_separately(self):
        q = Query(metrics=(Metric("c2", "sum", "amt"), Metric("c2", "count")))
        rows = execute(self.conn, self.model, q)
        self.assertEqual(rows, [{"sum(c2.amt)": 14, "count(c2)": 2}])

    def test_filter_on_metric_branch_does_not_fanout(self):
        q = Query(
            filters=(Filter("c2", "amt", 7),),
            metrics=(Metric("p", "sum", "amt"), Metric("c2", "count")),
        )
        rows = execute(self.conn, self.model, q)
        self.assertEqual(rows, [{"sum(p.amt)": 100, "count(c2)": 2}])


class SharedPathDimTest(unittest.TestCase):
    def test_two_dims_on_same_entity(self):
        model = Model(
            entities={
                "r": Entity("r", "t_r", "id", {}),
                "c": Entity("c", "t_c", "id",
                            {"g": "g", "h": "h", "v": "v"},
                            parent="r", fk="pid"),
            },
            root="r",
        )
        conn = sqlite3.connect(":memory:")
        conn.executescript("""
        CREATE TABLE t_r(id INTEGER PRIMARY KEY);
        CREATE TABLE t_c(id INTEGER PRIMARY KEY, pid INTEGER NOT NULL,
                         g TEXT, h TEXT, v INTEGER);
        INSERT INTO t_r VALUES (1);
        INSERT INTO t_c VALUES (1,1,'a','u',1),(2,1,'a','v',2),(3,1,'b','u',3);
        """)
        q = Query(
            dimensions=(Dimension("c", "g"), Dimension("c", "h")),
            metrics=(Metric("c", "sum", "v"), Metric("c", "count")),
        )
        rows = execute(conn, model, q)
        got = {(r["c.g"], r["c.h"]): (r["sum(c.v)"], r["count(c)"])
               for r in rows}
        self.assertEqual(got, {("a", "u"): (1, 1), ("a", "v"): (2, 1),
                               ("b", "u"): (3, 1)})
        conn.close()

    def test_dims_on_ancestor_and_descendant(self):
        model = Model(
            entities={
                "r": Entity("r", "t_r", "id", {"rv": "v"}),
                "a": Entity("a", "t_a", "id", {"g": "g"}, parent="r", fk="pid"),
                "b": Entity("b", "t_b", "id", {"h": "h", "v": "v"},
                            parent="a", fk="pid"),
            },
            root="r",
        )
        conn = sqlite3.connect(":memory:")
        conn.executescript("""
        CREATE TABLE t_r(id INTEGER PRIMARY KEY, v INTEGER);
        CREATE TABLE t_a(id INTEGER PRIMARY KEY, pid INTEGER NOT NULL, g TEXT);
        CREATE TABLE t_b(id INTEGER PRIMARY KEY, pid INTEGER NOT NULL,
                         h TEXT, v INTEGER);
        INSERT INTO t_r VALUES (1, 10);
        INSERT INTO t_a VALUES (1,1,'x'),(2,1,'y');
        INSERT INTO t_b VALUES (1,1,'p',1),(2,1,'q',2),(3,2,'p',4);
        """)
        q = Query(
            dimensions=(Dimension("a", "g"), Dimension("b", "h")),
            metrics=(Metric("r", "sum", "rv"), Metric("b", "sum", "v"),
                     Metric("a", "count")),
        )
        rows = execute(conn, model, q)
        got = {(r["a.g"], r["b.h"]): (r["sum(r.rv)"], r["sum(b.v)"],
                                      r["count(a)"]) for r in rows}
        self.assertEqual(got, {("x", "p"): (10, 1, 1),
                               ("x", "q"): (10, 2, 1),
                               ("y", "p"): (10, 4, 1)})
        conn.close()


if __name__ == "__main__":
    unittest.main()
