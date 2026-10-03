"""Compiles a structured query into a single SQL statement with parameters."""

from typing import Dict, List, Optional, Tuple

from .model import Dimension, Filter, Metric, Model, Query


def compile(model: Model, query: Query) -> Tuple[str, List]:
    """Return (sql, params) ready for ``sqlite3`` execution."""
    return _Compiler(model, query).build()


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def metric_labels(query: Query) -> List[str]:
    """Stable, unique output label for every metric in the query."""
    labels = []
    used = set()
    for m in query.metrics:
        base = m.label
        if not base:
            base = f"{m.op}_{m.entity}" + (f"_{m.field}" if m.field else "")
        label = base
        n = 2
        while label in used:
            label = f"{base}_{n}"
            n += 1
        used.add(label)
        labels.append(label)
    return labels


def dimension_labels(query: Query) -> List[str]:
    return [f"{d.entity}.{d.field}" for d in query.dimensions]


class _Compiler:
    def __init__(self, model: Model, query: Query):
        self.model = model
        self.query = query
        self.params: List = []
        self._seq = 0

    def alias(self) -> str:
        self._seq += 1
        return f"t{self._seq}"

    def column(self, entity: str, field: str) -> str:
        e = self.model.entity(entity)
        try:
            return e.fields[field]
        except KeyError:
            raise ValueError(f"unknown field {field!r} on entity {entity!r}")

    # ------------------------------------------------------------------
    # join helpers
    # ------------------------------------------------------------------
    def join_down(self, aliases: Dict[str, str], frm: str, to: str,
                  join: str = "JOIN") -> str:
        """JOIN chain from ``frm`` (alias known) down to ``to``."""
        sql = ""
        prev = frm
        for name in self.model.path_below(frm, to):
            e = self.model.entity(name)
            p = self.model.entity(prev)
            a = self.alias()
            aliases[name] = a
            sql += (f" {join} {_quote(e.table)} {a}"
                    f" ON {a}.{_quote(e.fk)} = {aliases[prev]}.{_quote(p.pk)}")
            prev = name
        return sql

    def ensure_path(self, aliases: Dict[str, str], frm: str, to: str,
                    join: str = "LEFT JOIN") -> str:
        """Like join_down but reuses aliases for already-joined entities."""
        sql = ""
        prev = frm
        for name in self.model.path_below(frm, to):
            if name in aliases:
                prev = name
                continue
            e = self.model.entity(name)
            p = self.model.entity(prev)
            a = self.alias()
            aliases[name] = a
            sql += (f" {join} {_quote(e.table)} {a}"
                    f" ON {a}.{_quote(e.fk)} = {aliases[prev]}.{_quote(p.pk)}")
            prev = name
        return sql

    # ------------------------------------------------------------------
    # filter handling
    # ------------------------------------------------------------------
    def split_filters(self, scope_path: List[str],
                      sub_scopes: List[Tuple[str, List[str]]],
                      filters: List[Filter]):
        """Assign each filter to this scope or to a nested dim subquery.

        ``sub_scopes`` entries are (key_entity, [dim_entity, ...]): a filter
        belongs to a subquery when its deepest shared ancestor with one of
        the dimension entities lies strictly below the correlation key.
        """
        own: List[Filter] = []
        subs: List[List[Filter]] = [[] for _ in sub_scopes]
        for f in filters:
            if f.entity in scope_path:
                own.append(f)
                continue
            placed = False
            for i, (key, dim_entities) in enumerate(sub_scopes):
                for dim_entity in dim_entities:
                    l = self.model.lca(f.entity, dim_entity)
                    if l != key and self.model.depth(l) > self.model.depth(key):
                        subs[i].append(f)
                        placed = True
                        break
                if placed:
                    break
            if not placed:
                own.append(f)
        return own, subs

    def render_own_filters(self, aliases: Dict[str, str], scope_path: List[str],
                           filters: List[Filter]) -> List[str]:
        by_entity: Dict[str, List[Filter]] = {}
        for f in filters:
            by_entity.setdefault(f.entity, []).append(f)
        conds = []
        existential: Dict[str, List[Filter]] = {}
        for entity, fs in by_entity.items():
            if entity in aliases:
                a = aliases[entity]
                for f in fs:
                    conds.append(f"{a}.{_quote(self.column(entity, f.field))} = ?")
                    self.params.append(f.value)
            else:
                existential[entity] = fs
        # Existential filter entities that are ancestor/descendant related
        # must be satisfied along a single record chain: group them into one
        # EXISTS per comparability component.
        groups: List[List[str]] = []
        for entity in existential:
            for g in groups:
                if any(self._comparable(entity, other) for other in g):
                    g.append(entity)
                    break
            else:
                groups.append([entity])
        for g in groups:
            conds.append(self.render_exists(aliases, g, existential))
        return conds

    def _comparable(self, a: str, b: str) -> bool:
        path_b = self.model.path(b)
        return a in path_b or b in self.model.path(a)

    def render_exists(self, aliases: Dict[str, str], entities: List[str],
                      by_entity: Dict[str, List[Filter]]) -> str:
        model = self.model
        deepest = max(entities, key=model.depth)
        shallowest = min(entities, key=model.depth)
        anchor = None
        for name in reversed(model.path(shallowest)):
            if name in aliases:
                anchor = name
                break
        chain = model.path_below(anchor, deepest)
        first = self.model.entity(chain[0])
        a0 = self.alias()
        sql = f"EXISTS (SELECT 1 FROM {_quote(first.table)} {a0}"
        conds = [f"{a0}.{_quote(first.fk)} = "
                 f"{aliases[anchor]}.{_quote(model.entity(anchor).pk)}"]
        local = {chain[0]: a0}
        prev = chain[0]
        for name in chain[1:]:
            e = model.entity(name)
            p = model.entity(prev)
            a = self.alias()
            local[name] = a
            sql += (f" JOIN {_quote(e.table)} {a}"
                    f" ON {a}.{_quote(e.fk)} = {local[prev]}.{_quote(p.pk)}")
            prev = name
        for entity in entities:
            for f in by_entity[entity]:
                conds.append(
                    f"{local[entity]}.{_quote(self.column(entity, f.field))} = ?")
                self.params.append(f.value)
        return sql + " WHERE " + " AND ".join(conds) + ")"

    # ------------------------------------------------------------------
    # dimension sources (pre-deduplicated, correlated to a key entity)
    # ------------------------------------------------------------------
    def render_dim_source(self, key: str, dims: List[Dimension],
                          filters: List[Filter]) -> Tuple[str, bool]:
        """Distinct (key pk, dim values...) tuples below ``key``.

        Returns (sql, filtered); ``filtered`` tells the caller to use an
        INNER join so records failing the filters drop out entirely.
        """
        key_e = self.model.entity(key)
        ka = self.alias()
        aliases = {key: ka}
        from_sql = _quote(key_e.table) + " " + ka
        value_cols = []
        for pos, dim in enumerate(dims):
            from_sql += self.ensure_path(aliases, key, dim.entity)
            da = aliases[dim.entity]
            value_cols.append(
                f"{da}.{_quote(self.column(dim.entity, dim.field))} AS v{pos}")
        sql = (f"SELECT DISTINCT {ka}.{_quote(key_e.pk)} AS k, "
               f"{', '.join(value_cols)} FROM {from_sql}")
        scope_path = list(aliases)
        conds = self.render_own_filters(aliases, scope_path, filters)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        return sql, bool(filters)

    def group_off_path_dims(self, dims: List[Dimension],
                            available: Dict[str, str],
                            key_of) -> List[Tuple[List[int], str, List[Dimension]]]:
        """Group off-path dimensions that share correlated path segments.

        Two dimensions keyed at the same entity must be rendered by a single
        subquery when their paths below the key overlap; otherwise their
        values would be wrongly cross-producted.
        """
        off = [(i, d) for i, d in enumerate(dims) if d.entity not in available]
        groups: List[Tuple[List[int], str, List[Dimension]]] = []
        for i, d in off:
            key = key_of(d)
            chain = set(self.model.path_below(key, d.entity))
            merged = False
            for idxs, g_key, g_dims in groups:
                if g_key != key:
                    continue
                shared = set()
                for g in g_dims:
                    shared |= set(self.model.path_below(key, g.entity))
                if chain & shared:
                    idxs.append(i)
                    g_dims.append(d)
                    merged = True
                    break
            if not merged:
                groups.append(([i], key, [d]))
        return groups

    # ------------------------------------------------------------------
    # metric subquery
    # ------------------------------------------------------------------
    def render_metric(self, metric: Metric, dims: List[Dimension],
                      filters: List[Filter]) -> str:
        model = self.model
        entity = metric.entity
        path = model.path(entity)
        root = path[0]
        ra = self.alias()
        aliases = {root: ra}
        from_sql = _quote(model.entity(root).table) + " " + ra
        from_sql += self.join_down(aliases, root, entity, join="LEFT JOIN")
        e_alias = aliases[entity]
        e_pk = model.entity(entity).pk

        sub_scopes = self.group_off_path_dims(
            dims, aliases, lambda d: model.lca(d.entity, entity))
        own_filters, sub_filters = self.split_filters(
            path, [(k, [g.entity for g in gs]) for _, k, gs in sub_scopes],
            filters)

        dim_exprs: Dict[int, str] = {}
        for i, d in enumerate(dims):
            if d.entity in aliases:
                dim_exprs[i] = f"{aliases[d.entity]}.{_quote(self.column(d.entity, d.field))}"
        for i, (idxs, key, group_dims) in enumerate(sub_scopes):
            dd_sql, _ = self.render_dim_source(key, group_dims, sub_filters[i])
            ja = self.alias()
            from_sql += (f" JOIN ({dd_sql}) {ja} ON {ja}.k = "
                         f"{aliases[key]}.{_quote(model.entity(key).pk)}")
            for pos, di in enumerate(idxs):
                dim_exprs[di] = f"{ja}.v{pos}"

        conds = [f"{e_alias}.{_quote(e_pk)} IS NOT NULL"]
        conds += self.render_own_filters(aliases, path, own_filters)

        select = [f"{e_alias}.{_quote(e_pk)} AS pk"]
        if metric.op in ("sum", "avg"):
            select.append(f"{e_alias}.{_quote(self.column(entity, metric.field))} AS v")
        for i in range(len(dims)):
            select.append(f"{dim_exprs[i]} AS d{i}")
        inner = (f"SELECT DISTINCT {', '.join(select)} FROM {from_sql} "
                 f"WHERE {' AND '.join(conds)}")

        if metric.op == "count":
            agg = "COUNT(*)"
        elif metric.op == "sum":
            agg = "SUM(v)"
        elif metric.op == "avg":
            agg = "AVG(v)"
        else:
            raise ValueError(f"unsupported metric op {metric.op!r}")
        if dims:
            keys = ", ".join(f"d{i}" for i in range(len(dims)))
            return f"SELECT {keys}, {agg} AS val FROM ({inner}) GROUP BY {keys}"
        return f"SELECT {agg} AS val FROM ({inner})"

    # ------------------------------------------------------------------
    # dimension spine: the universe of groups after filtering
    # ------------------------------------------------------------------
    def render_spine(self, dims: List[Dimension], filters: List[Filter]) -> str:
        model = self.model
        prefix = model.path(dims[0].entity)
        for d in dims[1:]:
            p = model.path(d.entity)
            n = 0
            while n < len(prefix) and n < len(p) and prefix[n] == p[n]:
                n += 1
            prefix = prefix[:n]

        root = prefix[0]
        ra = self.alias()
        aliases = {root: ra}
        from_sql = _quote(model.entity(root).table) + " " + ra
        if len(prefix) > 1:
            from_sql += self.join_down(aliases, root, prefix[-1], join="LEFT JOIN")

        sub_scopes = self.group_off_path_dims(
            dims, aliases, lambda d: prefix[-1])
        own_filters, sub_filters = self.split_filters(
            prefix, [(k, [g.entity for g in gs]) for _, k, gs in sub_scopes],
            filters)

        dim_exprs: Dict[int, str] = {}
        for i, d in enumerate(dims):
            if d.entity in aliases:
                dim_exprs[i] = f"{aliases[d.entity]}.{_quote(self.column(d.entity, d.field))}"
        for i, (idxs, key, group_dims) in enumerate(sub_scopes):
            sd_sql, filtered = self.render_dim_source(key, group_dims,
                                                      sub_filters[i])
            ja = self.alias()
            join = "JOIN" if filtered else "LEFT JOIN"
            from_sql += (f" {join} ({sd_sql}) {ja} ON {ja}.k = "
                         f"{aliases[key]}.{_quote(model.entity(key).pk)}")
            for pos, di in enumerate(idxs):
                dim_exprs[di] = f"{ja}.v{pos}"

        select = ", ".join(f"{dim_exprs[i]} AS d{i}" for i in range(len(dims)))
        sql = f"SELECT DISTINCT {select} FROM {from_sql}"
        conds = self.render_own_filters(aliases, prefix, own_filters)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        return sql

    # ------------------------------------------------------------------
    # top level
    # ------------------------------------------------------------------
    def build(self) -> Tuple[str, List]:
        query = self.query
        dims = query.dimensions
        m_labels = metric_labels(query)
        d_labels = dimension_labels(query)
        if len(dims) > 2:
            raise ValueError("at most 2 grouping dimensions are supported")

        if dims:
            spine = self.render_spine(dims, query.filters)
            select = [f"s.d{i} AS {_quote(d_labels[i])}" for i in range(len(dims))]
            joins = ""
            for mi, m in enumerate(query.metrics):
                sub = self.render_metric(m, dims, query.filters)
                ma = f"m{mi}"
                on = " AND ".join(f"s.d{i} IS {ma}.d{i}" for i in range(len(dims)))
                joins += f" LEFT JOIN ({sub}) {ma} ON {on}"
                expr = f"{ma}.val"
                if m.op == "count":
                    expr = f"COALESCE({ma}.val, 0)"
                select.append(f"{expr} AS {_quote(m_labels[mi])}")
            sql = f"SELECT {', '.join(select)} FROM ({spine}) s{joins}"
        else:
            parts = []
            for mi, m in enumerate(query.metrics):
                sub = self.render_metric(m, [], query.filters)
                expr = f"({sub})"
                if m.op == "count":
                    expr = f"COALESCE(({sub}), 0)"
                parts.append(f"{expr} AS {_quote(m_labels[mi])}")
            sql = "SELECT " + (", ".join(parts) if parts else "NULL")
        return sql, self.params
