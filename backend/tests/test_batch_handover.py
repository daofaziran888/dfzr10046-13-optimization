"""批量确认 + 装载窗口交接的验收链路。

覆盖题目中的实际交班场景：
两个来源的三笔编号，首笔已由另一窗口确认，尾笔延迟返回；
装载班中途先查到首笔时只能“先接收、等收齐”，尾笔返回后才可装载；
最终装载窗口接收的编号与抽取页成功结果严格一致。
"""
from __future__ import annotations

import threading

import pytest

from app.services.load_window import LoadWindowService, handover_board
from app.services.src_extract import SrcExtractService, _CLAIMS
from app.store import store


@pytest.fixture()
def scenario_rows():
    """造两个来源的三笔编号，测试结束清理，避免污染其他用例。"""
    handover_board.reset()
    _CLAIMS.clear()
    rows = store.rows("src_extract")
    base = max((int(r.get("id", 0)) for r in rows), default=0)
    created: list[int] = []
    ids: list[int] = []
    for i, system in enumerate(["ERP", "WMS", "ERP"]):
        eid = base + i + 1
        rows.append({
            "id": eid,
            "抽取编号": f"HANDOVER-{eid}",
            "来源系统": system,
            "抽取范围": f"2026-11-0{i + 1}",
            "抽取方式": "增量抽取",
            "抽取行数": "100",
            "抽取水位": "2026-11-26",
            "校验摘要": "MD5-test",
            "抽取时间": "2026-11-26",
            "status": "待抽取",
            "pending": True,
            "abnormal": False,
        })
        ids.append(eid)
        created.append(eid)
    yield ids
    for eid in created:
        row = store.find("src_extract", eid)
        if row is not None:
            rows.remove(row)
        for todo in [r for r in store.rows("load_window")
                     if r.get("确认批次", "").startswith(("B-HAND-", "CFM-"))]:
            store.rows("load_window").remove(todo)
    handover_board.reset()
    _CLAIMS.clear()


def test_three_ids_two_sources_handover_consistency(scenario_rows):
    src = SrcExtractService()
    lw = LoadWindowService()
    ids = scenario_rows
    batch_no = "B-HAND-MAIN"

    # 1) 另一窗口先把首笔确认掉（逐笔动作口，兼容旧确认）。
    first, message = src.run_action(ids[0], "确认抽取")
    assert first is not None and "已确认抽取" in message

    # 2) 整组三笔一次提交；尾笔通过 sender 屏障模拟延迟回包。
    entered = threading.Event()
    release = threading.Event()

    def sender(row):
        if row["id"] == ids[2]:
            entered.set()
            assert release.wait(timeout=5)

    outcome: dict = {}

    def worker():
        outcome["result"] = src.batch_confirm(
            ids, batch_no=batch_no, owner="抽取页", sender=sender)

    thread = threading.Thread(target=worker)
    thread.start()
    assert entered.wait(timeout=2), "延迟回包屏障未触发"

    # 3) 回包未归、轮次未齐：装载班按首笔编号先查到 -> 可接收但不可装载。
    mid = lw.inspect_extract_for_load(ids[0])
    assert mid["found"] is True
    assert mid["confirmed"] is True
    assert mid["can_load"] is False
    assert "收齐" in mid["load_guide"]

    board = handover_board.get(batch_no)
    assert board is not None
    assert board["received_ids"] == [ids[0]]
    assert set(board["pending_ids"]) == {ids[1], ids[2]}

    # 装载班主动接收同一笔必须幂等，不能重复编号。
    again = lw.receive_from_extract(batch_no, [ids[0]])
    assert again["received_ids"] == [ids[0]]
    can, guide = lw.can_start_loading(batch_no)
    assert can is False and "收齐" in guide

    # 4) 尾笔延迟返回，整批收齐。
    release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    result = outcome["result"]

    outcomes = [item["outcome"] for item in result["results"]]
    assert outcomes == ["already_confirmed", "confirmed", "confirmed"]
    assert result["success_ids"] == ids
    assert result["success_count"] == 3

    # 5) 最终装载窗口接收编号与抽取页成功结果一致（顺序也一致）。
    final_board = handover_board.get(batch_no)
    assert final_board["received_ids"] == result["success_ids"] == ids
    assert final_board["received_numbers"] == result["success_numbers"]
    assert result["handover_consistent"] is True

    readiness = lw.loading_readiness(batch_no)
    assert readiness["can_load"] is True
    assert "直接开装载" in readiness["load_guide"]

    # 待办：每轮恰好一张聚合交接单（原地更新不重复），另有逐编号待办可核对。
    todos = [r for r in store.rows("load_window") if r.get("确认批次") == batch_no]
    aggregate = [r for r in todos if r.get("待办类型") == "源端抽取确认交接"]
    per_id = [r for r in todos if r.get("待办类型") == "源端抽取编号待办"]
    assert len(aggregate) == 1
    assert aggregate[0]["交接状态"] == "已收齐"
    assert aggregate[0]["已接收编号"]
    assert {r["extract_id"] for r in per_id} == set(ids)

    # 已确认项不再进入可操作范围，重开后余项为空。
    assert src.eligible_entries(ids=ids) == []
    assert src.remaining_ids(batch_no) == []


def test_occupied_by_other_window_then_reopen_lists_remaining(scenario_rows):
    src = SrcExtractService()
    ids = scenario_rows

    # 另一窗口占用第二笔（在途），批量确认应跳过它而不是强写。
    claim = src.claim_entry(ids[1], owner="装载组窗口-2")
    assert claim["ok"]

    result = src.batch_confirm(ids, batch_no="B-HAND-OCC", delay=0)
    by_id = {item["id"]: item["outcome"] for item in result["results"]}
    assert by_id[ids[1]] == "occupied"
    assert by_id[ids[0]] in {"confirmed"}
    assert by_id[ids[2]] == "confirmed"
    assert ids[1] not in result["success_ids"]

    # 占用未释放：重开后当前资格里没有被占用的那笔。
    assert src.remaining_ids("B-HAND-OCC") == []
    eligible_now = {r["id"] for r in src.eligible_entries(ids=ids)}
    assert ids[1] not in eligible_now

    # 他窗释放且行回到待抽取：重开后按当前资格列出该余项并可补确认。
    assert src.release_claim(ids[1], owner="装载组窗口-2")
    row = src.resolve_entry(ids[1])
    row["status"] = "待抽取"
    assert src.remaining_ids("B-HAND-OCC") == [ids[1]]
    recovered = src.batch_confirm([ids[1]], batch_no="B-HAND-OCC-2")
    assert recovered["success_count"] == 1


def test_failed_item_retry_recovers_only_failures(scenario_rows):
    src = SrcExtractService()
    ids = scenario_rows
    result = src.batch_confirm(ids, batch_no="B-HAND-FAIL", fail_ids=[ids[2]])
    assert result["success_count"] == 2
    assert result["failed_ids"] == [ids[2]]
    assert result["ok"] is False

    retried = src.retry_failed("B-HAND-FAIL")
    assert retried["success_count"] == 1
    assert retried["failed_count"] == 0
    board = handover_board.get("B-HAND-FAIL")
    assert board is not None
    assert set(board["received_ids"]) == set(ids)


def test_service_result_supports_tuple_and_dict_styles():
    from app.services.results import ServiceResult
    result = ServiceResult(ok=True, message="ok", success_ids=[1])
    ok, message, data = result
    assert ok is True and message == "ok" and data["success_ids"] == [1]
    assert result["success_ids"] == [1]
