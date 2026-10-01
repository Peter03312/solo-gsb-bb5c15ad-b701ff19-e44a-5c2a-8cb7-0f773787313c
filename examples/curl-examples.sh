#!/usr/bin/env bash
# 博物馆展柜温度暴露审定服务 —— curl 调用示例
# 用法：BASE_URL=http://localhost:8000 bash examples/curl-examples.sh
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
api() { # method path [json]
  local method="$1" path="$2" json="${3:-}"
  if [[ -n "$json" ]]; then
    curl -sS -X "$method" "$BASE_URL$path" -H 'Content-Type: application/json' -d "$json"
  else
    curl -sS -X "$method" "$BASE_URL$path"
  fi
  echo
}

echo "== 健康检查 =="
api GET /health

echo "== 1. 创建审定窗口（1 小时；允许 18~22°C；最长连续超限 300s；最大采样间隔 600s）=="
WINDOW=$(api POST /api/v1/windows '{
  "name": "展柜A-2026-10-01",
  "start_at": "2026-10-01T00:00:00Z",
  "end_at":   "2026-10-01T01:00:00Z",
  "allowed_temp_min": 18.0,
  "allowed_temp_max": 22.0,
  "max_continuous_exceed_seconds": 300,
  "max_sample_interval_seconds": 600
}')
echo "$WINDOW" | python3 -m json.tool
WID=$(echo "$WINDOW" | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")

echo "== 2. 提交读数（reading_id + revision）=="
api POST "/api/v1/windows/$WID/readings" '{"reading_id":"r-00","revision":1,"measured_at":"2026-10-01T00:00:00Z","temperature":20.1}' >/dev/null
api POST "/api/v1/windows/$WID/readings" '{"reading_id":"r-01","revision":1,"measured_at":"2026-10-01T00:10:00Z","temperature":22.4}' >/dev/null
api POST "/api/v1/windows/$WID/readings" '{"reading_id":"r-02","revision":1,"measured_at":"2026-10-01T00:20:00Z","temperature":23.1}' >/dev/null
api POST "/api/v1/windows/$WID/readings" '{"reading_id":"r-03","revision":1,"measured_at":"2026-10-01T00:30:00Z","temperature":20.5}' >/dev/null

echo "== 3. 相同 ID + 修订号重复提交（幂等，内容一致）=="
api POST "/api/v1/windows/$WID/readings" '{"reading_id":"r-00","revision":1,"measured_at":"2026-10-01T00:00:00Z","temperature":20.1}'

echo "== 4. 相同 ID + 修订号但温度冲突 -> 409 =="
api POST "/api/v1/windows/$WID/readings" '{"reading_id":"r-00","revision":1,"measured_at":"2026-10-01T00:00:00Z","temperature":99.9}' || true

echo "== 5. 旧修订号 -> 409；更高修订号替换重算 =="
api POST "/api/v1/windows/$WID/readings" '{"reading_id":"r-01","revision":1,"measured_at":"2026-10-01T00:10:00Z","temperature":22.4}' >/dev/null
api POST "/api/v1/windows/$WID/readings" '{"reading_id":"r-01","revision":1,"measured_at":"2026-10-01T00:10:00Z","temperature":20.0}' || true
api POST "/api/v1/windows/$WID/readings" '{"reading_id":"r-01","revision":2,"measured_at":"2026-10-01T00:10:00Z","temperature":20.0}'

echo "== 6. 补齐整窗读数（间隔 <= 600s；末尾含窗口终点读数作为覆盖终止采样）=="
for min in 40 50 60; do
  if [[ "$min" -eq 60 ]]; then ts="2026-10-01T01:00:00Z"; else ts="2026-10-01T00:$min:00Z"; fi
  api POST "/api/v1/windows/$WID/readings" \
    "{\"reading_id\":\"r-$min\",\"revision\":1,\"measured_at\":\"$ts\",\"temperature\":20.0}" >/dev/null
done

echo "== 7. 查看当前审定（区段 + 最长连续超限 + 结论）=="
api GET "/api/v1/windows/$WID/evaluation" | python3 -m json.tool

echo "== 8. 首次签发（v1）=="
api POST "/api/v1/windows/$WID/certificates/issue" '{"note":"首次签发"}' | python3 -m json.tool

echo "== 9. 修订 r-02 为正常值后再次签发（v2；v1 快照不变）=="
api POST "/api/v1/windows/$WID/readings" '{"reading_id":"r-02","revision":2,"measured_at":"2026-10-01T00:20:00Z","temperature":20.2}' >/dev/null
api POST "/api/v1/windows/$WID/certificates/issue" '{"note":"修订后重新签发"}' | python3 -m json.tool

echo "== 10. 列出全部证书版本 =="
api GET "/api/v1/windows/$WID/certificates" | python3 -m json.tool

echo "== 11. 修订影响报告：比对 v1/v2 两张已签发证书的不可变快照 =="
# 从版本列表取两个证书主键 ID（响应只解析一次，避免 api 尾部换行拆词）
CIDS=$(api GET "/api/v1/windows/$WID/certificates" \
  | python3 -c "import sys,json;print(' '.join(str(c['id']) for c in json.load(sys.stdin)))")
read -r OLD_ID NEW_ID <<< "$CIDS"
api GET "/api/v1/certificates/revision-report?old_certificate_id=$OLD_ID&new_certificate_id=$NEW_ID" \
  | python3 -m json.tool

echo "== 12. 报告拒绝：版本倒序 / 跨窗口 / 同 ID（均 422），不存在 404 =="
api GET "/api/v1/certificates/revision-report?old_certificate_id=$NEW_ID&new_certificate_id=$OLD_ID" || true
api GET "/api/v1/certificates/revision-report?old_certificate_id=$OLD_ID&new_certificate_id=99999" || true

echo "== 13. 采样仪器补传：单独建一个窗口演示批量提交（整批事务）=="
BW=$(api POST /api/v1/windows '{
  "name": "展柜B-补传",
  "start_at": "2026-10-01T00:00:00Z",
  "end_at":   "2026-10-01T01:00:00Z",
  "allowed_temp_min": 18.0,
  "allowed_temp_max": 22.0,
  "max_continuous_exceed_seconds": 300,
  "max_sample_interval_seconds": 600
}')
BID=$(echo "$BW" | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")

echo "-- 13.1 一次批量创建多条读数（逐项返回 created，并按最终状态重算审定）--"
api POST "/api/v1/windows/$BID/readings/batch" '{
  "readings": [
    {"reading_id":"a","revision":1,"measured_at":"2026-10-01T00:00:00Z","temperature":20.1},
    {"reading_id":"b","revision":1,"measured_at":"2026-10-01T00:10:00Z","temperature":23.0},
    {"reading_id":"c","revision":1,"measured_at":"2026-10-01T00:20:00Z","temperature":20.2}
  ]
}' | python3 -c "import sys,json;d=json.load(sys.stdin);print('effects:',[x['effect'] for x in d['results']]);print('verdict:',d['current_evaluation']['verdict'])"

echo "-- 13.2 同批重放：全部 idempotent，不产生新修订 --"
api POST "/api/v1/windows/$BID/readings/batch" '{
  "readings": [
    {"reading_id":"a","revision":1,"measured_at":"2026-10-01T00:00:00Z","temperature":20.1},
    {"reading_id":"b","revision":1,"measured_at":"2026-10-01T00:10:00Z","temperature":23.0},
    {"reading_id":"c","revision":1,"measured_at":"2026-10-01T00:20:00Z","temperature":20.2}
  ]
}' | python3 -c "import sys,json;print('effects:',[x['effect'] for x in json.load(sys.stdin)['results']])"

echo "-- 13.3 两条读数以更高修订号互换采样时刻（按最终状态判同刻，顺序无关，均 updated）--"
api POST "/api/v1/windows/$BID/readings/batch" '{
  "readings": [
    {"reading_id":"a","revision":2,"measured_at":"2026-10-01T00:10:00Z","temperature":20.1},
    {"reading_id":"b","revision":2,"measured_at":"2026-10-01T00:00:00Z","temperature":20.0}
  ]
}' | python3 -c "import sys,json;print([(x['reading']['reading_id'],x['effect'],x['reading']['measured_at']) for x in json.load(sys.stdin)['results']])"

echo "-- 13.4 整批拒收：一项 409（b 用旧修订号 rev1）则整批不变（c 不会被创建）--"
api POST "/api/v1/windows/$BID/readings/batch" '{
  "readings": [
    {"reading_id":"b","revision":1,"measured_at":"2026-10-01T00:00:00Z","temperature":20.0},
    {"reading_id":"x","revision":1,"measured_at":"2026-10-01T00:30:00Z","temperature":20.0}
  ]
}' || true
echo "活动读数（应仍为 a/b/c 三条，无 x）："
api GET "/api/v1/windows/$BID/readings" \
  | python3 -c "import sys,json;print(sorted(r['reading_id'] for r in json.load(sys.stdin)))"

echo "-- 13.5 整批拒收：同批 reading_id 重复（422）；空批次也是 422 --"
api POST "/api/v1/windows/$BID/readings/batch" '{
  "readings": [
    {"reading_id":"a","revision":3,"measured_at":"2026-10-01T00:30:00Z","temperature":20.0},
    {"reading_id":"a","revision":3,"measured_at":"2026-10-01T00:40:00Z","temperature":20.0}
  ]
}' || true
api POST "/api/v1/windows/$BID/readings/batch" '{"readings":[]}' || true

echo "== 14. 馆员作废与恢复（误采确认）=="
VW=$(api POST /api/v1/windows '{
  "name": "展柜C-作废恢复",
  "start_at": "2026-10-01T00:00:00Z",
  "end_at":   "2026-10-01T01:00:00Z",
  "allowed_temp_min": 18.0,
  "allowed_temp_max": 22.0,
  "max_continuous_exceed_seconds": 300,
  "max_sample_interval_seconds": 600
}')
VID=$(echo "$VW" | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")

echo "-- 14.1 提交读数：m-01 为误采超温（00:10->00:20 超限 600s -> fail），并签发 v1 --"
api POST "/api/v1/windows/$VID/readings" '{"reading_id":"m-00","revision":1,"measured_at":"2026-10-01T00:00:00Z","temperature":20.0}' >/dev/null
api POST "/api/v1/windows/$VID/readings" '{"reading_id":"m-01","revision":1,"measured_at":"2026-10-01T00:10:00Z","temperature":23.5}' >/dev/null
api POST "/api/v1/windows/$VID/readings" '{"reading_id":"m-02","revision":1,"measured_at":"2026-10-01T00:20:00Z","temperature":20.2}' >/dev/null
api GET "/api/v1/windows/$VID/evaluation" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('作废前:',d['verdict'],d['max_continuous_exceeded_seconds'],'s')"
VC1=$(api POST "/api/v1/windows/$VID/certificates/issue" '{"note":"作废前"}')
VC1ID=$(echo "$VC1" | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")

echo "-- 14.2 作废 m-01（递增修订号 rev2 + 非空原因）：留存原因/操作时间/前一版本，当前审定立即剔除重算 --"
api POST "/api/v1/windows/$VID/readings/m-01/void" \
  '{"revision":2,"reason":"误采：传感器未校准"}' | python3 -m json.tool

echo "-- 14.3 相同作废修订+原因重放 -> idempotent；换原因 -> 409；已作废再以新修订号作废 -> 409；从未存在 -> 404 --"
api POST "/api/v1/windows/$VID/readings/m-01/void" \
  '{"revision":2,"reason":"误采：传感器未校准"}' \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('重放 effect:',d['effect'])"
api POST "/api/v1/windows/$VID/readings/m-01/void" '{"revision":2,"reason":"另一个原因"}' || true
api POST "/api/v1/windows/$VID/readings/m-01/void" '{"revision":3,"reason":"再次作废"}' || true
api POST "/api/v1/windows/$VID/readings/ghost/void" '{"revision":1,"reason":"x"}' || true

echo "-- 14.4 修订历史：rev1 superseded、rev2 voided（含作废留存）--"
api GET "/api/v1/windows/$VID/readings/m-01/revisions" | python3 -m json.tool

echo "-- 14.5 作废后签发 v2（快照剔除 m-01）；修订影响报告 v1->v2 呈现 removed；v1 快照不变 --"
VC2=$(api POST "/api/v1/windows/$VID/certificates/issue" '{"note":"作废后"}')
VC2ID=$(echo "$VC2" | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
api GET "/api/v1/certificates/revision-report?old_certificate_id=$VC1ID&new_certificate_id=$VC2ID" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('removed:',[r['reading_id'] for r in d['readings_diff']['removed']],'added:',[r['reading_id'] for r in d['readings_diff']['added']])"
api GET "/api/v1/certificates/$VC1ID" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('v1 快照读数（不变）:',sorted(r['reading_id'] for r in d['snapshot']['readings']))"

echo "-- 14.6 恢复：更高修订号提交 -> restored，审定恢复 fail --"
api POST "/api/v1/windows/$VID/readings" \
  '{"reading_id":"m-01","revision":3,"measured_at":"2026-10-01T00:10:00Z","temperature":23.5}' \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('effect:',d['effect'],'verdict:',d['current_evaluation']['verdict'])"

echo "-- 14.7 再次作废（rev4）后：旧版提交（rev3）不得复活 -> 409；批量更高修订号恢复 -> restored --"
api POST "/api/v1/windows/$VID/readings/m-01/void" '{"revision":4,"reason":"复核仍为误采"}' >/dev/null
api POST "/api/v1/windows/$VID/readings" \
  '{"reading_id":"m-01","revision":3,"measured_at":"2026-10-01T00:10:00Z","temperature":23.5}' || true
api POST "/api/v1/windows/$VID/readings/batch" '{
  "readings": [
    {"reading_id":"m-01","revision":5,"measured_at":"2026-10-01T00:10:00Z","temperature":23.5}
  ]
}' | python3 -c "import sys,json;d=json.load(sys.stdin);print('批量 effects:',[x['effect'] for x in d['results']])"

echo "-- 14.8 恢复后签发 v3；修订影响报告 v2->v3 呈现 added（读数再出现）--"
VC3=$(api POST "/api/v1/windows/$VID/certificates/issue" '{"note":"恢复后"}')
VC3ID=$(echo "$VC3" | python3 -c "import sys,json;print(json.load(sys.stdin)['id'])")
api GET "/api/v1/certificates/revision-report?old_certificate_id=$VC2ID&new_certificate_id=$VC3ID" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('added:',[(r['reading_id'],r['revision']) for r in d['readings_diff']['added']],'removed:',[r['reading_id'] for r in d['readings_diff']['removed']])"
