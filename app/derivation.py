"""地层关系推导引擎（纯 Python，无外部依赖）。

语义规则：
- 同一期（same_period）：对称、可传递，构成等价类（并查集）。
- 早于（earlier_than）：沿“同一期 + 早于”混合路径推导。
  若 x ~ a（同一期链）、a < b（早于）、b ~ y（同一期链），则 x 早于 y。
- 约束（对整批应用后的最终关系核验）：
  1. 组内早于禁止：同一等价类内不得存在（直接或间接的）早于关系；
  2. 早于闭环禁止：等价类商图上的早于关系必须是有向无环图（DAG）。

路径选取：证据数最少者优先；并列时按证据编号序列字典序最小者。
"""
from __future__ import annotations

import heapq
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

SAME = "same_period"
EARLIER = "earlier_than"

RELATION_LABELS = {
    "same_period": "同一期",
    "earlier_than": "早于",
    "later_than": "晚于",
    "undetermined": "未定",
}


@dataclass(frozen=True)
class Assertion:
    """一条生效中的断言。relation=earlier_than 表示 a 早于 b。"""

    evidence_id: str
    a: str
    b: str
    relation: str


class UnionFind:
    def __init__(self) -> None:
        self.parent: Dict[str, str] = {}
        self.rank: Dict[str, int] = {}

    def find(self, x: str) -> str:
        if x not in self.parent:
            self.parent[x] = x
            self.rank[x] = 0
            return x
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        # 路径压缩
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, x: str, y: str) -> str:
        rx, ry = self.find(x), self.find(y)
        if rx == ry:
            return rx
        if self.rank[rx] < self.rank[ry]:
            rx, ry = ry, rx
        self.parent[ry] = rx
        if self.rank[rx] == self.rank[ry]:
            self.rank[rx] += 1
        return rx


def _build_adj(assertions: Iterable[Assertion], kinds: frozenset) -> Dict[str, List[Tuple[str, str]]]:
    """构建邻接表：同一期为无向边，早于是 a -> b 的有向边。边权为证据编号。"""
    adj: Dict[str, List[Tuple[str, str]]] = {}
    for a in assertions:
        if a.relation == SAME and SAME in kinds:
            adj.setdefault(a.a, []).append((a.b, a.evidence_id))
            adj.setdefault(a.b, []).append((a.a, a.evidence_id))
        elif a.relation == EARLIER and EARLIER in kinds:
            adj.setdefault(a.a, []).append((a.b, a.evidence_id))
    return adj


def best_path(adj: Dict[str, List[Tuple[str, str]]], start: str, goal: str) -> Optional[Tuple[str, ...]]:
    """Dijkstra，代价为 (证据数, 证据编号元组)，即先最少证据、再按编号字典序。

    返回证据编号序列（推导顺序），不可达返回 None。
    """
    if start == goal:
        return ()
    best: Dict[str, Tuple[int, Tuple[str, ...]]] = {start: (0, ())}
    pq: List[Tuple[Tuple[int, Tuple[str, ...]], str]] = [((0, ()), start)]
    while pq:
        cost, node = heapq.heappop(pq)
        if cost != best.get(node):
            continue
        if node == goal:
            return cost[1]
        for nxt, eid in adj.get(node, ()):
            ncost = (cost[0] + 1, cost[1] + (eid,))
            old = best.get(nxt)
            if old is None or ncost < old:
                best[nxt] = ncost
                heapq.heappush(pq, (ncost, nxt))
    return None


def _find_cycle(edges: Dict[str, List[Tuple[str, Assertion]]]) -> Optional[List[Assertion]]:
    """在等价类商图上找一个有向环，返回构成环的“早于”断言序列（沿环方向）。"""
    color: Dict[str, int] = {}  # 0/缺省=未访问 1=在栈上 2=已完成

    def ordered(n: str) -> List[Tuple[str, Assertion]]:
        return sorted(edges.get(n, []), key=lambda t: (t[1].evidence_id, t[0]))

    for start in sorted(edges):
        if color.get(start):
            continue
        color[start] = 1
        path_nodes: List[str] = [start]
        path_edges: List[Assertion] = []  # path_edges[i]: path_nodes[i] -> path_nodes[i+1]
        stack = [(start, iter(ordered(start)))]
        while stack:
            node, it = stack[-1]
            advanced = False
            for v, a in it:
                c = color.get(v, 0)
                if c == 0:
                    color[v] = 1
                    path_nodes.append(v)
                    path_edges.append(a)
                    stack.append((v, iter(ordered(v))))
                    advanced = True
                    break
                if c == 1:
                    idx = path_nodes.index(v)
                    return path_edges[idx:] + [a]
            if not advanced:
                color[node] = 2
                stack.pop()
                path_nodes.pop()
                if path_edges:
                    path_edges.pop()
    return None


class Derivation:
    """对一组生效断言的推导与核验。"""

    def __init__(self, assertions: Sequence[Assertion]) -> None:
        self.assertions: List[Assertion] = sorted(assertions, key=lambda a: a.evidence_id)
        self.uf = UnionFind()
        for a in self.assertions:
            self.uf.find(a.a)
            self.uf.find(a.b)
        for a in self.assertions:
            if a.relation == SAME:
                self.uf.union(a.a, a.b)
        self.same_adj = _build_adj(self.assertions, frozenset({SAME}))
        self.mixed_adj = _build_adj(self.assertions, frozenset({SAME, EARLIER}))

    # ------------------------------------------------------------------ 核验
    def validate(self) -> List[dict]:
        """核验最终关系，返回冲突列表（空列表表示通过）。"""
        conflicts: List[dict] = []

        # 1) 组内早于：早于断言两端落在同一等价类
        for a in self.assertions:
            if a.relation != EARLIER:
                continue
            if self.uf.find(a.a) == self.uf.find(a.b):
                chain = best_path(self.same_adj, a.a, a.b) or ()
                conflicts.append(
                    {
                        "type": "intra_group_earlier",
                        "message": (
                            f"证据 {a.evidence_id} 断言 {a.a} 早于 {a.b}，"
                            f"但同一期证据链表明二者同属一期（组内早于）"
                        ),
                        "evidence_path": list(chain) + [a.evidence_id],
                    }
                )

        # 2) 早于闭环：等价类商图上的有向环
        best_edge: Dict[Tuple[str, str], Assertion] = {}
        for a in self.assertions:
            if a.relation != EARLIER:
                continue
            ra, rb = self.uf.find(a.a), self.uf.find(a.b)
            if ra == rb:
                continue
            key = (ra, rb)
            if key not in best_edge or a.evidence_id < best_edge[key].evidence_id:
                best_edge[key] = a
        edges: Dict[str, List[Tuple[str, Assertion]]] = {}
        for (ra, rb), a in best_edge.items():
            edges.setdefault(ra, []).append((rb, a))

        cycle = _find_cycle(edges)
        if cycle:
            full_path: List[str] = []
            k = len(cycle)
            for i, cur in enumerate(cycle):
                nxt = cycle[(i + 1) % k]
                full_path.append(cur.evidence_id)
                full_path.extend(best_path(self.same_adj, cur.b, nxt.a) or ())
            conflicts.append(
                {
                    "type": "earlier_cycle",
                    "message": "早于关系形成闭环: "
                    + " -> ".join(f"{e.evidence_id}({e.a} 早于 {e.b})" for e in cycle),
                    "evidence_path": full_path,
                }
            )
        return conflicts

    # ------------------------------------------------------------------ 查询
    def relation(self, x: str, y: str) -> Tuple[str, Optional[List[str]]]:
        """返回 (关系, 证据路径)。关系 ∈ same_period / earlier_than / later_than / undetermined。"""
        if x == y:
            return "same_period", []
        if self.uf.find(x) == self.uf.find(y):
            path = best_path(self.same_adj, x, y) or ()
            return "same_period", list(path)
        p = best_path(self.mixed_adj, x, y)
        if p is not None:
            return "earlier_than", list(p)
        p = best_path(self.mixed_adj, y, x)
        if p is not None:
            return "later_than", list(p)
        return "undetermined", None


# ------------------------------------------------------------------ 分期骨架
@dataclass(frozen=True)
class SkeletonEdge:
    """组间早于关系图上的一条**直接**（传递约简后保留）边。

    from_min < to_min 不做要求：方向即时间方向（from 组早于 to 组）；
    evidence 为该组间全部生效的直接早于证据编号（平行断言合并为一条边，按编号排序）。
    """

    from_group: int          # 组序号（组按最小编号排序后从 1 起）
    to_group: int
    from_min: str            # 源组最小编号（稳定标识）
    to_min: str
    evidence: Tuple[str, ...]


@dataclass
class Skeleton:
    """分期骨架：同一期等价组（含孤立地层）+ 组间直接早于关系图（传递约简图）。"""

    groups: Tuple[Tuple[str, ...], ...]  # 每组内地层按编号排序；组间按首元素（最小编号）排序
    edges: Tuple[SkeletonEdge, ...]      # 边按 (源组最小编号, 目标组最小编号) 排序
    omitted_edge_count: int              # 被传递关系省略的组间边数（平行断言只算一条边）


def periodization_skeleton(
    strata: Sequence[str],
    assertions: Sequence[Assertion],
) -> Skeleton:
    """由已登记地层与生效断言构造分期骨架（纯函数，结果顺序稳定）。

    - 同一期（same_period）断言的传递闭包构成等价类；未出现在任何同一期断言中的
      地层各自成组（孤立地层），因此全部已登记地层恰好出现一次；
    - 组间早于图：每条生效“早于”断言按其两端所在组投影为一条商图有向边
      （源组早于目标组），同一组间的多条平行断言合并为一条边；
    - 传递约简：若从源组到目标组存在长度 >= 2 的组间路径（经其他组），则该直接边
      可由传递关系推出，予以省略；只保留“不存在经其他组到达”的直接先后边。
      生效断言集已通过 validate()（无组内早于、商图为 DAG），DAG 的传递约简唯一。
    """
    uf = UnionFind()
    for s in strata:
        uf.find(s)
    for a in assertions:
        uf.find(a.a)
        uf.find(a.b)
    for a in assertions:
        if a.relation == SAME:
            uf.union(a.a, a.b)

    members: Dict[str, List[str]] = defaultdict(list)
    for s in strata:
        members[uf.find(s)].append(s)
    groups_list = [sorted(m) for m in members.values()]
    groups_list.sort(key=lambda g: g[0])
    groups_tuple = tuple(tuple(g) for g in groups_list)

    # 最小地层编号 -> 组序号（从 1 起），组按最小编号排序
    index_by_min: Dict[str, int] = {g[0]: i + 1 for i, g in enumerate(groups_tuple)}
    index_by_root: Dict[str, int] = {uf.find(g[0]): i + 1 for i, g in enumerate(groups_tuple)}

    # 平行断言合并：(源组最小编号, 目标组最小编号) -> 全部生效直接早于证据编号
    raw_edges: Dict[Tuple[str, str], Set[str]] = defaultdict(set)
    for a in assertions:
        if a.relation != EARLIER:
            continue
        ra, rb = uf.find(a.a), uf.find(a.b)
        if ra == rb:
            # 生效断言集理论上已通过 validate()（组内早于是冲突，不可能落库）；
            # 防御性跳过，保证骨架视图在任何输入下只产出组间边。
            continue
        ga, gb = groups_list[index_by_root[ra] - 1], groups_list[index_by_root[rb] - 1]
        raw_edges[(ga[0], gb[0])].add(a.evidence_id)

    # 商图邻接（不含证据），用于“是否存在经其他组到达”的可达性判定
    adj: Dict[str, Set[str]] = {g[0]: set() for g in groups_tuple}
    for (u, v) in raw_edges:
        adj[u].add(v)

    def reachable_via_other(src: str, dst: str) -> bool:
        """从 src 出发是否存在长度 >= 2 的组间路径到达 dst（必须经过中间组）。

        枚举 src 的直接后继（跳过直达 dst 的那条边，且同组间平行断言已合并为一条边），
        再判断该后继能否（经零或多条组间边）到达 dst。商图是 DAG，普通 BFS 即可。
        """
        for first in adj.get(src, ()):  # 第一步：必须经过另一个组
            if first == dst:
                continue
            seen = {first}
            queue = deque([first])
            while queue:
                node = queue.popleft()
                if node == dst:
                    return True
                for nxt in adj.get(node, ()):
                    if nxt not in seen:
                        seen.add(nxt)
                        queue.append(nxt)
        return False

    kept: List[SkeletonEdge] = []
    omitted = 0
    for (umin, vmin), evidence in raw_edges.items():
        if reachable_via_other(umin, vmin):
            omitted += 1
            continue
        kept.append(
            SkeletonEdge(
                from_group=index_by_min[umin],
                to_group=index_by_min[vmin],
                from_min=umin,
                to_min=vmin,
                evidence=tuple(sorted(evidence)),
            )
        )
    kept.sort(key=lambda e: (e.from_min, e.to_min))
    return Skeleton(groups=groups_tuple, edges=tuple(kept), omitted_edge_count=omitted)
