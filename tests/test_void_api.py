"""HTTP API 集成测试：馆员作废（误采确认）与恢复。

覆盖：
- 作废：递增修订号 + 非空原因；作废修订留存原因/操作时间/前一版本；
  当前审定立即剔除并重算；
- 幂等：相同作废修订号+原因重放幂等（不产生新修订行）；原因不同或
  低/等修订号按 409 拒绝；从未存在 404；已作废不能再次以新修订号作废；
- 恢复：单条/批量以更高修订号恢复（restored），遵守窗口边界与最终状态
  同刻唯一；旧版提交不得复活；批量任一项冲突整批回滚；
- 证书与报告：已签发快照不变，后续签发反映作废/恢复，修订影响报告
  呈现读数移除（removed）与再出现（added）。

运行：PYTHONPATH=. python -m pytest tests/test_void_api.py -q
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import settings


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(settings, "db_path", str(db_file))
    monkeypatch.setattr(settings, "db_timeout", 5)
    # 延迟导入 app，确保 connect() 读取被覆盖后的 settings
    from app.main import app

    with TestClient(app) as c:
        yield c


WINDOW_BASE = "2026-10-01T00:00:00Z"


def make_window(client, start=WINDOW_BASE, minutes=60, tmin=18.0, tmax=22.0,
                max_exceed=300.0, max_gap=600.0, name=None):
    body = {
        "start_at": start,
        "end_at": (datetime.fromisoformat(start.replace("Z", "+00:00"))
                   + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z"),
        "allowed_temp_min": tmin,
        "allowed_temp_max": tmax,
        "max_continuous_exceed_seconds": max_exceed,
        "max_sample_interval_seconds": max_gap,
    }
    if name:
        body["name"] = name
    r = client.post("/api/v1/windows", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def reading(rid, rev, minute, temp):
    ts = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc) + timedelta(minutes=minute)
    return {
        "reading_id": rid, "revision": rev,
        "measured_at": ts.isoformat().replace("+00:00", "Z"),
        "temperature": temp,
    }


def submit(client, wid, body):
    return client.post(f"/api/v1/windows/{wid}/readings", json=body)


def void(client, wid, rid, rev, reason):
    return client.post(f"/api/v1/windows/{wid}/readings/{rid}/void",
                       json={"revision": rev, "reason": reason})


def active_map(client, wid):
    return {r["reading_id"]: r
            for r in client.get(f"/api/v1/windows/{wid}/readings").json()}


def revisions(client, wid, rid):
    return client.get(f"/api/v1/windows/{wid}/readings/{rid}/revisions")


def seed_fail_window(client):
    """a@00:00 正常、b@00:01 超温（误采）、c@00:07 正常、d@00:08 正常。

    b 覆盖 [00:01, 00:07) 超限 360s > 300 -> fail；作废 b 后无超限，
    尾部未知 -> pending；恢复 b 后再次 fail。
    """
    make_window(client, max_gap=600.0)
    submit(client, 1, reading("a", 1, 0, 20.0))
    submit(client, 1, reading("b", 1, 1, 23.0))
    submit(client, 1, reading("c", 1, 7, 20.0))
    submit(client, 1, reading("d", 1, 8, 20.0))
    ev = client.get("/api/v1/windows/1/evaluation").json()
    assert ev["verdict"] == "fail"
    assert ev["max_continuous_exceeded_seconds"] == 360.0
    return ev


# ---------------------------------------------------------------- 作废基本流程

def test_void_removes_reading_and_recomputes_immediately(client):
    seed_fail_window(client)
    # 作废误采的超温读数 b（rev1 -> 作废修订 rev2）
    r = void(client, 1, "b", 2, "误采：传感器未校准")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["effect"] == "voided"
    assert body["reading_id"] == "b" and body["revision"] == 2
    # 留存：原因、操作时间、前一版本
    assert body["void"]["reason"] == "误采：传感器未校准"
    assert body["void"]["voided_at"].endswith("Z")
    assert body["void"]["previous"] == {
        "revision": 1,
        "measured_at": "2026-10-01T00:01:00Z",
        "temperature": 23.0,
    }
    # 当前审定立即剔除该读数并重算：无超限，尾部未知 -> pending
    ev = body["current_evaluation"]
    assert ev["reading_count"] == 3
    assert ev["max_continuous_exceeded_seconds"] == 0.0
    assert ev["verdict"] == "pending"
    # 与独立查询一致
    assert client.get("/api/v1/windows/1/evaluation").json() == ev
    # 活动读数列表不再包含 b
    assert set(active_map(client, 1)) == {"a", "c", "d"}


def test_void_revision_history_retains_reason_time_previous(client):
    seed_fail_window(client)
    void(client, 1, "b", 2, "误采")
    r = revisions(client, 1, "b")
    assert r.status_code == 200, r.text
    rows = r.json()
    assert [(x["revision"], x["status"]) for x in rows] == [(1, "superseded"), (2, "voided")]
    v = rows[1]
    assert v["measured_at"] is None and v["temperature"] is None
    assert v["void"]["reason"] == "误采"
    assert v["void"]["voided_at"].endswith("Z")
    assert v["void"]["previous"]["revision"] == 1
    assert v["void"]["previous"]["measured_at"] == "2026-10-01T00:01:00Z"
    assert v["void"]["previous"]["temperature"] == 23.0
    # 未作废的读数历史正常返回采样内容
    rows_a = revisions(client, 1, "a").json()
    assert rows_a[0]["status"] == "active" and rows_a[0]["void"] is None
    assert rows_a[0]["measured_at"] == "2026-10-01T00:00:00Z"


def test_void_replay_same_revision_and_reason_idempotent(client):
    seed_fail_window(client)
    r1 = void(client, 1, "b", 2, "误采")
    assert r1.status_code == 201 and r1.json()["effect"] == "voided"
    r2 = void(client, 1, "b", 2, "误采")
    assert r2.status_code == 201 and r2.json()["effect"] == "idempotent"
    # 重放返回相同的留存信息（操作时间一致），审定一致
    assert r2.json()["void"] == r1.json()["void"]
    assert r2.json()["current_evaluation"] == r1.json()["current_evaluation"]
    # 不产生新修订行
    rows = revisions(client, 1, "b").json()
    assert [x["revision"] for x in rows] == [1, 2]


def test_void_same_revision_different_reason_409(client):
    seed_fail_window(client)
    void(client, 1, "b", 2, "误采")
    r = void(client, 1, "b", 2, "另一个原因")
    assert r.status_code == 409 and "内容冲突" in r.json()["detail"]


def test_void_lower_or_equal_revision_409(client):
    seed_fail_window(client)
    # revision 必须 >= 1（请求校验）
    assert void(client, 1, "b", 0, "误采").status_code == 422
    # 等于当前读数版本 -> 409
    r = void(client, 1, "b", 1, "误采")
    assert r.status_code == 409 and "当前读数版本" in r.json()["detail"]
    # 先修到 rev3，再用旧修订号作废 -> 409
    submit(client, 1, reading("b", 3, 1, 23.0))
    r = void(client, 1, "b", 2, "误采")
    assert r.status_code == 409 and "旧修订号" in r.json()["detail"]
    # 仍未作废
    assert "b" in active_map(client, 1)


def test_void_never_existed_reading_404(client):
    seed_fail_window(client)
    r = void(client, 1, "ghost", 1, "误采")
    assert r.status_code == 404 and "从未存在" in r.json()["detail"]
    # 修订历史查询同样 404
    assert revisions(client, 1, "ghost").status_code == 404


def test_void_missing_window_404(client):
    make_window(client)
    r = void(client, 999, "a", 1, "误采")
    assert r.status_code == 404


def test_void_already_voided_cannot_void_again_with_new_revision(client):
    seed_fail_window(client)
    void(client, 1, "b", 2, "误采")
    r = void(client, 1, "b", 3, "再次作废")
    assert r.status_code == 409 and "已处于作废状态" in r.json()["detail"]
    # 历史未变
    rows = revisions(client, 1, "b").json()
    assert [x["revision"] for x in rows] == [1, 2]


def test_void_blank_reason_rejected(client):
    seed_fail_window(client)
    assert void(client, 1, "b", 2, "").status_code == 422
    assert void(client, 1, "b", 2, "   ").status_code == 422
    assert "b" in active_map(client, 1)


def test_void_frees_timestamp_for_other_reading(client):
    seed_fail_window(client)
    void(client, 1, "b", 2, "误采")
    # 原 00:01 时刻已释放，其它读数可以使用
    r = submit(client, 1, reading("e", 1, 1, 20.0))
    assert r.status_code == 201 and r.json()["effect"] == "created"


# ---------------------------------------------------------------- 恢复（单条）

def test_restore_with_higher_revision_single(client):
    seed_fail_window(client)
    void(client, 1, "b", 2, "误采：后确认实为有效")
    assert set(active_map(client, 1)) == {"a", "c", "d"}
    # 以更高修订号重新提交 -> 恢复
    r = submit(client, 1, reading("b", 3, 1, 23.0))
    assert r.status_code == 201, r.text
    assert r.json()["effect"] == "restored"
    assert r.json()["current_evaluation"]["verdict"] == "fail"
    assert r.json()["current_evaluation"]["reading_count"] == 4
    rows = active_map(client, 1)
    assert rows["b"]["revision"] == 3 and rows["b"]["temperature"] == 23.0
    # 历史完整：rev1 superseded、rev2 voided（留存）、rev3 active
    hist = {x["revision"]: x["status"] for x in revisions(client, 1, "b").json()}
    assert hist == {1: "superseded", 2: "voided", 3: "active"}


def test_restore_with_old_revision_rejected(client):
    seed_fail_window(client)
    void(client, 1, "b", 2, "误采")
    # 等于作废修订号 -> 409
    r = submit(client, 1, reading("b", 2, 1, 23.0))
    assert r.status_code == 409 and "已作废" in r.json()["detail"]
    # 低于作废修订号（旧版提交不得复活）-> 409
    r = submit(client, 1, reading("b", 1, 1, 23.0))
    assert r.status_code == 409 and "已作废" in r.json()["detail"]
    assert "b" not in active_map(client, 1)


def test_restore_respects_window_boundary(client):
    seed_fail_window(client)
    void(client, 1, "b", 2, "误采")
    r = submit(client, 1, reading("b", 3, 61, 23.0))  # 超出窗口终点
    assert r.status_code == 422 and "超出窗口" in r.json()["detail"]
    assert "b" not in active_map(client, 1)


def test_restore_respects_same_timestamp_uniqueness(client):
    seed_fail_window(client)
    void(client, 1, "b", 2, "误采")
    # 恢复到 d 的 00:08 时刻 -> 同刻拒收
    r = submit(client, 1, reading("b", 3, 8, 23.0))
    assert r.status_code == 422 and "同刻" in r.json()["detail"]
    assert "b" not in active_map(client, 1)


def test_restore_then_void_again_lifecycle(client):
    seed_fail_window(client)
    void(client, 1, "b", 2, "第一次作废")
    submit(client, 1, reading("b", 3, 1, 23.0))
    # 恢复后重放旧的作废修订号 -> 低修订号 409
    r = void(client, 1, "b", 2, "第一次作废")
    assert r.status_code == 409 and "旧修订号" in r.json()["detail"]
    # 恢复后是新的当前读数，可再次以更高修订号作废
    r = void(client, 1, "b", 4, "复核后再次确认为误采")
    assert r.status_code == 201 and r.json()["effect"] == "voided"
    assert r.json()["void"]["previous"]["revision"] == 3
    hist = {x["revision"]: x["status"] for x in revisions(client, 1, "b").json()}
    assert hist == {1: "superseded", 2: "voided", 3: "superseded", 4: "voided"}


# ---------------------------------------------------------------- 恢复（批量）

def test_restore_via_batch(client):
    seed_fail_window(client)
    void(client, 1, "b", 2, "误采")
    r = client.post("/api/v1/windows/1/readings/batch", json={"readings": [
        reading("b", 3, 1, 23.0),   # 恢复
        reading("e", 1, 10, 20.0),  # 新建
    ]})
    assert r.status_code == 201, r.text
    assert [x["effect"] for x in r.json()["results"]] == ["restored", "created"]
    assert r.json()["current_evaluation"]["reading_count"] == 5
    assert set(active_map(client, 1)) == {"a", "b", "c", "d", "e"}


def test_batch_restore_final_state_same_timestamp_rolls_back(client):
    seed_fail_window(client)
    void(client, 1, "b", 2, "误采")
    # 恢复 b 到 00:08（d 的时刻）：最终状态同刻 -> 整批拒收，e 也不得落库
    r = client.post("/api/v1/windows/1/readings/batch", json={"readings": [
        reading("b", 3, 8, 23.0),
        reading("e", 1, 10, 20.0),
    ]})
    assert r.status_code == 422 and "同刻" in r.json()["detail"]
    assert set(active_map(client, 1)) == {"a", "c", "d"}
    assert revisions(client, 1, "e").status_code == 404


def test_batch_restore_with_old_revision_rolls_back(client):
    seed_fail_window(client)
    void(client, 1, "b", 2, "误采")
    # 批内旧修订号恢复 -> 409 整批回滚（e 不创建，b 仍作废）
    r = client.post("/api/v1/windows/1/readings/batch", json={"readings": [
        reading("b", 1, 1, 23.0),
        reading("e", 1, 10, 20.0),
    ]})
    assert r.status_code == 409 and "已作废" in r.json()["detail"]
    assert set(active_map(client, 1)) == {"a", "c", "d"}
    assert revisions(client, 1, "e").status_code == 404


def test_batch_restore_outside_window_rolls_back(client):
    seed_fail_window(client)
    void(client, 1, "b", 2, "误采")
    r = client.post("/api/v1/windows/1/readings/batch", json={"readings": [
        reading("b", 3, 61, 23.0),  # 窗口外
        reading("e", 1, 10, 20.0),
    ]})
    assert r.status_code == 422 and "超出窗口" in r.json()["detail"]
    assert set(active_map(client, 1)) == {"a", "c", "d"}


# ---------------------------------------------------------------- 证书与修订影响报告

def test_certificates_and_report_reflect_void_and_restore(client):
    seed_fail_window(client)
    # v1：含 b（fail）
    c1 = client.post("/api/v1/windows/1/certificates/issue", json={"note": "首签"}).json()
    assert c1["verdict"] == "fail"
    assert {r["reading_id"] for r in c1["snapshot"]["readings"]} == {"a", "b", "c", "d"}

    # 作废 b 后签发 v2：快照剔除 b
    void(client, 1, "b", 2, "误采")
    c2 = client.post("/api/v1/windows/1/certificates/issue", json={"note": "作废后"}).json()
    assert c2["version"] == 2
    assert {r["reading_id"] for r in c2["snapshot"]["readings"]} == {"a", "c", "d"}

    # v1 快照保持原样
    old = client.get(f"/api/v1/certificates/{c1['id']}").json()
    assert {r["reading_id"] for r in old["snapshot"]["readings"]} == {"a", "b", "c", "d"}
    assert old["verdict"] == "fail"

    # 修订影响报告 v1->v2：b 出现在 removed
    rep = client.get("/api/v1/certificates/revision-report"
                     f"?old_certificate_id={c1['id']}&new_certificate_id={c2['id']}").json()
    assert [r["reading_id"] for r in rep["readings_diff"]["removed"]] == ["b"]
    assert rep["readings_diff"]["removed_count"] == 1
    assert rep["readings_diff"]["added"] == []

    # 恢复 b 后签发 v3：快照再出现
    submit(client, 1, reading("b", 3, 1, 23.0))
    c3 = client.post("/api/v1/windows/1/certificates/issue", json={"note": "恢复后"}).json()
    assert c3["version"] == 3
    assert {r["reading_id"] for r in c3["snapshot"]["readings"]} == {"a", "b", "c", "d"}

    # 修订影响报告 v2->v3：b 出现在 added（恢复后的新修订号）
    rep2 = client.get("/api/v1/certificates/revision-report"
                      f"?old_certificate_id={c2['id']}&new_certificate_id={c3['id']}").json()
    added = rep2["readings_diff"]["added"]
    assert [r["reading_id"] for r in added] == ["b"]
    assert added[0]["revision"] == 3
    assert rep2["readings_diff"]["removed"] == []
    # v2 快照未被恢复影响
    mid = client.get(f"/api/v1/certificates/{c2['id']}").json()
    assert {r["reading_id"] for r in mid["snapshot"]["readings"]} == {"a", "c", "d"}


def test_void_does_not_change_issued_snapshot(client):
    seed_fail_window(client)
    c1 = client.post("/api/v1/windows/1/certificates/issue", json={}).json()
    void(client, 1, "b", 2, "误采")
    void(client, 1, "c", 2, "误采")
    old = client.get(f"/api/v1/certificates/{c1['id']}").json()
    assert old["snapshot"] == c1["snapshot"]
    assert old["verdict"] == "fail"
