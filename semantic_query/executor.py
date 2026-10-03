"""Executes a compiled query against a sqlite3 connection."""

from typing import Any, Dict, List

from .compiler import compile
from .model import Model, Query


def execute(conn, model: Model, query: Query) -> List[Dict[str, Any]]:
    """Compile and run the query; return one dict per result row."""
    sql, params = compile(model, query)
    cur = conn.execute(sql, params)
    columns = [d[0] for d in cur.description]
    if columns == ["NULL"]:
        return [{}]
    return [dict(zip(columns, row)) for row in cur.fetchall()]
