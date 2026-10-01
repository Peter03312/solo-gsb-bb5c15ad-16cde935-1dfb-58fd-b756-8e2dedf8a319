"""分期试排（只读）业务逻辑。

考古人员试排“当前正式修订”或“指定推演修订”的分期：在同一只读视图中读取地层与
生效断言，按

- 同一期等价组占同一期；
- 早于关系源组期位严格小于目标组（含沿同一期链推出的先后关系）；
- 未锚定组可占任一期（无涉组可同期）；

判断给定总期数（1～64）与可选地层期位锚点下是否存在可行排布。可行时返回每组所含
地层，以及它在**所有**可行排布中的最早、最晚期位（不只给一套任意排布）；不可行时
区分“同组锚点矛盾”与“先后链与期数或锚点不相容”，指出关联锚点与证据路径，不返回
部分排布。

两种视图：
- formal：在 REPEATABLE READ READ ONLY 快照事务内读一次当前正式修订号，
  expected_revision 过期即 STALE_REVISION，绝不输出混合版本数据；
- projection：按推演冻结的基线地层 / 生效断言（基线快照减去推演内撤回、加上推演内
  仍生效的新增）分析；**已关闭（merged/discarded）推演仍按其冻结视图分析**；
  expected_revision 须等于该推演当前修订号。

全程只读：不写库、不递增正式或推演修订号、不产生变化记录；既有查询与批次语义不变。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from .db import db_conn
from .derivation import Assertion, RELATION_LABELS, trial_arrangement
from .errors import ApiError
from .models import ArrangementIn
from .projections import _get_projection, _load_view


# ------------------------------------------------------------------ 请求校验
def validate_request(body: ArrangementIn) -> List[Tuple[str, int]]:
    """结构性校验（不触库）：返回锚点 (stratum, position) 列表；非法即明确拒绝。"""
    if body.view == "projection" and body.projection_id is None:
        raise ApiError(
            400,
            "MISSING_PROJECTION",
            "view=projection 时必须提供 projection_id",
            {"field": "projection_id"},
        )
    anchors = list(body.anchors or [])
    seen: set = set()
    for item in anchors:
        if item.stratum in seen:
            raise ApiError(
                400,
                "DUPLICATE_ANCHOR_STRATUM",
                f"锚点地层重复: {item.stratum}（同一地层在一次试排中只能指定一个期位）",
                {"stratum": item.stratum},
            )
        seen.add(item.stratum)
        if item.position < 1 or item.position > body.total_periods:
            raise ApiError(
                400,
                "ANCHOR_POSITION_OUT_OF_RANGE",
                f"锚点期位越界: {item.stratum} -> 第 {item.position} 期；"
                f"总期数为 {body.total_periods}，期位须在 1～{body.total_periods} 之间",
                {"stratum": item.stratum, "position": item.position, "total_periods": body.total_periods},
            )
    return [(item.stratum, item.position) for item in anchors]


def _path_detail(
    path: Sequence[str],
    assertions: Sequence[Assertion],
    projection_scope: Optional[set],
) -> List[Dict[str, Any]]:
    """把证据编号序列展开为逐步断言；推演视图附 scope（base/projection）。"""
    by_id = {a.evidence_id: a for a in assertions}
    out: List[Dict[str, Any]] = []
    for eid in path:
        a = by_id.get(eid)
        if a is None:
            continue
        step = {
            "evidence_id": eid,
            "stratum_a": a.a,
            "stratum_b": a.b,
            "relation": a.relation,
            "relation_label": RELATION_LABELS.get(a.relation, a.relation),
            "status": "active",
        }
        if projection_scope is not None:
            step["scope"] = "projection" if eid in projection_scope else "base"
        out.append(step)
    return out


def _conflict_payload(conflict: dict, assertions: Sequence[Assertion], scope: Optional[set]) -> Dict[str, Any]:
    path = conflict.get("evidence_path") or []
    out = dict(conflict)
    out["path_detail"] = _path_detail(path, assertions, scope)
    return out


def _success_payload(
    result,
    anchors: Sequence[Tuple[str, int]],
    view_meta: Dict[str, Any],
    assertions: Sequence[Assertion],
    scope: Optional[set],
) -> Dict[str, Any]:
    anchor_map = {s: p for s, p in anchors}
    return {
        **view_meta,
        "total_periods": result.total_periods,
        "anchors": [{"stratum": s, "position": p} for s, p in anchors],
        "feasible": True,
        "groups": [
            {
                "group": g.group,
                "min_stratum": g.min_stratum,
                "strata": list(g.strata),
                "anchored": g.anchored,
                "anchor_position": g.anchor_position,
                "anchor_strata": list(g.anchor_strata),
                "earliest_position": result.earliest[g.min_stratum],
                "latest_position": result.latest[g.min_stratum],
            }
            for g in result.groups
        ],
        "conflicts": [],
    }


def _failure_payload(
    result,
    anchors: Sequence[Tuple[str, int]],
    view_meta: Dict[str, Any],
    assertions: Sequence[Assertion],
    scope: Optional[set],
) -> Dict[str, Any]:
    return {
        **view_meta,
        "total_periods": result.total_periods,
        "anchors": [{"stratum": s, "position": p} for s, p in anchors],
        "feasible": False,
        "groups": [],
        "conflicts": [_conflict_payload(c, assertions, scope) for c in result.conflicts],
    }


# ------------------------------------------------------------------ 正式视图
def _analyze_formal(cur, body: ArrangementIn, anchors: List[Tuple[str, int]]) -> Dict[str, Any]:
    # 只读快照事务：修订号在事务内只读一次，组与证据均取自同一快照
    cur.execute("SET TRANSACTION READ ONLY ISOLATION LEVEL REPEATABLE READ")
    cur.execute("SELECT revision FROM revision_counter WHERE id = TRUE")
    current = cur.fetchone()[0]
    if body.expected_revision != current:
        raise ApiError(
            409,
            "STALE_REVISION",
            f"修订号过期：请求携带 {body.expected_revision}，当前正式修订号为 {current}",
            {"view": "formal", "current_revision": current},
        )
    cur.execute("SELECT stratum_id FROM strata ORDER BY stratum_id")
    strata = [r[0] for r in cur.fetchall()]
    cur.execute(
        "SELECT evidence_id, stratum_a, stratum_b, relation "
        "FROM assertions WHERE status = 'active' ORDER BY evidence_id"
    )
    assertions = [Assertion(evidence_id=r[0], a=r[1], b=r[2], relation=r[3]) for r in cur.fetchall()]

    strata_set = set(strata)
    for s, _ in anchors:
        if s not in strata_set:
            raise ApiError(400, "INVALID_STRATUM", f"地层未登记: {s}", {"stratum": s, "view": "formal"})

    result = trial_arrangement(strata, assertions, body.total_periods, anchors)
    view_meta = {
        "view": "formal",
        "expected_revision": current,
        "current_revision": current,
        "revision": current,
    }
    if result.feasible:
        return _success_payload(result, anchors, view_meta, assertions, None)
    return _failure_payload(result, anchors, view_meta, assertions, None)


# ------------------------------------------------------------------ 推演视图
def _analyze_projection(cur, body: ArrangementIn, anchors: List[Tuple[str, int]]) -> Dict[str, Any]:
    # 只读分析：不要求推演 open——已关闭推演仍按其冻结视图分析；不锁定、不写入。
    cur.execute("SET TRANSACTION READ ONLY ISOLATION LEVEL REPEATABLE READ")
    proj = _get_projection(cur, body.projection_id)
    if body.expected_revision != proj["projection_revision"]:
        raise ApiError(
            409,
            "STALE_REVISION",
            f"推演修订号过期：请求携带 {body.expected_revision}，推演 {proj['id']} 当前为 "
            f"{proj['projection_revision']}",
            {
                "view": "projection",
                "projection_id": proj["id"],
                "current_revision": proj["projection_revision"],
            },
        )
    _, effective, _ = _load_view(cur, proj["id"], proj)
    strata = list(proj["base_strata"])
    strata_set = set(strata)
    for s, _ in anchors:
        if s not in strata_set:
            raise ApiError(
                400,
                "INVALID_STRATUM",
                f"地层不在推演基线内: {s}",
                {"stratum": s, "view": "projection", "projection_id": proj["id"]},
            )

    # 推演内新增证据（即使后来撤回也不影响“仍生效”集合，scope 只标注生效路径上的来源）
    cur.execute(
        "SELECT evidence_id FROM projection_assertions WHERE projection_id = %s AND status = 'active'",
        (proj["id"],),
    )
    projection_scope = {r[0] for r in cur.fetchall()}

    result = trial_arrangement(strata, effective, body.total_periods, anchors)
    view_meta = {
        "view": "projection",
        "projection_id": proj["id"],
        "projection_status": proj["status"],
        "base_revision": proj["base_revision"],
        "expected_revision": proj["projection_revision"],
        "current_revision": proj["projection_revision"],
        "projection_revision": proj["projection_revision"],
    }
    if result.feasible:
        return _success_payload(result, anchors, view_meta, effective, projection_scope)
    return _failure_payload(result, anchors, view_meta, effective, projection_scope)


def analyze_arrangement(body: ArrangementIn) -> Dict[str, Any]:
    """分期试排入口：结构校验后在对应只读视图中完成分析（HTTP 恒为 200；结构错误 4xx）。"""
    anchors = validate_request(body)
    with db_conn() as conn:
        with conn.cursor() as cur:
            if body.view == "projection":
                return _analyze_projection(cur, body, anchors)
            return _analyze_formal(cur, body, anchors)
