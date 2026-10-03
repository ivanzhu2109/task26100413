"""Semantic metric query compiler for SQLite.

Callers supply a model (entities mapped to tables, tree-shaped relations)
and a structured query (dimensions, filters, metrics). compile() produces
a single executable SQL statement plus bind parameters; execute() runs it
and returns rows of dimension values and metric values.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class Entity:
    """A business entity mapped to one table with an integer primary key."""
    name: str
    table: str
    pk: str
    fields: dict[str, str]          # business field -> physical column
    parent: Optional[str] = None    # parent entity name (None for root)
    fk: Optional[str] = None        # column in this table referencing parent pk


@dataclass(frozen=True)
class Model:
    entities: dict[str, Entity]
    root: str


@dataclass(frozen=True)
class Dimension:
    entity: str
    field: str
    alias: Optional[str] = None


@dataclass(frozen=True)
class Filter:
    """Equality filter: entity.field = value (value must not be None)."""
    entity: str
    field: str
    value: Any


@dataclass(frozen=True)
class Metric:
    entity: str
    agg: str                        # 'sum' | 'count' | 'avg'
    field: Optional[str] = None     # required for sum/avg, None for count
    alias: Optional[str] = None


@dataclass(frozen=True)
class Query:
    dimensions: tuple[Dimension, ...] = ()
    filters: tuple[Filter, ...] = ()
    metrics: tuple[Metric, ...] = ()




def _qident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


class _Compiler:
    """Translates one (model, query) pair into SQL + params."""

    def __init__(self, model: Model, query: Query):
        self.model = model
        self.query = query
        self._validate()
        # filter conditions grouped by entity: entity -> [(column, value)]
        self.conds: dict[str, list[tuple[str, Any]]] = {}
        for f in query.filters:
            col = model.entities[f.entity].fields[f.field]
            self.conds.setdefault(f.entity, []).append((col, f.value))
        self.children: dict[str, list[str]] = {n: [] for n in model.entities}
        for n, e in model.entities.items():
            if e.parent is not None:
                self.children[e.parent].append(n)
        self.paths: dict[str, list[str]] = {
            n: self._path(n) for n in model.entities
        }

    # ---------- model helpers ----------

    def _validate(self) -> None:
        m, q = self.model, self.query
        if m.root not in m.entities:
            raise ValueError(f"unknown root entity {m.root!r}")
        for n, e in m.entities.items():
            if n != e.name:
                raise ValueError(f"entity key {n!r} != entity name {e.name!r}")
            if n == m.root:
                if e.parent is not None or e.fk is not None:
                    raise ValueError("root entity must not have parent/fk")
            else:
                if e.parent not in m.entities:
                    raise ValueError(f"entity {n!r} has unknown parent {e.parent!r}")
                if not e.fk:
                    raise ValueError(f"entity {n!r} missing fk")
        for n in m.entities:
            self._path(n)  # raises on cycles
        if len(q.dimensions) > 2:
            raise ValueError("at most 2 dimensions supported")
        for d in q.dimensions:
            self._field(d.entity, d.field)
        for f in q.filters:
            self._field(f.entity, f.field)
            if f.value is None:
                raise ValueError("filter value must be a non-NULL constant")
        for mt in q.metrics:
            if mt.entity not in m.entities:
                raise ValueError(f"unknown metric entity {mt.entity!r}")
            if mt.agg not in ("sum", "count", "avg"):
                raise ValueError(f"unsupported agg {mt.agg!r}")
            if mt.agg == "count":
                if mt.field is not None:
                    raise ValueError("count metric must not reference a field")
            else:
                self._field(mt.entity, mt.field)

    def _field(self, entity: str, field: str) -> None:
        e = self.model.entities.get(entity)
        if e is None:
            raise ValueError(f"unknown entity {entity!r}")
        if field not in e.fields:
            raise ValueError(f"unknown field {field!r} on entity {entity!r}")

    def _path(self, name: str) -> list[str]:
        """Entity names from root down to `name`."""
        path = []
        seen = set()
        cur: Optional[str] = name
        while cur is not None:
            if cur in seen:
                raise ValueError("entity relations contain a cycle")
            seen.add(cur)
            path.append(cur)
            cur = self.model.entities[cur].parent
        path.reverse()
        if path[0] != self.model.root:
            raise ValueError(f"entity {name!r} is not reachable from root")
        return path

    def _lca(self, a: str, b: str) -> str:
        pa, pb = self.paths[a], self.paths[b]
        lca = pa[0]
        for x, y in zip(pa, pb):
            if x != y:
                break
            lca = x
        return lca

    def _descendant_or_self(self, maybe_desc: str, anc: str) -> bool:
        return anc in self.paths[maybe_desc]

    # ---------- filter SQL fragments ----------

    def _filter_clauses(self, joined: dict[str, str]) -> tuple[list[str], list[Any]]:
        """All query filters as SQL conditions for a subquery whose joined
        entity set is `joined` (entity -> alias). Filters on joined entities
        become WHERE conditions; others become EXISTS semi-joins anchored at
        the deepest joined ancestor."""
        parts: list[str] = []
        params: list[Any] = []
        for ent, conds in self.conds.items():
            if ent in joined:
                for col, val in conds:
                    parts.append(f"{_qident(joined[ent])}.{_qident(col)} = ?")
                    params.append(val)
        off = [e for e in self.conds if e not in joined]
        for comp in self._components(off, set(joined)):
            sql, p = self._exists_sql(comp, joined)
            parts.append(sql)
            params.extend(p)
        return parts, params

    def _components(self, off: list[str], joined: set[str]) -> list[list[str]]:
        """Group off-path filter entities into connected components of the
        forest obtained by removing `joined` nodes from the entity tree."""
        parent = {e: e for e in off}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: str, b: str) -> None:
            parent[find(a)] = find(b)

        owner: dict[str, str] = {}
        for e in off:
            for node in self.paths[e]:
                if node in joined:
                    continue
                if node in owner:
                    union(e, owner[node])
                else:
                    owner[node] = e
        groups: dict[str, list[str]] = {}
        for e in off:
            groups.setdefault(find(e), []).append(e)
        return list(groups.values())

    def _exists_sql(self, comp: list[str], joined: dict[str, str]) -> tuple[str, list[Any]]:
        """One EXISTS subquery joining the union of the component's paths
        below the joined set, anchored at the top node's parent."""
        jset = set(joined)
        nodes: list[str] = []
        seen: set[str] = set()
        for e in comp:
            for n in self.paths[e]:
                if n not in jset and n not in seen:
                    seen.add(n)
                    nodes.append(n)
        tops = [n for n in nodes if self.model.entities[n].parent not in seen]
        if len(tops) != 1:
            raise AssertionError("filter component is not a single tree")
        top = tops[0]
        anchor = self.model.entities[top].parent
        # BFS order from top so parents are aliased before children
        order = [top]
        i = 0
        while i < len(order):
            for c in self.children[order[i]]:
                if c in seen:
                    order.append(c)
            i += 1
        alias = {n: f"x{k}" for k, n in enumerate(order)}
        params: list[Any] = []
        from_sql = f"{_qident(self.model.entities[top].table)} {_qident(alias[top])}"
        for n in order[1:]:
            e = self.model.entities[n]
            p = e.parent
            from_sql += (
                f" JOIN {_qident(e.table)} {_qident(alias[n])}"
                f" ON {_qident(alias[n])}.{_qident(e.fk)}"
                f" = {_qident(alias[p])}.{_qident(self.model.entities[p].pk)}"
            )
        where = [
            f"{_qident(alias[top])}.{_qident(self.model.entities[top].fk)}"
            f" = {_qident(joined[anchor])}.{_qident(self.model.entities[anchor].pk)}"
        ]
        for e in comp:
            for col, val in self.conds[e]:
                where.append(f"{_qident(alias[e])}.{_qident(col)} = ?")
                params.append(val)
        sql = f"EXISTS (SELECT 1 FROM {from_sql} WHERE {' AND '.join(where)})"
        return sql, params

    # ---------- spine (dimension combinations) ----------

    def _dim_required(self, dim_entity: str) -> bool:
        """True when some filter lies on the path below root to the dimension
        entity or in its subtree; then surviving roots/skeleton rows must
        actually have a matching dimension row."""
        path = self.paths[dim_entity]
        for ent in self.conds:
            if ent == self.model.root:
                continue
            if ent in path[1:] or self._descendant_or_self(ent, dim_entity):
                return True
        return False

    def _build_spine(self) -> tuple[str, list[Any], dict]:
        """Returns (cte_sql, params, info). info maps:
        'k' -> [key column names per dim], 'pk' -> {entity: pk output name}
        for root/shared nodes plus per-dim private LCA columns
        ('pl', (dim_idx, lca_entity)) -> output name."""
        dims = self.query.dimensions
        dim_paths = [self.paths[d.entity] for d in dims]
        usage: dict[str, int] = {}
        for p in dim_paths:
            for n in p[1:]:
                usage[n] = usage.get(n, 0) + 1
        shared = {n for n, c in usage.items() if c > 1}

        # metric LCAs that the spine must expose as pk columns
        lca_needed: dict[str, list[str]] = {}  # dim idx -> [lca entities]
        for i, d in enumerate(dims):
            for mt in self.query.metrics:
                if mt.entity == d.entity or self._descendant_or_self(mt.entity, d.entity):
                    continue  # comparable: key match, no pk needed
                if self._descendant_or_self(d.entity, mt.entity):
                    continue  # dim below metric: key match
                lca_needed.setdefault(i, [])
                lca = self._lca(d.entity, mt.entity)
                if lca not in lca_needed[i]:
                    lca_needed[i].append(lca)

        info = {"k": [f"k{i}" for i in range(len(dims))], "pk": {}, "pl": {}}
        params: list[Any] = []

        # skeleton: root + shared nodes (topo order), LEFT JOINed
        joined: dict[str, str] = {self.model.root: "t0"}
        from_sql = f"{_qident(self.model.entities[self.model.root].table)} {_qident('t0')}"
        skel_order = [n for n in self.paths[dims[0].entity] if n in shared]
        for p in dim_paths[1:]:
            for n in p:
                if n in shared and n not in skel_order:
                    skel_order.append(n)
        # ensure parents come before children
        skel_order.sort(key=lambda n: len(self.paths[n]))
        for k, n in enumerate(skel_order):
            alias = f"s{k}"
            e = self.model.entities[n]
            p_alias = joined[e.parent]
            from_sql += (
                f" LEFT JOIN {_qident(e.table)} {_qident(alias)}"
                f" ON {_qident(alias)}.{_qident(e.fk)}"
                f" = {_qident(p_alias)}.{_qident(self.model.entities[e.parent].pk)}"
            )
            joined[n] = alias

        info["pk"][self.model.root] = "pk_root"
        select = [f"{_qident('t0')}.{_qident(self.model.entities[self.model.root].pk)} AS {_qident('pk_root')}"]
        for n in skel_order:
            out = f"pk_{n}"
            info["pk"][n] = out
            select.append(f"{_qident(joined[n])}.{_qident(self.model.entities[n].pk)} AS {_qident(out)}")

        # per-dimension dedup derived tables
        where_parts: list[str] = []
        for i, d in enumerate(dims):
            ent = d.entity
            col = self.model.entities[ent].fields[d.field]
            if ent == self.model.root or ent in shared:
                src = joined[ent]
                select.append(f"{_qident(src)}.{_qident(col)} AS {_qident(info['k'][i])}")
                if self._dim_required(ent):
                    where_parts.append(
                        f"{_qident(src)}.{_qident(self.model.entities[ent].pk)} IS NOT NULL"
                    )
                continue
            dt_sql, dt_params, dt_info = self._dim_dt(i, d, shared, lca_needed.get(i, []))
            params.extend(dt_params)
            alias = f"dt{i}"
            on = [f"{_qident(alias)}.{_qident('p_root')} = {_qident('t0')}.{_qident(self.model.entities[self.model.root].pk)}"]
            for n in dim_paths[i][1:]:
                if n in shared:
                    on.append(
                        f"{_qident(alias)}.{_qident('p_' + n)}"
                        f" = {_qident(joined[n])}.{_qident(self.model.entities[n].pk)}"
                    )
            from_sql += f" LEFT JOIN ({dt_sql}) {_qident(alias)} ON {' AND '.join(on)}"
            select.append(f"{_qident(alias)}.{_qident('k')} AS {_qident(info['k'][i])}")
            for lca in dt_info["pl"]:
                out = f"pl_{i}_{lca}"
                info["pl"][(i, lca)] = out
                select.append(f"{_qident(alias)}.{_qident('pl_' + lca)} AS {_qident(out)}")
            if self._dim_required(ent):
                where_parts.append(f"{_qident(alias)}.{_qident('xpk')} IS NOT NULL")

        # LCAs living on root/shared nodes get spine pk outputs
        for i, lcas in lca_needed.items():
            for lca in lcas:
                if (i, lca) not in info["pl"]:
                    info["pl"][(i, lca)] = info["pk"][lca]

        filt, filt_params = self._filter_clauses(joined)
        where_parts.extend(filt)
        params.extend(filt_params)

        sql = f"SELECT {', '.join(select)} FROM {from_sql}"
        if where_parts:
            sql += f" WHERE {' AND '.join(where_parts)}"
        return sql, params, info

    def _dim_dt(self, idx: int, dim: Dimension, shared: set[str], lcas: list[str]):
        """Derived table of distinct (root pk, shared-node pks, private LCA
        pks, dim key) tuples for one dimension entity."""
        ent = dim.entity
        path = self.paths[ent]
        alias = {n: f"d{k}" for k, n in enumerate(path)}
        joined = dict(alias)
        params: list[Any] = []
        from_sql = f"{_qident(self.model.entities[path[0]].table)} {_qident(alias[path[0]])}"
        for n in path[1:]:
            e = self.model.entities[n]
            p = e.parent
            from_sql += (
                f" JOIN {_qident(e.table)} {_qident(alias[n])}"
                f" ON {_qident(alias[n])}.{_qident(e.fk)}"
                f" = {_qident(alias[p])}.{_qident(self.model.entities[p].pk)}"
            )
        root = self.model.root
        select = [
            f"{_qident(alias[root])}.{_qident(self.model.entities[root].pk)} AS {_qident('p_root')}",
            f"1 AS {_qident('xpk')}",
        ]
        for n in path[1:]:
            if n in shared:
                select.append(
                    f"{_qident(alias[n])}.{_qident(self.model.entities[n].pk)} AS {_qident('p_' + n)}"
                )
        pl_out = []
        for lca in lcas:
            if lca != root and lca not in shared:
                pl_out.append(lca)
                select.append(
                    f"{_qident(alias[lca])}.{_qident(self.model.entities[lca].pk)} AS {_qident('pl_' + lca)}"
                )
        col = self.model.entities[ent].fields[dim.field]
        select.append(f"{_qident(alias[ent])}.{_qident(col)} AS {_qident('k')}")
        filt, filt_params = self._filter_clauses(joined)
        params.extend(filt_params)
        sql = f"SELECT DISTINCT {', '.join(select)} FROM {from_sql}"
        if filt:
            sql += f" WHERE {' AND '.join(filt)}"
        return sql, params, {"pl": pl_out}

    # ---------- metric derived tables ----------

    def _metric_dt(self, j: int, mt: Metric, info) -> tuple[str, list[Any]]:
        dims = self.query.dimensions
        mrows_sql, mrows_params, mcols = self._metric_rows(mt)
        params: list[Any] = []
        if not dims:
            if mt.agg == "count":
                agg = f"COUNT({_qident('mr')}.{_qident('mpk')})"
            else:
                agg = f"{mt.agg.upper()}({_qident('mr')}.{_qident('mval')})"
            sql = f"SELECT {agg} AS {_qident('v')} FROM ({mrows_sql}) {_qident('mr')}"
            params.extend(mrows_params)
            return sql, params

        keys = info["k"]
        key_sel = ", ".join(
            f"{_qident('s')}.{_qident(k)} AS {_qident(k)}" for k in keys
        )
        on = [f"{_qident('mr')}.{_qident('p_root')} = {_qident('s')}.{_qident('pk_root')}"]
        for i, d in enumerate(dims):
            if ("dk", i) in mcols:
                on.append(
                    f"{_qident('mr')}.{_qident('dk' + str(i))}"
                    f" IS {_qident('s')}.{_qident(keys[i])}"
                )
            else:
                lca = self._lca(d.entity, mt.entity)
                on.append(
                    f"{_qident('mr')}.{_qident('pl' + str(i))}"
                    f" = {_qident('s')}.{_qident(info['pl'][(i, lca)])}"
                )
        join = (
            f"{_qident('spine')} {_qident('s')}"
            f" JOIN ({mrows_sql}) {_qident('mr')} ON {' AND '.join(on)}"
        )
        group = ", ".join(_qident(k) for k in keys)
        if mt.agg == "count":
            sql = (
                f"SELECT {key_sel}, COUNT(DISTINCT {_qident('mr')}.{_qident('mpk')}) AS {_qident('v')}"
                f" FROM {join} GROUP BY {group}"
            )
        else:
            inner = (
                f"SELECT DISTINCT {key_sel}, {_qident('mr')}.{_qident('mpk')} AS {_qident('mpk')},"
                f" {_qident('mr')}.{_qident('mval')} AS {_qident('mval')}"
                f" FROM {join}"
            )
            sql = (
                f"SELECT {', '.join(_qident(k) for k in keys)},"
                f" {mt.agg.upper()}({_qident('mval')}) AS {_qident('v')}"
                f" FROM ({inner}) GROUP BY {group}"
            )
        params.extend(mrows_params)
        return sql, params

    def _metric_rows(self, mt: Metric) -> tuple[str, list[Any], dict]:
        """One row per (deduplicated) metric-entity record, carrying its root
        pk, LCA anchor pks for incomparable dims, and key values for
        comparable dims."""
        dims = self.query.dimensions
        M = mt.entity
        path_m = self.paths[M]
        below = [i for i, d in enumerate(dims)
                 if d.entity != M and self._descendant_or_self(d.entity, M)]
        onpath = [i for i, d in enumerate(dims) if d.entity in path_m]
        incomp = [i for i in range(len(dims)) if i not in below and i not in onpath]

        alias = {n: f"m{k}" for k, n in enumerate(path_m)}
        from_sql = (
            f"{_qident(self.model.entities[path_m[0]].table)} {_qident(alias[path_m[0]])}"
        )
        for n in path_m[1:]:
            e = self.model.entities[n]
            p = e.parent
            from_sql += (
                f" JOIN {_qident(e.table)} {_qident(alias[n])}"
                f" ON {_qident(alias[n])}.{_qident(e.fk)}"
                f" = {_qident(alias[p])}.{_qident(self.model.entities[p].pk)}"
            )
        # below-metric dimension entities: LEFT JOIN tree from M downwards
        extra: list[str] = []
        for i in below:
            for n in self.paths[dims[i].entity]:
                if n not in alias and n not in extra:
                    extra.append(n)
        extra.sort(key=lambda n: len(self.paths[n]))
        for n in extra:
            alias[n] = f"m{len(alias)}"
            e = self.model.entities[n]
            p = e.parent
            from_sql += (
                f" LEFT JOIN {_qident(e.table)} {_qident(alias[n])}"
                f" ON {_qident(alias[n])}.{_qident(e.fk)}"
                f" = {_qident(alias[p])}.{_qident(self.model.entities[p].pk)}"
            )

        root = self.model.root
        select = [
            f"{_qident(alias[root])}.{_qident(self.model.entities[root].pk)} AS {_qident('p_root')}"
        ]
        mcols = {}
        for i in incomp:
            lca = self._lca(dims[i].entity, M)
            select.append(
                f"{_qident(alias[lca])}.{_qident(self.model.entities[lca].pk)} AS {_qident('pl' + str(i))}"
            )
            mcols[("pl", i)] = True
        for i in onpath + below:
            ent = dims[i].entity
            col = self.model.entities[ent].fields[dims[i].field]
            select.append(
                f"{_qident(alias[ent])}.{_qident(col)} AS {_qident('dk' + str(i))}"
            )
            mcols[("dk", i)] = True
        select.append(
            f"{_qident(alias[M])}.{_qident(self.model.entities[M].pk)} AS {_qident('mpk')}"
        )
        if mt.agg != "count":
            col = self.model.entities[M].fields[mt.field]
            select.append(f"{_qident(alias[M])}.{_qident(col)} AS {_qident('mval')}")

        filt, filt_params = self._filter_clauses(alias)
        sql = f"SELECT DISTINCT {', '.join(select)} FROM {from_sql}"
        if filt:
            sql += f" WHERE {' AND '.join(filt)}"
        return sql, filt_params, mcols


def _dim_name(d: Dimension) -> str:
    return d.alias or f"{d.entity}.{d.field}"


def _metric_name(mt: Metric) -> str:
    if mt.alias:
        return mt.alias
    if mt.agg == "count":
        return f"count({mt.entity})"
    return f"{mt.agg}({mt.entity}.{mt.field})"


def compile(model: Model, query: Query) -> tuple[str, list[Any]]:
    """Compile a structured query into (sql, params)."""
    c = _Compiler(model, query)
    dims = query.dimensions
    params: list[Any] = []
    prefix = ""
    if dims:
        spine_sql, spine_params, info = c._build_spine()
        params.extend(spine_params)
        prefix = f"WITH {_qident('spine')} AS ({spine_sql}) "
        keys = ", ".join(_qident(k) for k in info["k"])
        outer_from = f"(SELECT DISTINCT {keys} FROM {_qident('spine')}) {_qident('s')}"
        selects = [
            f"{_qident('s')}.{_qident(k)} AS {_qident(_dim_name(d))}"
            for k, d in zip(info["k"], dims)
        ]
    else:
        info = None
        outer_from = f"(SELECT 1) {_qident('s')}"
        selects = []
    for j, mt in enumerate(query.metrics):
        dt_sql, dt_params = c._metric_dt(j, mt, info)
        params.extend(dt_params)
        alias = f"md{j}"
        if dims:
            on = " AND ".join(
                f"{_qident(alias)}.{_qident(k)} IS {_qident('s')}.{_qident(k)}"
                for k in info["k"]
            )
        else:
            on = "1=1"
        outer_from += f" LEFT JOIN ({dt_sql}) {_qident(alias)} ON {on}"
        v = f"{_qident(alias)}.{_qident('v')}"
        if mt.agg == "count":
            v = f"COALESCE({v}, 0)"
        selects.append(f"{v} AS {_qident(_metric_name(mt))}")
    if not selects:
        selects = ["1"]
    sql = f"{prefix}SELECT {', '.join(selects)} FROM {outer_from}"
    return sql, params


def execute(conn: sqlite3.Connection, model: Model, query: Query) -> list[dict[str, Any]]:
    """Compile and run the query, returning one dict per result row."""
    sql, params = compile(model, query)
    names = [_dim_name(d) for d in query.dimensions]
    names += [_metric_name(mt) for mt in query.metrics]
    cur = conn.execute(sql, params)
    return [dict(zip(names, row)) for row in cur.fetchall()]
