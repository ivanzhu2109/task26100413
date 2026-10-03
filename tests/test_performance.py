"""One root with three sibling branches of 10000 rows each.

Grouping by the dimensions of the first two branches must not trigger a
cartesian join of the raw sibling details: the query has to finish well
within the timeout and return 4 correct groups.
"""

import sqlite3
import time
import unittest

from semantic_query import Dimension, Entity, Metric, Model, Query, execute

N = 10000
TIMEOUT_SECONDS = 30


def build_model():
    return Model([
        Entity("r", "r", "id", {}),
        Entity("b1", "b1", "id", {"dim": "dim", "val": "val"},
               parent="r", fk="rid"),
        Entity("b2", "b2", "id", {"dim": "dim", "val": "val"},
               parent="r", fk="rid"),
        Entity("b3", "b3", "id", {"dim": "dim", "val": "val"},
               parent="r", fk="rid"),
    ])


def build_conn():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE r(id INTEGER PRIMARY KEY);
        CREATE TABLE b1(id INTEGER PRIMARY KEY, rid INTEGER NOT NULL,
                        dim INTEGER, val INTEGER);
        CREATE TABLE b2(id INTEGER PRIMARY KEY, rid INTEGER NOT NULL,
                        dim INTEGER, val INTEGER);
        CREATE TABLE b3(id INTEGER PRIMARY KEY, rid INTEGER NOT NULL,
                        dim INTEGER, val INTEGER);
        INSERT INTO r VALUES (1);
        """
    )
    for table in ("b1", "b2", "b3"):
        conn.executemany(
            f"INSERT INTO {table} VALUES (?, 1, ?, ?)",
            ((i, i % 2, i) for i in range(N)),
        )
    return conn


class PerformanceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model = build_model()
        cls.conn = build_conn()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_three_branch_grouping(self):
        query = Query(
            dimensions=[Dimension("b1", "dim"), Dimension("b2", "dim")],
            metrics=[Metric("sum", "b1", "val"),
                     Metric("sum", "b2", "val"),
                     Metric("sum", "b3", "val"),
                     Metric("count", "b3")],
        )
        start = time.monotonic()
        rows = execute(self.conn, self.model, query)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, TIMEOUT_SECONDS,
                        f"query took {elapsed:.1f}s")

        self.assertEqual(len(rows), 4)
        by_key = {(r["b1.dim"], r["b2.dim"]): r for r in rows}
        self.assertEqual(set(by_key), {(0, 0), (0, 1), (1, 0), (1, 1)})

        even_sum = sum(i for i in range(N) if i % 2 == 0)
        odd_sum = sum(i for i in range(N) if i % 2 == 1)
        total_sum = sum(range(N))
        for (d1, d2), row in by_key.items():
            # branch 1 records contribute to the groups of their own dim
            self.assertEqual(row["sum_b1_val"], even_sum if d1 == 0 else odd_sum)
            self.assertEqual(row["sum_b2_val"], even_sum if d2 == 0 else odd_sum)
            # branch 3 records appear once in every group
            self.assertEqual(row["sum_b3_val"], total_sum)
            self.assertEqual(row["count_b3"], N)

    def test_grand_total_single_contribution(self):
        query = Query(metrics=[Metric("sum", "b1", "val"),
                               Metric("sum", "b2", "val"),
                               Metric("sum", "b3", "val")])
        (row,) = execute(self.conn, self.model, query)
        self.assertEqual(row["sum_b1_val"], sum(range(N)))
        self.assertEqual(row["sum_b2_val"], sum(range(N)))
        self.assertEqual(row["sum_b3_val"], sum(range(N)))


if __name__ == "__main__":
    unittest.main()
