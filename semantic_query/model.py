"""Data structures describing the semantic model and the structured query."""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class Entity:
    """A business entity mapped to one physical table."""

    name: str
    table: str
    pk: str
    fields: Dict[str, str]
    parent: Optional[str] = None
    fk: Optional[str] = None


class Model:
    """A rooted tree of entities."""

    def __init__(self, entities: List[Entity]):
        self.entities: Dict[str, Entity] = {e.name: e for e in entities}
        self.children: Dict[str, List[str]] = {e.name: [] for e in entities}
        roots = []
        for e in entities:
            if e.parent is None:
                roots.append(e.name)
            else:
                self.children[e.parent].append(e.name)
        if len(roots) != 1:
            raise ValueError("model must have exactly one root entity")
        self.root: str = roots[0]

    def entity(self, name: str) -> Entity:
        return self.entities[name]

    def path(self, name: str) -> List[str]:
        """Entity names from the root down to ``name`` (inclusive)."""
        chain = []
        node = name
        while node is not None:
            chain.append(node)
            node = self.entities[node].parent
        chain.reverse()
        if chain[0] != self.root:
            raise ValueError(f"entity {name!r} is not reachable from the root")
        return chain

    def depth(self, name: str) -> int:
        return len(self.path(name)) - 1

    def lca(self, a: str, b: str) -> str:
        """Deepest common ancestor of entities ``a`` and ``b``."""
        pa = self.path(a)
        pb = self.path(b)
        last = pa[0]
        for x, y in zip(pa, pb):
            if x != y:
                break
            last = x
        return last

    def path_below(self, ancestor: str, descendant: str) -> List[str]:
        """Entities strictly below ``ancestor`` down to ``descendant``."""
        pa = self.path(ancestor)
        pd = self.path(descendant)
        if pd[: len(pa)] != pa:
            raise ValueError(f"{ancestor!r} is not an ancestor of {descendant!r}")
        return pd[len(pa):]


@dataclass(frozen=True)
class Dimension:
    entity: str
    field: str


@dataclass(frozen=True)
class Filter:
    entity: str
    field: str
    value: Any  # never None


@dataclass(frozen=True)
class Metric:
    op: str  # "sum" | "count" | "avg"
    entity: str
    field: Optional[str] = None  # None for count
    label: Optional[str] = None


@dataclass
class Query:
    dimensions: List[Dimension] = field(default_factory=list)
    filters: List[Filter] = field(default_factory=list)
    metrics: List[Metric] = field(default_factory=list)
