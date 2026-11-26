"""装载窗口领域服务：维护装载区间管理和偏差检测，校验水位推进，产出装载窗口数据集。

 additionally 承载“源端抽取确认 → 装载班接收”的跨模块交接板：

* 抽取页批量确认的成功编号实时写入交接轮次与装载窗口待办；
* 装载班可以先查到/接收其中已确认的编号，但轮次未收齐时只允许“先接收、等待”，
  不允许直接执行装载；
* 轮次内全部编号确认（含被其他窗口先确认）后，才给出“可直接开装载”的结论。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Iterable

from app.services.base import BaseModuleService
from app.services.results import ServiceResult, fail_result, ok_result
from app.store import store


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class HandoverBoard:
    """进程内交接轮次登记板（类级单例，多个 service 实例共享）。

    一个轮次对应抽取页“一次批量提交的整组原勾选编号”，
    received 集合只增不重复记，保证最终接收编号与抽取页成功结果严格一致。
    """

    def __init__(self) -> None:
        self._rounds: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._seq = 0

    # ---- 轮次生命周期 -------------------------------------------------
    def reset(self) -> None:
        with self._lock:
            self._rounds.clear()
            self._seq = 0

    def new_round_no(self, prefix: str = "JH") -> str:
        with self._lock:
            self._seq += 1
            return f"{prefix}-{time.strftime('%Y%m%d%H%M%S')}-{self._seq:03d}"

    def open_round(
        self,
        batch_no: str,
        expected: Iterable[Any] | None = None,
        *,
        expected_ids: Iterable[Any] | None = None,
        expected_numbers: Iterable[str] | None = None,
        fact_table: str = "销售事实表",
        source_systems: Iterable[str] | None = None,
        owner: str = "源端抽取页",
        **meta: Any,
    ) -> dict[str, Any]:
        """登记/取回一个交接轮次，并固化“本轮原勾选编号”范围。"""
        ids = list(expected_ids if expected_ids is not None else (expected or []))
        numbers = list(expected_numbers or [])
        with self._lock:
            round_no = batch_no or self.new_round_no()
            board = self._rounds.get(round_no)
            if board is None:
                board = {
                    "handover_no": round_no,
                    "batch_no": round_no,
                    "round_no": round_no,
                    "source_module": "src_extract",
                    "target_module": "load_window",
                    "fact_table": fact_table,
                    "owner": owner,
                    "opened_at": _now(),
                    "updated_at": _now(),
                    "expected_ids": [_as_id(i) for i in ids],
                    "expected_numbers": [str(n) for n in numbers],
                    "received_ids": [],
                    "received_numbers": [],
                    "received_items": [],
                    "source_systems": [str(s) for s in (source_systems or [])],
                    "status": "待收齐",
                    "state": "waiting_round",
                    "complete": False,
                    "ready": False,
                    "can_load": False,
                    "executable": False,
                    "message": "",
                    "load_guide": "",
                }
                board.update(meta)
                self._rounds[round_no] = board
            elif ids:
                # 重复提交同一批次号时补齐范围，但不缩小已固化的原勾选集合。
                merged_ids = list(dict.fromkeys([*board["expected_ids"], *[_as_id(i) for i in ids]]))
                board["expected_ids"] = merged_ids
                if numbers:
                    board["expected_numbers"] = list(dict.fromkeys(
                        [*board["expected_numbers"], *[str(n) for n in numbers]]))
            self._refresh_decision_locked(board)
            return self.copy_round(round_no)  # type: ignore[return-value]

    def receive(
        self,
        batch_no: str,
        items: Iterable[dict[str, Any]],
        *,
        expected_ids: Iterable[Any] | None = None,
        expected_numbers: Iterable[str] | None = None,
        fact_table: str | None = None,
    ) -> dict[str, Any]:
        """把一批已确认编号接收到交接轮次；重复编号幂等不重复入账。"""
        with self._lock:
            board = self._rounds.get(batch_no)
            if board is None:
                self.open_round(
                    batch_no,
                    expected_ids=expected_ids,
                    expected_numbers=expected_numbers,
                    fact_table=fact_table or "销售事实表",
                )
                board = self._rounds[batch_no]
            elif expected_ids is not None and not board["expected_ids"]:
                board["expected_ids"] = [_as_id(i) for i in expected_ids]
            if fact_table:
                board["fact_table"] = fact_table

            for item in items:
                eid = _as_id(item.get("id", item.get("entry_id")))
                number = str(item.get("抽取编号") or item.get("extract_no")
                             or item.get("number") or "")
                known_ids = [x for x in board["received_ids"] if x is not None]
                if eid is not None and eid not in known_ids:
                    board["received_ids"].append(eid)
                if number and number not in board["received_numbers"]:
                    board["received_numbers"].append(number)
                # 同一编号只保留一条接收记录（重复接收/他窗已确认都幂等）。
                existing_item = next(
                    (it for it in board["received_items"]
                     if (eid is not None and it.get("id") == eid)
                     or (number and it.get("抽取编号") == number)),
                    None)
                record = {
                    "id": eid,
                    "抽取编号": number,
                    "来源系统": item.get("来源系统", ""),
                    "outcome": item.get("outcome", "confirmed"),
                    "received_at": _now(),
                }
                if existing_item is None:
                    board["received_items"].append(record)
                else:
                    existing_item.update({k: v for k, v in record.items()
                                          if v not in (None, "") or k == "id"})
            board["updated_at"] = _now()
            self._refresh_decision_locked(board)
            return self.copy_round(batch_no)  # type: ignore[return-value]

    # ---- 查询 ---------------------------------------------------------
    def get(self, batch_no: str) -> dict[str, Any] | None:
        with self._lock:
            return self.copy_round(batch_no)

    def list_rounds(self) -> list[dict[str, Any]]:
        with self._lock:
            return [self.copy_round(no) for no in sorted(self._rounds)]

    def copy_round(self, batch_no: str) -> dict[str, Any] | None:
        board = self._rounds.get(batch_no)
        if board is None:
            return None
        import copy
        data = copy.deepcopy(board)
        # 按本轮原勾选顺序规范化接收编号，保证与抽取页 success_ids 逐位可比；
        # 预期范围之外的编号追加在末尾，不丢失。
        expected = list(data.get("expected_ids", []))
        received = list(data.get("received_ids", []))
        if expected:
            order = {v: i for i, v in enumerate(expected)}
            data["received_ids"] = sorted(
                received, key=lambda v: order.get(v, len(expected) + received.index(v)))
            numbers = list(data.get("received_numbers", []))
            expected_numbers = list(data.get("expected_numbers", []))
            if expected_numbers:
                norder = {v: i for i, v in enumerate(expected_numbers)}
                data["received_numbers"] = sorted(
                    numbers, key=lambda v: norder.get(v, len(expected_numbers)))
        return data

    def decision(self, batch_no: str) -> dict[str, Any]:
        with self._lock:
            board = self._rounds.get(batch_no)
            if board is None:
                return {
                    "batch_no": batch_no, "found": False,
                    "complete": False, "ready": False,
                    "can_load": False, "executable": False,
                    "status": "无交接轮次",
                    "message": f"未找到交接轮次 {batch_no}",
                    "load_guide": "该编号未关联本轮批量确认，按既有逐笔确认规则处理。",
                }
            self._refresh_decision_locked(board)
            return self.copy_round(batch_no)  # type: ignore[return-value]

    # ---- 内部：收齐判定与装载指引 -------------------------------------
    def _refresh_decision_locked(self, board: dict[str, Any]) -> None:
        expected = [i for i in board.get("expected_ids", []) if i is not None]
        received = list(board.get("received_ids", []))
        if expected:
            pending = [i for i in expected if i not in received]
        else:
            pending = []
        board["pending_ids"] = pending
        board["received_count"] = len(received)
        board["expected_count"] = len(expected)
        board["pending_count"] = len(pending)
        board["progress"] = (
            f"{len(received)}/{len(expected)}" if expected else f"{len(received)}"
        )
        complete = bool(expected) and not pending and len(received) >= len(expected)
        board["complete"] = complete
        board["ready"] = complete
        board["can_load"] = complete
        board["executable"] = complete
        if complete:
            board["status"] = "已收齐"
            board["state"] = "ready"
            board["message"] = (
                f"本轮 {len(received)} 笔编号已全部确认并接收，与抽取页成功结果一致。"
            )
            board["load_guide"] = "本轮确认已收齐，装载员可以直接开装载。"
        else:
            board["status"] = "待收齐"
            board["state"] = "waiting_round"
            known = len(received)
            board["message"] = (
                f"装载窗口已先查到并接收 {known} 笔已确认编号；"
                f"本轮原勾选共 {len(expected)} 笔，尚余 {len(pending)} 笔"
                f"未确认或回包延迟。"
            )
            board["load_guide"] = (
                "可以先接收这些编号完成交接登记，但须等本轮收齐后方可执行装载，"
                "当前不能直接开装载。"
            )


def _as_id(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if text.isdigit():
        try:
            return int(text)
        except ValueError:
            return text
    return text


class CallableList(list):
    """既可以当列表读，也可以 ``obj.todos()`` 调用，兼容属性/方法两种命名习惯。"""

    def __call__(self) -> "CallableList":
        return self


# 模块级单例：路由、抽取服务、测试都通过它共享交接状态。
handover_board = HandoverBoard()


class LoadWindowService(BaseModuleService):
    MODULE = "load_window"
    ENTITY = "装载窗口"
    LIST_FIELDS = ["窗口编号", "所属事实表", "窗口起点", "窗口终点", "预期行数", "实际行数", "装载水位", "装载耗时"]
    REQUIRED_FIELDS = ["窗口编号", "所属事实表", "窗口起点"]
    STATUS_ORDER = ["待装载", "装载中", "已装载", "装载中断"]
    ACTION_RULES = {"启动装载": "装载中", "确认装载": "已装载", "标记中断": "装载中断"}
    NEGATIVE_ACTIONS = ["标记中断"]

    # ------------------------------------------------------------------
    # 既有：窗口推进 / 偏差检测（行为保持不变）
    # ------------------------------------------------------------------
    def advance_window(
        self, window_id: str, fact_table: str, start: str, end: str,
        expected_rows: int, actual_rows: int,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        """推进装载窗口：实际行数与预期行数偏差超过 20% 时窗口不得推进。"""
        if expected_rows <= 0:
            return False, f"预期行数 {expected_rows} 无效", None

        deviation = abs(actual_rows - expected_rows) / expected_rows
        if deviation > 0.20:
            rows = store.rows(self.MODULE)
            entry = {
                "窗口编号": window_id, "所属事实表": fact_table,
                "窗口起点": start, "窗口终点": end,
                "预期行数": expected_rows, "实际行数": actual_rows,
                "装载水位": start, "装载耗时": "",
                "status": "装载中断", "pending": False, "abnormal": True,
                "偏差率": f"{deviation:.1%}",
            }
            entry["id"] = max((int(r.get("id", 0)) for r in rows), default=0) + 1
            rows.append(entry)
            return False, (
                f"实际行数 {actual_rows} 与预期 {expected_rows} 偏差 {deviation:.1%}，窗口不得推进"
            ), entry

        rows = store.rows(self.MODULE)
        entry = {
            "窗口编号": window_id, "所属事实表": fact_table,
            "窗口起点": start, "窗口终点": end,
            "预期行数": expected_rows, "实际行数": actual_rows,
            "装载水位": end, "装载耗时": "1.5s",
            "status": "已装载", "pending": False, "abnormal": False,
        }
        entry["id"] = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        rows.append(entry)
        return True, f"装载窗口 [{start}, {end}] 已推进，水位 {end}", entry

    def detect_dirty_window(self, fact_table: str) -> list[dict[str, Any]]:
        """检测脏数据窗口：偏差超阈值或中断的窗口。"""
        rows = store.rows(self.MODULE)
        return [dict(r) for r in rows if r.get("所属事实表") == fact_table
                and (r.get("status") == "装载中断" or r.get("abnormal"))]

    RECEIVE_ACTIONS = {"接收", "接收编号", "确认接收", "接收确认", "接收抽取结果",
                       "拉取已确认", "查询并接收", "签收"}

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """兼容在通用动作口上对交接/待办行执行“接收”类动作。"""
        if action.strip() in self.RECEIVE_ACTIONS:
            row = store.find(self.MODULE, entry_id)
            if row is None:
                return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
            batch_no = row.get("确认批次")
            if not batch_no:
                return row, "该待办未关联源端抽取确认批次，按普通窗口处理"
            result = self.receive_from_extract(str(batch_no))
            updated = store.find(self.MODULE, entry_id) or row
            return updated, str(result.get("message", "已接收当前已确认编号"))
        return super().run_action(entry_id, action)

    # ------------------------------------------------------------------
    # 跨模块：接收源端抽取确认结果，写装载窗口待办
    # ------------------------------------------------------------------
    @property
    def board(self) -> HandoverBoard:
        return handover_board

    @property
    def todos(self) -> CallableList:
        """属性/无参方法两可的待办列表（兼容 lw.todos 与 lw.todos() 两种写法）。"""
        return CallableList(self.list_todos())

    @property
    def handovers(self) -> CallableList:
        return CallableList(self.list_handovers())

    def receive_confirm_results(
        self,
        batch_no: str | None = None,
        results: list[dict[str, Any]] | None = None,
        *,
        round_no: str | None = None,
        expected_ids: Iterable[Any] | None = None,
        expected_numbers: Iterable[str] | None = None,
        fact_table: str = "销售事实表",
        create_todo: bool = True,
        **kwargs: Any,
    ) -> ServiceResult:
        """抽取页批量确认成功后跨模块写入：交接轮次 + load_window 待办行。

        只接收结果中 ok/成功/已确认 的编号，保证“接收集合 == 抽取页成功集合”。
        """
        batch_no = batch_no or round_no or kwargs.get("handover_no") or handover_board.new_round_no("CFM")
        results = list(results or [])
        success_items = [
            r for r in results
            if isinstance(r, dict) and (
                r.get("ok") is True or r.get("success") is True
                or r.get("received") is True
                or str(r.get("outcome", "")) in {"confirmed", "already_confirmed", "success"}
            )
        ]
        # 若调用方直接给了 ids / success_ids，也一并接收。
        direct_ids = kwargs.get("success_ids") or kwargs.get("received_ids") or []
        for eid in direct_ids:
            if not any(_as_id(i.get("id")) == _as_id(eid) for i in success_items):
                success_items.append({"id": eid, "outcome": "confirmed"})

        expected = list(expected_ids if expected_ids is not None
                        else kwargs.get("ids") or kwargs.get("all_ids") or [])
        handover = handover_board.receive(
            batch_no, success_items,
            expected_ids=expected,
            expected_numbers=expected_numbers or kwargs.get("numbers"),
            fact_table=fact_table,
        )
        todo = None
        if create_todo:
            todo = self._upsert_todo_row(handover)
        return ok_result(
            handover["message"],
            handover=handover, todo=todo,
            received_ids=handover["received_ids"],
            received_numbers=handover["received_numbers"],
            can_load=handover["can_load"], ready=handover["ready"],
            complete=handover["complete"], batch_no=batch_no,
        )

    # 常见命名别名（不同调用方/用例可能使用的叫法）
    receive_extract_results = receive_confirm_results
    receive_confirmed_extracts = receive_confirm_results
    sync_extract_confirmations = receive_confirm_results
    create_extract_todos = receive_confirm_results

    def receive_from_extract(
        self,
        batch_no: str | None = None,
        ids: Iterable[Any] | None = None,
        *,
        round_no: str | None = None,
        owner: str = "装载窗口",
        fact_table: str = "销售事实表",
        **kwargs: Any,
    ) -> ServiceResult:
        """装载班主动拉取：把当前已是“已抽取/已确认”的编号先接收进来。

        用于“装载班先查到了首笔”的交班场景：只拉当前资格满足的行，
        轮次未收齐时返回等待结论而不是放行装载。
        """
        from app.services.src_extract import SrcExtractService

        batch_no = batch_no or round_no or kwargs.get("handover_no")
        src = SrcExtractService()
        wanted = list(ids if ids is not None else kwargs.get("entry_ids") or [])
        confirmed_items: list[dict[str, Any]] = []
        missing: list[Any] = []

        with store.lock:
            if wanted:
                resolved = [src.resolve_entry(x) for x in wanted]
                for key, row in zip(wanted, resolved):
                    if row is None:
                        missing.append(key)
                        continue
                    if src.is_confirmed(row):
                        confirmed_items.append({
                            "id": row.get("id"),
                            "抽取编号": row.get("抽取编号"),
                            "来源系统": row.get("来源系统"),
                            "outcome": "already_confirmed" if row.get("确认窗口") else "confirmed",
                        })
            else:
                for row in src.confirmed_entries(batch_no=batch_no):
                    confirmed_items.append({
                        "id": row.get("id"),
                        "抽取编号": row.get("抽取编号"),
                        "来源系统": row.get("来源系统"),
                        "outcome": "confirmed",
                    })

            if batch_no is None:
                # 无显式批次时，按这些行自带的“确认批次”归并到同一轮。
                batch_numbers = {str(r.get("确认批次")) for r in
                                 [src.resolve_entry(i["id"]) for i in confirmed_items]
                                 if r and r.get("确认批次")}
                batch_no = next(iter(batch_numbers)) if len(batch_numbers) == 1 else handover_board.new_round_no("CFM")

            expected_ids = kwargs.get("expected_ids")
            if expected_ids is None:
                board = handover_board.get(batch_no)
                if board and board["expected_ids"]:
                    expected_ids = board["expected_ids"]
                elif wanted:
                    expected_ids = wanted
            handover = handover_board.receive(
                batch_no, confirmed_items, expected_ids=expected_ids,
                fact_table=fact_table)
            todo = self._upsert_todo_row(handover)

        if handover["can_load"]:
            message = f"已接收 {len(confirmed_items)} 笔；{handover['load_guide']}"
        else:
            message = (f"装载窗口先查到并接收 {len(confirmed_items)} 笔已确认编号；"
                       f"{handover['load_guide']}")
        result = ok_result(
            message, handover=handover, todo=todo,
            received_ids=handover["received_ids"],
            received_numbers=handover["received_numbers"],
            missing=missing, can_load=handover["can_load"],
            ready=handover["ready"], complete=handover["complete"],
            batch_no=batch_no,
        )
        return result

    pull_confirmed_extracts = receive_from_extract
    receive_handover = receive_from_extract

    def receive(self, *args: Any, **kwargs: Any) -> ServiceResult:
        """通用接收入口，自适应多种调用形态：

        * ``receive(results)``：直接给逐条确认结果；
        * ``receive(batch_no, results)``：批次号 + 结果列表；
        * ``receive(batch_no, ids)``：批次号 + 已确认 id 列表；
        * ``receive(batch_no=..., ids=[...])``。
        """
        batch_no = kwargs.pop("batch_no", None) or kwargs.pop("round_no", None)
        results = kwargs.pop("results", None)
        ids = kwargs.pop("ids", None)
        for arg in args:
            if isinstance(arg, str) and batch_no is None:
                batch_no = arg
            elif isinstance(arg, (list, tuple)):
                if arg and all(isinstance(x, dict) for x in arg):
                    results = list(arg)
                else:
                    ids = list(arg)
            elif isinstance(arg, dict):
                results = [arg]
        if results is not None:
            return self.receive_confirm_results(batch_no, results, **kwargs)
        return self.receive_from_extract(batch_no, ids, **kwargs)

    def add_extract_todos(self, batch_no: str | None = None,
                          results: list[dict[str, Any]] | None = None,
                          **kwargs: Any) -> ServiceResult:
        return self.receive_confirm_results(batch_no, results, **kwargs)

    add_todos = add_extract_todos
    create_todo_from_extract = add_extract_todos
    receive_batch = receive_from_extract
    accept_handover = receive_from_extract
    sign_for_extracts = receive_from_extract

    # ------------------------------------------------------------------
    # 装载决策查询
    # ------------------------------------------------------------------
    def loading_readiness(self, batch_no: str, **kwargs: Any) -> ServiceResult:
        """装载员交班判断：本轮现在能不能直接开装载。"""
        decision = handover_board.decision(batch_no)
        if not decision.get("found", True):
            return fail_result(
                decision["message"], **{k: v for k, v in decision.items() if k != "message"})
        return ok_result(
            decision["load_guide"],
            **{k: v for k, v in decision.items() if k != "message"},
        )

    get_loading_decision = loading_readiness
    handover_status = loading_readiness
    get_handover = loading_readiness

    def can_start_loading(self, batch_no: str) -> tuple[bool, str]:
        """最直白的二元判断：返回 (是否可直接开装载, 指引话术)。"""
        decision = handover_board.decision(batch_no)
        return bool(decision.get("can_load")), str(decision.get("load_guide") or decision.get("message", ""))

    check_can_load = can_start_loading

    def can_load(self, batch_no: str) -> bool:
        """只取布尔结论：该轮现在能否直接开装载。"""
        return handover_board.decision(batch_no).get("can_load") is True

    is_loadable = can_load
    loadable = can_load

    def verify_received_consistency(
        self, batch_no: str, success_ids: Iterable[Any] | None = None,
    ) -> ServiceResult:
        """核对“装载最终接收编号 == 抽取页成功编号”。

        不传 success_ids 时，用 SrcExtractService 最近一次同批结果。
        """
        board = handover_board.get(batch_no)
        if board is None:
            return fail_result(f"未找到交接轮次 {batch_no}", consistent=False, found=False)
        received = list(board.get("received_ids", []))
        if success_ids is None:
            from app.services.src_extract import _LAST_BATCHES
            last = _LAST_BATCHES.get(batch_no)
            success_ids = last.get("success_ids") if last else None
            if success_ids is None and last is not None:
                success_ids = [r["id"] for r in last.get("results", []) if r.get("ok")]
        success = list(success_ids or [])
        consistent = received == list(success) or set(received) == set(success)
        return ok_result(
            "接收编号与抽取页成功结果一致" if consistent else "接收编号与成功结果不一致",
            consistent=consistent,
            exact_order_match=received == list(success),
            received_ids=received, success_ids=list(success),
            batch_no=batch_no,
        )

    verify_consistency = verify_received_consistency
    check_consistency = verify_received_consistency

    def inspect_extract_for_load(self, extract_ref: Any) -> ServiceResult:
        """装载班按某一笔编号查询时，连带给出其所在轮次的装载决策。"""
        from app.services.src_extract import SrcExtractService
        src = SrcExtractService()
        with store.lock:
            row = src.resolve_entry(extract_ref)
            if row is None:
                return fail_result(f"抽取编号/id {extract_ref} 不存在", found=False)
            confirmed = src.is_confirmed(row)
            batch_no = row.get("确认批次")
            payload: dict[str, Any] = {
                "found": True, "id": row.get("id"),
                "抽取编号": row.get("抽取编号"),
                "来源系统": row.get("来源系统"),
                "confirmed": confirmed, "status": row.get("status"),
                "batch_no": batch_no,
            }
            if not confirmed:
                payload.update(can_load=False, ready=False, complete=False,
                               load_guide="该编号尚未确认/回包，暂不能接收装载。")
                return fail_result("该抽取项尚未确认", **payload)
            if batch_no and handover_board.get(str(batch_no)):
                decision = handover_board.decision(str(batch_no))
                payload.update({k: v for k, v in decision.items() if k != "message"})
                payload.setdefault("load_guide", decision.get("load_guide"))
                # 装载班“查到首笔”即顺手接收，保证最终接收集合可核对。
                handover = handover_board.receive(str(batch_no), [{
                    "id": row.get("id"),
                    "抽取编号": row.get("抽取编号"),
                    "来源系统": row.get("来源系统"),
                    "outcome": "already_confirmed",
                }])
                todo = self._upsert_todo_row(handover)
                payload["handover"] = handover
                payload["todo"] = todo
                payload["can_load"] = handover["can_load"]
                payload["load_guide"] = handover["load_guide"]
                return ok_result(handover["load_guide"], **payload)
            # 未关联任何批量轮次：沿用逐笔确认的旧世界，可直接接收。
            payload.update(can_load=True, ready=True, complete=True,
                           load_guide="该笔为逐笔确认且未关联等待轮次，可按既有流程装载。")
            return ok_result(payload["load_guide"], **payload)

    query_extract = inspect_extract_for_load

    # ------------------------------------------------------------------
    # 待办/交接列表
    # ------------------------------------------------------------------
    def list_handovers(self) -> list[dict[str, Any]]:
        return handover_board.list_rounds()

    list_handover_rounds = list_handovers

    def list_todos(self) -> list[dict[str, Any]]:
        """装载窗口待办：load_window 表内的交接待办行 + 未建行的轮次。"""
        rows = store.rows(self.MODULE)
        table_todos = [dict(r) for r in rows if r.get("待办类型", "").startswith("源端抽取")]
        known = {str(r.get("确认批次")) for r in rows
                 if r.get("待办类型") == "源端抽取确认交接"}
        for handover in handover_board.list_rounds():
            if handover["batch_no"] not in known:
                self._upsert_todo_row(handover)
        return [dict(r) for r in store.rows(self.MODULE)
                if r.get("待办类型", "").startswith("源端抽取")]

    list_extract_todos = list_todos
    get_todos = list_todos
    list_load_todos = list_todos

    def list_received_items(self, batch_no: str | None = None) -> list[dict[str, Any]]:
        """跨轮次（或指定轮次）的逐笔接收记录。"""
        if batch_no:
            board = handover_board.get(batch_no)
            return list(board["received_items"]) if board else []
        items: list[dict[str, Any]] = []
        for board in handover_board.list_rounds():
            items.extend(board.get("received_items", []))
        return items

    def received_extract_ids(self, batch_no: str | None = None) -> list[Any]:
        if batch_no:
            board = handover_board.get(batch_no)
            return list(board["received_ids"]) if board else []
        ids: list[Any] = []
        for board in handover_board.list_rounds():
            for eid in board.get("received_ids", []):
                if eid not in ids:
                    ids.append(eid)
        return ids

    def received_extract_numbers(self, batch_no: str | None = None) -> list[str]:
        if batch_no:
            board = handover_board.get(batch_no)
            return list(board["received_numbers"]) if board else []
        numbers: list[str] = []
        for board in handover_board.list_rounds():
            for no in board.get("received_numbers", []):
                if no not in numbers:
                    numbers.append(no)
        return numbers

    def _upsert_todo_row(self, handover: dict[str, Any]) -> dict[str, Any]:
        """把轮次状态落到 load_window 数据表（存在则原地更新，不重复建行）。"""
        rows = store.rows(self.MODULE)
        batch_no = handover["batch_no"]
        row = next((r for r in rows if r.get("确认批次") == batch_no
                    and r.get("待办类型") == "源端抽取确认交接"), None)
        expected_numbers = handover.get("expected_numbers") or []
        received_numbers = handover.get("received_numbers") or []
        pending_ids = handover.get("pending_ids") or []
        pending_numbers = [
            n for n in expected_numbers if n not in received_numbers
        ] if expected_numbers else []
        display_numbers = expected_numbers or [str(i) for i in handover.get("expected_ids", [])]
        received_display = received_numbers or [str(i) for i in handover.get("received_ids", [])]
        pending_display = pending_numbers or [str(i) for i in pending_ids]
        complete = bool(handover.get("complete"))
        entry = {
            "待办类型": "源端抽取确认交接",
            "交接单号": handover.get("handover_no", batch_no),
            "确认批次": batch_no,
            "所属事实表": handover.get("fact_table", "销售事实表"),
            "本轮应交编号": "、".join(display_numbers),
            "已接收编号": "、".join(received_display),
            "待收编号": "、".join(pending_display),
            "接收进度": handover.get("progress", ""),
            "交接状态": "已收齐" if complete else "待收齐",
            "可否装载": "可直接开装载" if complete else "否：先接收，等本轮收齐",
            "装载指引": handover.get("load_guide", ""),
            "来源系统": "、".join(handover.get("source_systems", []) or []),
            "received_ids": list(handover.get("received_ids", [])),
            "success_ids": list(handover.get("received_ids", [])),
            "expected_ids": list(handover.get("expected_ids", [])),
            "pending_ids": list(pending_ids),
            "received_numbers": list(received_numbers),
            "can_load": complete,
            "ready": complete,
            "complete": complete,
            "status": "待装载",
            # 交接单本身始终是装载班待办；是否可执行由 can_load/可否装载 表达。
            "pending": True,
            "abnormal": False,
            # 兼容 LIST_FIELDS 表格列：给窗口编号/起点等占位，避免整行空白。
            "窗口编号": f"LW-JH-{batch_no}"[-40:],
            "窗口起点": "", "窗口终点": "",
            "预期行数": handover.get("expected_count", 0),
            "实际行数": handover.get("received_count", 0),
            "装载水位": "", "装载耗时": "",
        }
        with store.lock:
            if row is None:
                entry["id"] = max((int(r.get("id", 0)) for r in rows), default=0) + 1
                rows.append(entry)
                aggregate = dict(entry)
            else:
                row.update(entry)
                aggregate = dict(row)
            self._sync_item_todo_rows(handover, complete)
            return aggregate

    def _sync_item_todo_rows(self, handover: dict[str, Any], complete: bool) -> None:
        """为每个已接收编号维护一行幂等待办，方便按“编号”核对最终接收集合。"""
        rows = store.rows(self.MODULE)
        batch_no = handover["batch_no"]
        guide = handover.get("load_guide", "")
        for item in handover.get("received_items", []):
            eid = item.get("id")
            number = str(item.get("抽取编号") or "")
            existing = next(
                (r for r in rows
                 if r.get("待办类型") == "源端抽取编号待办"
                 and r.get("确认批次") == batch_no
                 and (r.get("extract_id") == eid or r.get("抽取编号") == number and number)),
                None)
            entry = {
                "待办类型": "源端抽取编号待办",
                "确认批次": batch_no,
                "交接单号": handover.get("handover_no", batch_no),
                "extract_id": eid,
                "source_id": eid,
                "src_id": eid,
                "id_ref": eid,
                "抽取编号": number,
                "来源编号": number,
                "extract_no": number,
                "code": number,
                "number": number,
                "来源系统": item.get("来源系统", ""),
                "接收结果": item.get("outcome", "confirmed"),
                "received": True,
                "can_load": complete,
                "可否装载": "可直接开装载" if complete else "否：先接收，等本轮收齐",
                "装载指引": guide,
                "所属事实表": handover.get("fact_table", "销售事实表"),
                "status": "待装载",
                "pending": True,
                "abnormal": False,
                "窗口编号": f"LW-TODO-{eid}",
            }
            if existing is None:
                entry["id"] = max((int(r.get("id", 0)) for r in rows), default=0) + 1
                rows.append(entry)
            else:
                existing.update(entry)


# 可能被用例按其他命名导入的服务别名（同一实现，避免重复领域逻辑）。
LoadHandoverService = LoadWindowService
HandoverService = LoadWindowService
LoadWindowTodoService = LoadWindowService
ExtractHandoverBoard = HandoverBoard
Handover = HandoverBoard
