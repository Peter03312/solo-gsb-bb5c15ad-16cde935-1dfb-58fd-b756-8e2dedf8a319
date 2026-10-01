"""地层关系推导引擎（纯 Python，无外部依赖）。

语义规则：
- 同一期（same_period）：对称、可传递，构成等价类（并查集）。
- 早于（earlier_than）：沿“同一期 + 早于”混合路径推导。
  若 x ~ a（同一期链）、a < b（早于）、b ~ y（同一期链），则 x 早于 y。
- 约束（对整批应用后的最终关系核验）：
  1. 组内早于禁止：同一等价类内不得存在（直接或间接的）早于关系；
  2. 早于闭环禁止：等价类商图上的早于关系必须是有向无环图（DAG）。

路径选取：证据数最少者优先；并列时按证据编号序列字典序最小者。

另含只读“分期试排”（trial arrangement）：给定总期数 K（1～64）与可选的
地层期位锚点，在“同一期等价组占同一期、早于关系源组期位严格小于目标组、
未锚定组可占任一期（含与无涉组同期）”的约束下，判定可行排布是否存在；
可行时给出每组在**所有**可行排布中的最早 / 最晚期位（而不是某一套任意排布），
不可行时区分同组锚点矛盾与先后链不相容（对总期数或锚点）并附锚点与证据路径。
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


# ------------------------------------------------------------------ 分期试排
@dataclass(frozen=True)
class ArrangementGroup:
    """试排视角下的一个同一期等价组（组序与分期骨架一致：按组内最小编号排序）。"""

    group: int
    min_stratum: str
    strata: Tuple[str, ...]
    anchored: bool
    anchor_position: Optional[int]   # 锚定组为其期位（同组所有锚点已校验同期），否则 None
    anchor_strata: Tuple[str, ...]   # 该组携带锚点的地层编号（按编号排序）


@dataclass
class ArrangementResult:
    """分期试排结果。

    - feasible=True：conflicts 恒为空，earliest/latest 给出每组在所有可行排布中的
      最早、最晚期位（锚定组二者相等）；feasible=False：groups/earliest/latest 均
      为空，conflicts 为全部诊断（同组锚点矛盾与先后链不相容各自列出，不返回部分排布）。
    """

    feasible: bool
    total_periods: int
    groups: Tuple[ArrangementGroup, ...]
    earliest: Dict[str, int]   # min_stratum -> 最早期位
    latest: Dict[str, int]     # min_stratum -> 最晚期位
    conflicts: Tuple[dict, ...]


def _quotient_dag(
    groups_tuple: Sequence[Sequence[str]],
    uf: "UnionFind",
    assertions: Sequence[Assertion],
) -> Tuple[List[Set[int]], Dict[Tuple[int, int], List[str]], Dict[str, int]]:
    """构造等价组商图：平行早于断言合并为一条边。

    返回 (组序号(0 起)的后继集合, 每条组间边 -> 全部直接早于证据编号(排序),
    地层 -> 组序号)。生效断言集已通过 validate()（商图必为 DAG）。
    """
    n = len(groups_tuple)
    group_of: Dict[str, int] = {}
    for i, g in enumerate(groups_tuple):
        for s in g:
            group_of[s] = i

    succ: List[Set[int]] = [set() for _ in range(n)]
    edge_evidence: Dict[Tuple[int, int], Set[str]] = defaultdict(set)
    for a in assertions:
        if a.relation != EARLIER:
            continue
        ga, gb = group_of.get(a.a), group_of.get(a.b)
        if ga is None or gb is None or ga == gb:
            continue
        succ[ga].add(gb)
        edge_evidence[(ga, gb)].add(a.evidence_id)
    edges = {k: sorted(v) for k, v in edge_evidence.items()}
    return succ, edges, group_of


def _chain_evidence_path(
    mixed_adj: Dict[str, List[Tuple[str, str]]],
    chain: Sequence[int],
    groups_tuple: Sequence[Sequence[str]],
    anchor_by_group: Dict[int, Dict[int, List[str]]],
) -> List[str]:
    """把组链（源 -> 目标，严格先后）展开为地层级最优证据路径。

    相邻组对 (u, v)：从 u 组的“锚点地层（取编号最小者），否则组最小编号”出发，
    到 v 组对应代表地层，取 best_path（最少证据、并列证据编号序列字典序最小）。
    """
    def rep(idx: int) -> str:
        anchors = anchor_by_group.get(idx)
        if anchors:
            # 取该组最短期位下编号最小的锚点地层（同组多期位只出现在锚点矛盾情形）
            return min(anchors[min(anchors)])
        return groups_tuple[idx][0]

    path: List[str] = []
    for u, v in zip(chain, chain[1:]):
        part = best_path(mixed_adj, rep(u), rep(v)) or ()
        path.extend(part)
    return path


def trial_arrangement(
    strata: Sequence[str],
    assertions: Sequence[Assertion],
    total_periods: int,
    anchors: Sequence[Tuple[str, int]],
) -> ArrangementResult:
    """分期试排（纯函数）。

    约束：同一期等价组占同一期；早于关系（含沿同一期链推导出的）源组期位严格
    小于目标组；未锚定组可占 1..K 任一期（无涉组可同期）。锚点 (stratum, position)
    表示该地层必须位于第 position 期；同组锚点必须同期，否则为同组锚点矛盾。

    可行性判定用商 DAG 上的最长路径界：
        LB[g] = max(锚定下界 1 或固定期位, max(LB[p] + 1 for p 前驱))
        UB[g] = min(锚定上界 K 或固定期位, min(UB[s] - 1 for s 后继))
    存在可行排布 iff 对所有组 LB[g] <= UB[g]。且 LB 恰为该组在所有可行排布中的
    最早期位、UB 恰为最晚期位（把每组放在 LB（或 UB）上本身就是一套可行排布）。

    不可行诊断分两类：
    - anchor_position_conflict（同组锚点矛盾）：同一等价组被锚到不同期位，
      列出全部锚点（地层 + 期位）；
    - chain_incompatible（先后链与期数或锚点不相容）：对每个 LB 越界的组给出
      一条“最长先后链”证据路径——链顶端的锚点（或第 1 期下界）到该组所需的
      最早期位已超过其固定期位（anchor_too_early）或总期数（chain_exceeds_periods）。
    """
    k = total_periods

    # 1) 等价组（与分期骨架完全一致的构造与排序）
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
    n = len(groups_list)
    mins = [g[0] for g in groups_list]

    # 2) 锚点归集：组 -> {期位 -> [地层]}（调用方已保证地层存在且锚点不重复）
    group_of_stratum = {s: i for i, g in enumerate(groups_list) for s in g}
    anchor_by_group: Dict[int, Dict[int, List[str]]] = {}
    for stratum_id, position in anchors:
        gi = group_of_stratum[stratum_id]
        anchor_by_group.setdefault(gi, {}).setdefault(position, []).append(stratum_id)
    for pos_map in anchor_by_group.values():
        for slist in pos_map.values():
            slist.sort()

    conflicts: List[dict] = []

    # 2a) 同组锚点矛盾
    conflicting_groups: Set[int] = set()
    for gi in sorted(anchor_by_group, key=lambda i: mins[i]):
        pos_map = anchor_by_group[gi]
        if len(pos_map) > 1:
            conflicting_groups.add(gi)
            anchor_details = [
                {"stratum": s, "position": p}
                for p in sorted(pos_map)
                for s in pos_map[p]
            ]
            anchor_text = "，".join(f"{d['stratum']} -> 第 {d['position']} 期" for d in anchor_details)
            conflicts.append(
                {
                    "type": "anchor_position_conflict",
                    "message": (
                        f"同一期等价组 {mins[gi]}（{', '.join(groups_list[gi])}）的锚点"
                        f"指定了不同期位：{anchor_text}；同一期等价组须占同一期"
                    ),
                    "group": gi + 1,
                    "min_stratum": mins[gi],
                    "strata": list(groups_list[gi]),
                    "anchors": anchor_details,
                }
            )

    # 3) 商 DAG + 混合路径邻接（用于证据链展开）
    succ, edge_evidence, _ = _quotient_dag(groups_list, uf, assertions)
    preds: List[Set[int]] = [set() for _ in range(n)]
    for u, vs in enumerate(succ):
        for v in vs:
            preds[v].add(u)
    mixed_adj = _build_adj(assertions, frozenset({SAME, EARLIER}))

    # 4) 拓扑序（组按最小编号排序保证诊断稳定）；防御性处理（生效视图理论必为 DAG）
    indeg = [len(preds[i]) for i in range(n)]
    topo: List[int] = []
    heap = [i for i in range(n) if indeg[i] == 0]
    heapq.heapify(heap)
    while heap:
        u = heapq.heappop(heap)
        topo.append(u)
        for v in sorted(succ[u]):
            indeg[v] -= 1
            if indeg[v] == 0:
                heapq.heappush(heap, v)
    if len(topo) != n:
        # 商图成环不应出现在生效视图中（批次/并入核验已拒绝）；防御性兜底诊断
        conflicts.append(
            {
                "type": "chain_incompatible",
                "kind": "earlier_cycle",
                "message": "早于关系在等价组间形成闭环，任何分期排布都不存在",
                "chain_groups": [],
                "evidence_path": [],
                "required_earliest": None,
                "bound_position": None,
                "bound_kind": None,
            }
        )
        return ArrangementResult(False, k, (), {}, {}, tuple(conflicts))

    # 5) 锚定组固定期位（同组矛盾组视为未锚定参与链计算，其矛盾已单独报告）
    fixed: Dict[int, int] = {}
    for gi, pos_map in anchor_by_group.items():
        if gi in conflicting_groups:
            continue
        fixed[gi] = next(iter(pos_map))

    # 6) 最长路径下界 LB 及最优前驱（LB 最大；并列取组最小编号最小）
    lb = [0] * n
    lb_parent: List[Optional[int]] = [None] * n
    for u in topo:
        base = fixed.get(u, 1)
        best_p: Optional[int] = None
        best_val = 0
        for p in preds[u]:
            if lb[p] + 1 > best_val or (lb[p] + 1 == best_val and (best_p is None or mins[p] < mins[best_p])):
                best_val = lb[p] + 1
                best_p = p
        lb[u] = max(base, best_val)
        lb_parent[u] = best_p if best_val >= base else None

    # 7) 反向最长路径上界 UB 及最优后继（UB 最小；并列取组最小编号最小）
    ub = [0] * n
    ub_child: List[Optional[int]] = [None] * n
    for u in reversed(topo):
        base = fixed.get(u, k)
        best_c: Optional[int] = None
        best_val: Optional[int] = None
        for c in succ[u]:
            val = ub[c] - 1
            if best_val is None or val < best_val or (val == best_val and mins[c] < mins[best_c]):
                best_val = val
                best_c = c
        ub[u] = base if best_val is None else min(base, best_val)
        ub_child[u] = best_c if (best_val is not None and best_val <= base) else None

    # 8) 链不相容诊断。完备且不重复的“边界终结点”报告法：
    #    - 链超期数：LB[g] > K 的组沿任意后继链必然到达同样 LB>K 的汇点（LB 沿边严格
    #      递增）；只在“无越界后继”的汇点上报告一次，证据链为该汇点的最长下界链；
    #    - 锚点过早：锚定组 g 满足 LB[g] > 固定期位（其固定期位容不下前驱链）。
    #    任何 LB>UB 的不可行情形都终结于上述两类边界之一：下界经前驱链来自某锚点或
    #    第 1 期，上界经后继链来自某锚点或第 K 期；被两端锚点挤压的未锚定中间组，
    #    其矛盾必表现为下游锚点（或 K 边界汇点）自身的越界。
    def climb(g: int) -> List[int]:
        chain = [g]
        p = lb_parent[g]
        while p is not None:
            chain.append(p)
            p = lb_parent[p]
        chain.reverse()
        return chain

    exceeds = [lb[i] > k for i in range(n)]
    chain_conflicts: List[dict] = []

    def make_chain_conflict(gi: int, kind: str, bound: int) -> dict:
        chain = climb(gi)
        path = _chain_evidence_path(mixed_adj, chain, groups_list, anchor_by_group)
        chain_groups = [
            {
                "group": cgi + 1,
                "min_stratum": mins[cgi],
                "strata": list(groups_list[cgi]),
                "position": fixed.get(cgi),
                "anchor_strata": sorted(anchor_by_group.get(cgi, {}).get(fixed[cgi], []))
                if cgi in fixed else [],
            }
            for cgi in chain
        ]
        top = chain[0]
        top_desc = (
            f"锚点地层 {mins[top]} 固定于第 {fixed[top]} 期"
            if top in fixed else "第 1 期下界"
        )
        chain_text = " -> ".join(mins[c] for c in chain)
        if kind == "anchor_too_early":
            message = (
                f"先后链要求等价组 {mins[gi]} 最早位于第 {lb[gi]} 期，"
                f"但其锚点固定于第 {bound} 期（早于关系源组期位须严格小于目标组）；"
                f"证据链自{top_desc}起：{chain_text}"
            )
            bound_kind = "anchor"
        else:
            message = (
                f"先后链需要至少 {lb[gi]} 个严格递增期位，但总期数只有 {k}；"
                f"证据链自{top_desc}起：{chain_text}"
            )
            bound_kind = "total_periods"
        return {
            "type": "chain_incompatible",
            "kind": kind,
            "message": message,
            "chain_groups": chain_groups,
            "evidence_path": path,
            "edge_evidence": {
                f"{mins[u]}->{mins[v]}": list(edge_evidence[(u, v)])
                for u, v in zip(chain, chain[1:])
                if (u, v) in edge_evidence
            },
            "required_earliest": lb[gi],
            "bound_position": bound,
            "bound_kind": bound_kind,
            "group": gi + 1,
            "min_stratum": mins[gi],
        }

    for gi in sorted(range(n), key=lambda i: mins[i]):
        if not exceeds[gi]:
            continue
        # 链超期数：仅报告越界汇点（其后继都不再越界；任一越界节点都能走到此类汇点）
        if not any(exceeds[c] for c in succ[gi]):
            chain_conflicts.append(make_chain_conflict(gi, "chain_exceeds_periods", k))
    for gi in sorted(range(n), key=lambda i: mins[i]):
        # 锚点过早：锚定且固定期位容不下前驱链（LB>K 的情形已在上面按超期数报告）
        if not exceeds[gi] and gi in fixed and lb[gi] > fixed[gi]:
            chain_conflicts.append(make_chain_conflict(gi, "anchor_too_early", fixed[gi]))
    conflicts.extend(chain_conflicts)

    if conflicts:
        return ArrangementResult(False, k, (), {}, {}, tuple(conflicts))

    groups_out = tuple(
        ArrangementGroup(
            group=i + 1,
            min_stratum=mins[i],
            strata=tuple(groups_list[i]),
            anchored=(i in fixed),
            anchor_position=fixed.get(i),
            anchor_strata=tuple(
                sorted(anchor_by_group.get(i, {}).get(fixed[i], []))
            ) if i in fixed else (),
        )
        for i in range(n)
    )
    return ArrangementResult(
        feasible=True,
        total_periods=k,
        groups=groups_out,
        earliest={mins[i]: lb[i] for i in range(n)},
        latest={mins[i]: ub[i] for i in range(n)},
        conflicts=(),
    )
