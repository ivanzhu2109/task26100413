import unittest


import sqlite3

from semantic_query import (
    Dimension,
    Entity,
    Filter,
    Metric,
    Model,
    Query,
    compile,
    execute,
)


def fanout_model():
    return Model([
        Entity("p", "p", "id", {"amount": "amt"}),
        Entity("c1", "c1", "id", {"dim": "d"}, parent="p", fk="pid"),
        Entity("c2", "c2", "id", {"amount": "amt"}, parent="p", fk="pid"),
    ])


def fanout_conn():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE p(id INTEGER PRIMARY KEY, amt INTEGER);
        CREATE TABLE c1(id INTEGER PRIMARY KEY, pid INTEGER NOT NULL, d TEXT);
        CREATE TABLE c2(id INTEGER PRIMARY KEY, pid INTEGER NOT NULL, amt INTEGER);
        INSERT INTO p VALUES (1, 100);
        INSERT INTO c1 VALUES (1, 1, 'A'), (2, 1, 'B');
        INSERT INTO c2 VALUES (1, 1, 7), (2, 1, 7);
        """
    )
    return conn


def by_dim(rows, dim):
    return {row[dim]: row for row in rows}


class FanoutTest(unittest.TestCase):
    """One parent of 100; child1 has rows A,B; child2 has two rows of 7."""

    def setUp(self):
        self.model = fanout_model()
        self.conn = fanout_conn()

    def tearDown(self):
        self.conn.close()

    def test_grouped_by_first_child(self):
        q = Query(
            dimensions=[Dimension("c1", "dim")],
            metrics=[Metric("sum", "p", "amount"), Metric("sum", "c2", "amount")],
        )
        rows = by_dim(execute(self.conn, self.model, q), "c1.dim")
        self.assertEqual(set(rows), {"A", "B"})
        for row in rows.values():
            self.assertEqual(row["sum_p_amount"], 100)
            self.assertEqual(row["sum_c2_amount"], 14)

    def test_grand_total_not_sum_of_groups(self):
        q = Query(metrics=[Metric("sum", "p", "amount"),
                           Metric("sum", "c2", "amount")])
        rows = execute(self.conn, self.model, q)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sum_p_amount"], 100)
        self.assertEqual(rows[0]["sum_c2_amount"], 14)

    def test_count_and_avg(self):
        q = Query(metrics=[Metric("count", "c1"), Metric("avg", "c2", "amount")])
        (row,) = execute(self.conn, self.model, q)
        self.assertEqual(row["count_c1"], 2)
        self.assertAlmostEqual(row["avg_c2_amount"], 7.0)

    def test_empty_result_still_returns_global_row(self):
        q = Query(
            filters=[Filter("c1", "dim", "ZZZ")],
            metrics=[Metric("count", "c1"), Metric("sum", "p", "amount"),
                     Metric("avg", "c2", "amount")],
        )
        rows = execute(self.conn, self.model, q)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["count_c1"], 0)
        self.assertIsNone(rows[0]["sum_p_amount"])
        self.assertIsNone(rows[0]["avg_c2_amount"])

    def test_compile_returns_sql_and_params(self):
        q = Query(filters=[Filter("c1", "dim", "A")],
                  metrics=[Metric("count", "c2")])
        sql, params = compile(self.model, q)
        self.assertIsInstance(sql, str)
        self.assertEqual(params, ["A"])
        rows = self.conn.execute(sql, params).fetchall()
        self.assertEqual(rows[0][0], 2)


if __name__ == "__main__":
    unittest.main()
