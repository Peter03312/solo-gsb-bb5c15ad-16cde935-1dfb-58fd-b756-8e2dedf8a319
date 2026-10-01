#!/usr/bin/env bash
# 考古地层关系核验服务 —— 分期试排（/periodization/arrangements）端到端示例
# 前提：服务已启动（docker compose up --build），监听 http://localhost:8000
# 覆盖：正式视图可行（最早/最晚期位区间）、锚点收窄、未锚定无涉组同期、
#       总期数不足（链超期数）、锚点过早（链与锚点不相容）、同组锚点矛盾、
#       结构拒绝（锚点重复 / 期位越界 / 未登记地层 / 修订号过期）、
#       推演冻结视图（含已关闭推演）与证据 scope 标注；全程只读不写修订。
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
pp() { python3 -m json.tool 2>/dev/null || cat; }

echo "== 0. 健康检查 =="
curl -s "$BASE/health" | pp

echo; echo "== 1. 登记地层 L1～L6 =="
for s in L1 L2 L3 L4 L5 L6; do
  curl -s -X POST "$BASE/strata" -H 'Content-Type: application/json' \
    -d "{\"stratum_id\": \"$s\", \"site\": \"甲遗址\"}" >/dev/null
done
echo "已登记 L1～L6"

echo; echo "== 2. 正式批次（修订号 0 -> 1）：{L1,L2} < {L3} < {L4,L5}，L6 无断言 =="
curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d '{
  "expected_revision": 0,
  "operations": [
    {"op": "assert", "evidence_id": "EV-001", "stratum_a": "L1", "stratum_b": "L2", "relation": "same_period"},
    {"op": "assert", "evidence_id": "EV-005", "stratum_a": "L4", "stratum_b": "L5", "relation": "same_period"},
    {"op": "assert", "evidence_id": "EV-002", "stratum_a": "L2", "stratum_b": "L3", "relation": "earlier_than"},
    {"op": "assert", "evidence_id": "EV-003", "stratum_a": "L3", "stratum_b": "L4", "relation": "earlier_than"}
  ]}' | pp

echo; echo "== 3. 试排：正式视图、总期数 4、无锚点（应可行） =="
echo "  {L1,L2}∈[1,2]、{L3}∈[2,3]、{L4,L5}∈[3,4]、孤立 L6∈[1,4]（无涉组可与任一组同期）"
curl -s -X POST "$BASE/periodization/arrangements" -H 'Content-Type: application/json' -d '{
  "view": "formal",
  "expected_revision": 1,
  "total_periods": 4
}' | pp

echo; echo "== 4. 试排：锚点 L3 -> 第 2 期（应可行；L1 组被收窄为必在第 1 期） =="
curl -s -X POST "$BASE/periodization/arrangements" -H 'Content-Type: application/json' -d '{
  "view": "formal",
  "expected_revision": 1,
  "total_periods": 4,
  "anchors": [{"stratum": "L3", "position": 2}]
}' | pp

echo; echo "== 5. 试排：总期数 2（链需 3 个严格期位）-> feasible=false，"
echo "    chain_incompatible / chain_exceeds_periods，附组链与证据路径，不返回 groups 排布"
curl -s -X POST "$BASE/periodization/arrangements" -H 'Content-Type: application/json' -d '{
  "view": "formal",
  "expected_revision": 1,
  "total_periods": 2
}' | pp

echo; echo "== 6. 试排：锚点 L3 -> 第 1 期 -> chain_incompatible / anchor_too_early，"
echo "    required_earliest=2 > bound_position=1，证据路径 EV-001、EV-002"
curl -s -X POST "$BASE/periodization/arrangements" -H 'Content-Type: application/json' -d '{
  "view": "formal",
  "expected_revision": 1,
  "total_periods": 4,
  "anchors": [{"stratum": "L3", "position": 1}]
}' | pp

echo; echo "== 7. 试排：同组锚点矛盾 L1->1、L2->2 -> anchor_position_conflict（列全部关联锚点）；"
echo "    同时总期数 2 也放不下链 -> 两类冲突各自列出，不返回部分排布"
curl -s -X POST "$BASE/periodization/arrangements" -H 'Content-Type: application/json' -d '{
  "view": "formal",
  "expected_revision": 1,
  "total_periods": 2,
  "anchors": [{"stratum": "L1", "position": 1}, {"stratum": "L2", "position": 2}]
}' | pp

echo; echo "== 8. 结构拒绝 =="
echo "  8a. 锚点地层重复 -> 400 DUPLICATE_ANCHOR_STRATUM"
curl -s -i -X POST "$BASE/periodization/arrangements" -H 'Content-Type: application/json' -d '{
  "view": "formal", "expected_revision": 1, "total_periods": 4,
  "anchors": [{"stratum": "L3", "position": 1}, {"stratum": "L3", "position": 2}]}' | head -n 12

echo; echo "  8b. 期位越界（position=5 > total_periods=4） -> 400 ANCHOR_POSITION_OUT_OF_RANGE"
curl -s -i -X POST "$BASE/periodization/arrangements" -H 'Content-Type: application/json' -d '{
  "view": "formal", "expected_revision": 1, "total_periods": 4,
  "anchors": [{"stratum": "L3", "position": 5}]}' | head -n 12

echo; echo "  8c. 锚点地层未登记 -> 400 INVALID_STRATUM"
curl -s -i -X POST "$BASE/periodization/arrangements" -H 'Content-Type: application/json' -d '{
  "view": "formal", "expected_revision": 1, "total_periods": 4,
  "anchors": [{"stratum": "L99", "position": 1}]}' | head -n 12

echo; echo "  8d. 修订号过期（携带 0，当前为 1） -> 409 STALE_REVISION"
curl -s -i -X POST "$BASE/periodization/arrangements" -H 'Content-Type: application/json' -d '{
  "view": "formal", "expected_revision": 0, "total_periods": 4}' | head -n 12

echo; echo "  8e. view=projection 缺 projection_id -> 400 MISSING_PROJECTION"
curl -s -i -X POST "$BASE/periodization/arrangements" -H 'Content-Type: application/json' -d '{
  "view": "projection", "expected_revision": 0, "total_periods": 4}' | head -n 12

echo; echo "  8f. 总期数 0 / 65、期位 0 -> 422（请求体校验）"
curl -s -i -X POST "$BASE/periodization/arrangements" -H 'Content-Type: application/json' -d '{
  "view": "formal", "expected_revision": 1, "total_periods": 0}' | head -n 6

echo; echo "== 9. 推演（沙盘）冻结视图试排 =="
echo "  9a. 创建推演（固定正式修订 1、地层与生效断言）"
curl -s -X POST "$BASE/projections" -H 'Content-Type: application/json' \
  -d '{"label": "分期试排推演"}' | pp
PID=$(curl -s "$BASE/projections" | python3 -c 'import sys,json; print(json.load(sys.stdin)[-1]["id"])')
echo "  使用推演 #$PID"

echo; echo "  9b. 推演内批次（推演修订号 0 -> 1）：撤回 EV-003（L3<L4），新增 EV-P01（L3<L6）"
curl -s -X POST "$BASE/projections/$PID/batches" -H 'Content-Type: application/json' -d '{
  "expected_revision": 0,
  "operations": [
    {"op": "retract", "evidence_id": "EV-003"},
    {"op": "assert", "evidence_id": "EV-P01", "stratum_a": "L3", "stratum_b": "L6", "relation": "earlier_than"}
  ]}' | pp

echo; echo "  9c. 在推演冻结视图中试排（总期数 4）：L4/L5 组脱离链变为孤立；"
echo "      证据路径上 EV-P01 的 path_detail.scope=projection，基线证据 scope=base；正式状态不变"
curl -s -X POST "$BASE/periodization/arrangements" -H 'Content-Type: application/json' -d "{
  \"view\": \"projection\",
  \"projection_id\": $PID,
  \"expected_revision\": 1,
  \"total_periods\": 4
}" | pp

echo; echo "  9d. 推演修订号过期（携带 0，当前为 1） -> 409 STALE_REVISION（current_revision=推演修订号）"
curl -s -i -X POST "$BASE/periodization/arrangements" -H 'Content-Type: application/json' -d "{
  \"view\": \"projection\", \"projection_id\": $PID,
  \"expected_revision\": 0, \"total_periods\": 4}" | head -n 12

echo; echo "  9e. 放弃（关闭）推演后，仍可按其冻结视图分析"
curl -s -X DELETE "$BASE/projections/$PID" | pp
curl -s -X POST "$BASE/periodization/arrangements" -H 'Content-Type: application/json' -d "{
  \"view\": \"projection\",
  \"projection_id\": $PID,
  \"expected_revision\": 1,
  \"total_periods\": 2
}" | pp

echo; echo "  9f. 推演不存在 -> 404 PROJECTION_NOT_FOUND"
curl -s -i -X POST "$BASE/periodization/arrangements" -H 'Content-Type: application/json' -d '{
  "view": "projection", "projection_id": 999999,
  "expected_revision": 0, "total_periods": 4}' | head -n 8

echo; echo "== 10. 确认试排全程只读：正式修订号仍为 1 =="
curl -s "$BASE/revision" | pp

echo; echo "分期试排示例执行完毕。"
