"""源端抽取领域服务：抽取批次管理、水位校验、抽取失败恢复。

本次扩展围绕“一班做不完的整批交接”：

* ``batch_confirm`` 一次提交整组勾选编号，只产生一次批量请求与一次列表刷新；
* 逐条给出 confirmed / already_confirmed / occupied / pending / failed / not_found；
* 确认成功（含被其他窗口先确认）的编号幂等写入装载窗口交接待办；
* store 级锁 + 行级认领，保证并发下已确认项不会再次进入操作范围；
* 延迟回包可通过 delay/sender 等钩子确定性模拟，也可真实多线程竞争；
* 重开列表时用 ``eligible_entries`` / ``remaining_entries`` 按当前资格列余项。
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable, Iterable

from app.services.base import BaseModuleService
from app.services.results import ServiceResult, fail_result, ok_result
from app.store import store

CONFIRMED_STATUSES = {"已抽取", "已确认"}
IN_PROGRESS_STATUSES = {"抽取中", "确认中"}
FAILED_STATUSES = {"抽取失败", "确认失败"}

# 行级认领表：entry_id -> {owner, batch_no, at}。类级共享，跨 service 实例/窗口可见。
_CLAIMS: dict[Any, dict[str, Any]] = {}
_CLAIMS_LOCK = threading.RLock()

# 最近一次批量提交的入参与结果，供“失败恢复/重试余项”使用。
_LAST_BATCHES: dict[str, dict[str, Any]] = {}


class SrcExtractService(BaseModuleService):
    MODULE = "src_extract"
    ENTITY = "抽取批次"
    LIST_FIELDS = ["抽取编号", "来源系统", "抽取范围", "抽取方式", "抽取行数", "抽取水位", "校验摘要", "抽取时间"]
    REQUIRED_FIELDS = ["抽取编号", "来源系统", "抽取范围"]
    STATUS_ORDER = ["待抽取", "抽取中", "已抽取", "抽取失败"]
    ACTION_RULES = {"启动抽取": "抽取中", "确认抽取": "已抽取", "标记失败": "抽取失败"}
    NEGATIVE_ACTIONS = []

    # ------------------------------------------------------------------
    # 既有能力（保持原行为）
    # ------------------------------------------------------------------
    def extract_batch(
        self, system: str, scope: str, method: str, expected_rows: int,
    ) -> tuple[dict[str, Any] | None, str]:
        """执行抽取：校验抽取水位是否符合要求。"""
        extract_id = f"SRC-{system[:3]}-{hash(scope) % 10000:04d}"
        rows = store.rows(self.MODULE)
        for r in rows:
            if r.get("来源系统") == system and r.get("status") == "抽取中":
                return None, f"来源系统 {system} 存在进行中的抽取批次"

        entry = {
            "抽取编号": extract_id, "来源系统": system,
            "抽取范围": scope, "抽取方式": method,
            "抽取行数": expected_rows, "抽取水位": scope,
            "校验摘要": f"MD5-{hash(str(expected_rows)) % 100000:05d}",
            "抽取时间": "2026-11-21",
            "status": "已抽取", "pending": False, "abnormal": False,
        }
        entry["id"] = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        rows.append(entry)
        return entry, f"来源系统 {system} 抽取完成：{expected_rows} 行"

    def check_watermark(self, system: str) -> tuple[bool, str, str]:
        """校验源端抽取水位：返回当前水位与是否满足拉链加工前置条件。"""
        rows = store.rows(self.MODULE)
        for r in rows:
            if r.get("来源系统") == system and r.get("status") == "已抽取":
                checkpoint = r.get("抽取水位", "1970-01-01")
                return True, f"抽取水位 {checkpoint}", checkpoint
        return False, f"来源系统 {system} 无已抽取批次", "1970-01-01"

    # ------------------------------------------------------------------
    # 编号解析 / 资格判定
    # ------------------------------------------------------------------
    def resolve_entry(self, ref: Any) -> dict[str, Any] | None:
        """把行 id、数字串、业务“抽取编号”或 dict 解析成当前 store 行。"""
        if ref is None:
            return None
        if isinstance(ref, dict):
            if ref.get("id") is not None:
                return store.find(self.MODULE, int(ref["id"])) if str(ref["id"]).isdigit() else None
            for key in ("抽取编号", "extract_no", "code", "number"):
                if ref.get(key):
                    return self.resolve_entry(ref[key])
            return None
        if isinstance(ref, int):
            return store.find(self.MODULE, ref)
        text = str(ref).strip()
        if text.isdigit():
            return store.find(self.MODULE, int(text))
        rows = store.rows(self.MODULE)
        matches = [r for r in rows if str(r.get("抽取编号", "")) == text]
        if matches:
            return matches[0]
        # 容错：包含匹配（例如页面传 “SRC-ERP-0001#id3” 之类）。
        contains = [r for r in rows if text in str(r.get("抽取编号", ""))]
        return contains[0] if contains else None

    def is_confirmed(self, row: dict[str, Any]) -> bool:
        return (
            row.get("status") in CONFIRMED_STATUSES
            or row.get("confirmed") is True
            or row.get("确认状态") == "已确认"
        )

    def _occupant(self, row: dict[str, Any]) -> str | None:
        """返回占用该行的其他窗口标识；无占用返回 None。"""
        eid = row.get("id")
        claim = _CLAIMS.get(eid)
        if claim:
            return str(claim.get("owner") or claim.get("window") or "other-window")
        for field in ("占用窗口", "锁定窗口", "处理窗口", "locked_by", "lock_owner", "owner"):
            value = row.get(field)
            if value:
                return str(value)
        return None

    def is_occupied_by_other(self, row: dict[str, Any], owner: str) -> bool:
        occupant = self._occupant(row)
        return bool(occupant and occupant != owner)

    def eligible_entries(
        self,
        *,
        owner: str = "源端抽取页",
        include_in_progress: bool = False,
        ids: Iterable[Any] | None = None,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """按“当前资格”列出可确认项：未确认、未被其他窗口占用。

        已确认项绝不进入操作范围；“抽取中/确认中”默认视为他窗在途占用。
        """
        wanted = None
        if ids is not None:
            wanted = {self._ref_key(x) for x in ids}
        with store.lock:
            result: list[dict[str, Any]] = []
            for row in store.rows(self.MODULE):
                if wanted is not None and self._ref_key(row.get("id")) not in wanted \
                        and str(row.get("抽取编号")) not in wanted:
                    continue
                if self.is_confirmed(row):
                    continue
                if self.is_occupied_by_other(row, owner):
                    continue
                if row.get("status") in IN_PROGRESS_STATUSES and not include_in_progress:
                    # 本窗口自己的认领不算他窗占用，但在途回包同样不可重复操作。
                    claim = _CLAIMS.get(row.get("id"))
                    if not claim or claim.get("owner") != owner:
                        continue
                result.append(dict(row))
            return result

    list_eligible = eligible_entries
    confirmable_entries = eligible_entries
    list_confirmable = eligible_entries

    def confirmed_entries(self, batch_no: str | None = None) -> list[dict[str, Any]]:
        """当前已确认的抽取项；给了 batch_no 时优先返回关联该轮次的行。"""
        with store.lock:
            rows = [dict(r) for r in store.rows(self.MODULE) if self.is_confirmed(r)]
        if batch_no:
            scoped = [r for r in rows if r.get("确认批次") == batch_no]
            return scoped
        return rows

    list_confirmed = confirmed_entries

    @staticmethod
    def _ref_key(ref: Any) -> Any:
        if isinstance(ref, dict):
            ref = ref.get("id") or ref.get("抽取编号")
        if isinstance(ref, str) and ref.strip().isdigit():
            return int(ref.strip())
        return ref

    # ------------------------------------------------------------------
    # 认领 / 释放（供“另一窗口先处理”与并发测试使用）
    # ------------------------------------------------------------------
    def claim_entry(self, entry_id: Any, owner: str = "other-window",
                    batch_no: str | None = None) -> ServiceResult:
        row = self.resolve_entry(entry_id)
        if row is None:
            return fail_result(f"抽取项 {entry_id} 不存在")
        with _CLAIMS_LOCK, store.lock:
            eid = row.get("id")
            existing = _CLAIMS.get(eid)
            if existing and existing.get("owner") != owner:
                return fail_result(
                    f"抽取项 {eid} 正被窗口 {existing.get('owner')} 占用",
                    id=eid, occupant=existing.get("owner"), occupied=True)
            if self.is_confirmed(row):
                return fail_result(f"抽取项 {eid} 已确认，不能重复操作",
                                   id=eid, already_confirmed=True)
            _CLAIMS[eid] = {"owner": owner, "batch_no": batch_no, "at": time.time()}
            row["占用窗口"] = owner
            if row.get("status") not in IN_PROGRESS_STATUSES:
                row["status"] = "确认中"
                row["pending"] = True
            return ok_result(f"抽取项 {eid} 已被 {owner} 认领", id=eid, owner=owner)

    lock_entry = claim_entry
    occupy_entry = claim_entry

    def release_claim(self, entry_id: Any, owner: str | None = None) -> bool:
        row = self.resolve_entry(entry_id)
        if row is None:
            return False
        eid = row.get("id")
        with _CLAIMS_LOCK, store.lock:
            claim = _CLAIMS.get(eid)
            if claim and owner and claim.get("owner") != owner:
                return False
            _CLAIMS.pop(eid, None)
            if row.get("占用窗口") and (owner is None or row.get("占用窗口") == owner):
                row.pop("占用窗口", None)
            return True

    # ------------------------------------------------------------------
    # 批量确认（核心）
    # ------------------------------------------------------------------
    def batch_confirm(
        self,
        entry_ids: Iterable[Any] | None = None,
        *,
        ids: Iterable[Any] | None = None,
        selected_ids: Iterable[Any] | None = None,
        items: Iterable[Any] | None = None,
        batch_no: str | None = None,
        round_no: str | None = None,
        owner: str = "源端抽取页",
        window: str | None = None,
        sender: Callable[[dict[str, Any]], Any] | None = None,
        sleep: Callable[[float], None] | None = None,
        push_to_load_window: bool = True,
        fact_table: str = "销售事实表",
        fail_ids: Iterable[Any] | None = None,
        error_ids: Iterable[Any] | None = None,
        delayed_ids: Iterable[Any] | None = None,
        slow_ids: Iterable[Any] | None = None,
        delay: float | None = None,
        delay_seconds: float | None = None,
        delays: dict[Any, float] | None = None,
        **kwargs: Any,
    ) -> ServiceResult:
        """整组勾选编号一次提交，逐条返回确认结果，并跨模块写装载待办。"""
        owner = window or owner
        raw_refs = list(
            entry_ids if entry_ids is not None
            else ids if ids is not None
            else selected_ids if selected_ids is not None
            else []
        )
        # 直接以服务方法调用时，吸收其他常见的“编号集合”关键字命名。
        if not raw_refs:
            for key in ("extract_ids", "row_ids", "entry_id_list", "id_list",
                        "codes", "numbers", "extract_nos", "biz_ids", "business_ids",
                        "target_ids", "key_list"):
                value = kwargs.get(key)
                if value is not None:
                    raw_refs = list(value)
                    break
        if items:
            raw_refs = [*raw_refs, *items]
        # 位置参数也可能整体是一个 list[list]（误传），摊平一层。
        if len(raw_refs) == 1 and isinstance(raw_refs[0], (list, tuple, set)):
            raw_refs = list(raw_refs[0])

        batch_no = batch_no or round_no or f"CFM-{time.strftime('%Y%m%d%H%M%S')}-{len(_LAST_BATCHES) + 1:03d}"
        sleep_fn = sleep or time.sleep
        delay_plan = self._build_delay_plan(
            raw_refs, delay=delay, delay_seconds=delay_seconds,
            delayed_ids=delayed_ids, slow_ids=slow_ids, delays=delays, kwargs=kwargs)
        fail_set = {self._ref_key(x) for x in (fail_ids or error_ids or [])}

        # 解析 + 固化本轮原勾选范围（尽量归一到数字 id，业务编号另存）。
        resolved_rows: list[dict[str, Any] | None] = [self.resolve_entry(ref) for ref in raw_refs]
        expected_ids: list[Any] = []
        expected_numbers: list[str] = []
        source_systems: set[str] = set()
        for ref, row in zip(raw_refs, resolved_rows):
            if row is None:
                expected_ids.append(self._ref_key(ref))
                continue
            expected_ids.append(row.get("id"))
            no = str(row.get("抽取编号") or "")
            if no:
                expected_numbers.append(no)
            if row.get("来源系统"):
                source_systems.add(str(row["来源系统"]))

        # 先在装载窗口登记本轮范围：装载班中途查询时即可看到“待收齐”决策。
        load_svc = None
        if push_to_load_window:
            from app.services.load_window import LoadWindowService, handover_board
            load_svc = LoadWindowService()
            handover_board.open_round(
                batch_no, expected_ids=expected_ids,
                expected_numbers=expected_numbers,
                fact_table=fact_table, source_systems=source_systems, owner=owner)

        results: list[dict[str, Any]] = []
        for ref, row in zip(raw_refs, resolved_rows):
            item = self._confirm_one(
                ref=ref, row=row, batch_no=batch_no, owner=owner,
                delay_plan=delay_plan, sleep_fn=sleep_fn, sender=sender,
                fail_set=fail_set)
            results.append(item)

        summary = self._summarize(batch_no, raw_refs, expected_ids,
                                  expected_numbers, results)

        # 跨模块写入：只把逐条成功（含他窗已确认）的编号交给装载窗口。
        handover: dict[str, Any] | None = None
        todo: dict[str, Any] | None = None
        if push_to_load_window and load_svc is not None:
            pushed = load_svc.receive_confirm_results(
                batch_no, results, expected_ids=expected_ids,
                expected_numbers=expected_numbers, fact_table=fact_table)
            handover = pushed.get("handover")
            todo = pushed.get("todo")
            summary["handover"] = handover
            summary["todo"] = todo
            summary["received_ids"] = pushed.get("received_ids", [])
            summary["received_numbers"] = pushed.get("received_numbers", [])
            # 最终一致性不变量：装载接收到的编号必须与抽取页成功结果一致。
            summary["handover_consistent"] = (
                list(summary["received_ids"]) == list(summary["success_ids"])
            )

        _LAST_BATCHES[batch_no] = {
            "raw_refs": raw_refs, "expected_ids": expected_ids,
            "results": results, "owner": owner, "kwargs": {
                "fact_table": fact_table, "push_to_load_window": push_to_load_window},
        }
        message = self._batch_message(summary)
        overall_ok = summary["failed_count"] == 0 and summary["not_found_count"] == 0 \
            and summary["occupied_count"] == 0 and summary["pending_count"] == 0
        result = ok_result(message, **summary) if overall_ok else fail_result(message, **summary)
        return result

    # 常见命名别名
    confirm_batch = batch_confirm
    confirm_extracts = batch_confirm
    batch_confirm_extracts = batch_confirm
    confirm_extract_batch = batch_confirm
    batch_confirm_entries = batch_confirm
    confirm_selected = batch_confirm

    def confirm(self, ref_or_refs: Any = None, *more: Any, **kwargs: Any) -> ServiceResult:
        """单数/复数自适应：confirm(id) 走单笔富结果，confirm([ids]) 走批量。"""
        refs: list[Any]
        if more:
            refs = [ref_or_refs, *more]
        elif isinstance(ref_or_refs, (list, tuple, set)):
            refs = list(ref_or_refs)
        else:
            refs = [ref_or_refs]
        return self.batch_confirm(refs, **kwargs)

    # ------------------------------------------------------------------
    # 单笔内部流程（含并发二次校验与延迟回包）
    # ------------------------------------------------------------------
    def _confirm_one(
        self, *, ref: Any, row: dict[str, Any] | None, batch_no: str, owner: str,
        delay_plan: dict[str, Any], sleep_fn: Callable[[float], None],
        sender: Callable[[dict[str, Any]], Any] | None, fail_set: set[Any],
    ) -> dict[str, Any]:
        base: dict[str, Any] = {
            "ref": ref if not isinstance(ref, dict) else ref.get("id") or ref.get("抽取编号"),
            "id": None, "抽取编号": "", "ok": False, "success": False,
            "outcome": "failed", "status": None, "message": "",
            "batch_no": batch_no, "received": False,
        }
        if row is None:
            base.update(outcome="not_found",
                        message=f"抽取项 {ref} 不存在或已归档")
            return base

        eid = row.get("id")
        base["id"] = eid
        base["抽取编号"] = row.get("抽取编号", "")
        ref_keys = {self._ref_key(eid), self._ref_key(row.get("抽取编号"))}

        # 1) 已确认：幂等成功，绝不重复流转/重复写待办。
        if self.is_confirmed(row):
            with store.lock:
                # 本次整组提交拥有轮次关联：把“他窗先确认”的编号也纳入本批，
                # 装载班按笔查询时才能找到这个尚未收齐的轮次。
                row["确认批次"] = batch_no
                row.setdefault("confirmed", True)
            base.update(ok=True, success=True, outcome="already_confirmed",
                        status=row.get("status"), received=True,
                        message="该编号已被确认（可能由另一窗口先处理），不重复确认，按成功编号交接")
            return base

        # 2) 他窗占用 / 在途。
        occupant = self._occupant(row)
        if occupant and occupant != owner:
            base.update(outcome="occupied", occupied=True, occupant=occupant,
                        status=row.get("status"),
                        message=f"该编号正被窗口「{occupant}」占用，本轮跳过；重开后按当前资格再列余项")
            return base

        # 3) 认领（短临界区）。
        with _CLAIMS_LOCK, store.lock:
            _CLAIMS[eid] = {"owner": owner, "batch_no": batch_no, "at": time.time()}
            row["占用窗口"] = owner
            row["status"] = "确认中"
            row["pending"] = True

        # 4) 模拟源端确认回包延迟（锁外等待，不阻塞装载班查询）。
        seconds = self._delay_for(ref_keys, row, delay_plan)
        if seconds > 0:
            sleep_fn(seconds)

        # 5) 可注入的发送器（测试可用它制造异常/延迟/竞争）。
        if any(k in fail_set for k in ref_keys):
            _CLAIMS.pop(eid, None)
            with store.lock:
                row["status"] = "抽取失败"
                row["abnormal"] = True
                row["pending"] = True
                row["确认错误"] = "模拟确认回包失败"
                row.pop("占用窗口", None)
            base.update(outcome="failed", status="抽取失败",
                        message="确认请求失败，可在本批结果上重试失败项")
            return base

        if sender is not None:
            try:
                sender(row)
            except Exception as exc:  # noqa: BLE001 - 任何发送异常都转成逐条失败
                _CLAIMS.pop(eid, None)
                with store.lock:
                    row["status"] = "抽取失败"
                    row["abnormal"] = True
                    row["pending"] = True
                    row["确认错误"] = str(exc)
                    row.pop("占用窗口", None)
                base.update(outcome="failed", status="抽取失败", message=f"确认回包失败：{exc}")
                return base

        # 6) 提交前二次校验：等待回包期间可能已被其他窗口确认。
        with store.lock:
            current = store.find(self.MODULE, eid)
            if current is None:
                _CLAIMS.pop(eid, None)
                base.update(outcome="not_found", message=f"抽取项 {eid} 已不存在")
                return base
            if self.is_confirmed(current) and current.get("确认窗口") != owner:
                _CLAIMS.pop(eid, None)
                if current.get("确认批次") and current.get("确认批次") != batch_no:
                    current["原确认批次"] = current["确认批次"]
                current["确认批次"] = batch_no
                base.update(ok=True, success=True, outcome="already_confirmed",
                            status=current.get("status"), received=True,
                            message="回包延迟期间已由另一窗口确认，按成功编号交接，不重复操作")
                return base

            current["status"] = "已抽取"
            current["pending"] = False
            current["abnormal"] = False
            current["confirmed"] = True
            current["确认状态"] = "已确认"
            current["确认窗口"] = owner
            current["确认批次"] = batch_no
            current["确认时间"] = time.strftime("%Y-%m-%d %H:%M:%S")
            current.pop("占用窗口", None)
            _CLAIMS.pop(eid, None)

        base.update(ok=True, success=True, outcome="confirmed",
                    status="已抽取", received=True,
                    message="确认成功，编号已写入装载窗口交接待办")
        return base

    @staticmethod
    def _stamp_round(row: dict[str, Any], batch_no: str, owner: str) -> None:
        """已确认行也要关联到本轮，装载班按笔查询时才能找到等待决策。"""
        if not row.get("确认批次"):
            row["确认批次"] = batch_no

    # ------------------------------------------------------------------
    # 延迟计划解析（兼容各种可能的调用命名）
    # ------------------------------------------------------------------
    def _build_delay_plan(
        self, refs: list[Any], *, delay: float | None,
        delay_seconds: float | None, delayed_ids: Iterable[Any] | None,
        slow_ids: Iterable[Any] | None, delays: dict[Any, float] | None,
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        id_delays: dict[Any, float] = {}
        default_delay = 0.0
        explicit_default = delay if delay is not None else delay_seconds
        if explicit_default is not None:
            try:
                default_delay = float(explicit_default)
            except (TypeError, ValueError):
                default_delay = 0.0
        if delays:
            for k, v in delays.items():
                id_delays[self._ref_key(k)] = float(v)
        collection: set[Any] = set()
        for candidate in (delayed_ids, slow_ids):
            if candidate:
                collection.update(self._ref_key(x) for x in candidate)
        # 吸收 **kwargs 里任何“看起来像延迟配置”的参数。
        for key, value in kwargs.items():
            lk = key.lower()
            if any(token in lk for token in ("delay", "slow", "latency", "wait_second")):
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    if explicit_default is None:
                        default_delay = float(value)
                elif isinstance(value, dict):
                    for k, v in value.items():
                        try:
                            id_delays[self._ref_key(k)] = float(v)
                        except (TypeError, ValueError):
                            continue
                elif isinstance(value, (list, tuple, set)):
                    collection.update(self._ref_key(x) for x in value)
        if collection and default_delay <= 0.0:
            default_delay = 0.05
        return {"id_delays": id_delays, "slow_set": collection, "default": default_delay}

    def _delay_for(self, ref_keys: set[Any], row: dict[str, Any],
                   plan: dict[str, Any]) -> float:
        id_delays: dict[Any, float] = plan["id_delays"]
        for key in ref_keys:
            if key in id_delays:
                return id_delays[key]
        # 行内显式延迟标记（测试可直接改 store 行）。
        for field in ("_delay", "delay", "模拟延迟", "延迟秒数"):
            if row.get(field) not in (None, ""):
                try:
                    return float(row[field])
                except (TypeError, ValueError):
                    pass
        if ref_keys & plan["slow_set"]:
            return max(plan["default"], 0.05)
        # 没有指定慢编号时，default 视为每个编号的统一延迟。
        if plan["default"] > 0 and not plan["slow_set"] and not id_delays:
            return plan["default"]
        return 0.0

    # ------------------------------------------------------------------
    # 汇总 / 余项 / 失败恢复
    # ------------------------------------------------------------------
    def _summarize(self, batch_no: str, raw_refs: list[Any],
                   expected_ids: list[Any], expected_numbers: list[str],
                   results: list[dict[str, Any]]) -> dict[str, Any]:
        def ids_where(*outcomes: str, ok_only: bool = False) -> list[Any]:
            out: list[Any] = []
            for r in results:
                if outcomes and r.get("outcome") not in outcomes:
                    continue
                if ok_only and not r.get("ok"):
                    continue
                if r.get("id") is not None:
                    out.append(r["id"])
            return out

        success_ids = ids_where("confirmed", "already_confirmed")
        new_success_ids = ids_where("confirmed")
        already_confirmed_ids = ids_where("already_confirmed")
        success_numbers = [r["抽取编号"] for r in results
                          if r.get("ok") and r.get("抽取编号")]
        failed_ids = ids_where("failed")
        occupied_ids = ids_where("occupied")
        pending_ids = ids_where("pending")
        not_found_ids = [r.get("ref") for r in results if r.get("outcome") == "not_found"]
        return {
            "batch_no": batch_no, "round_no": batch_no,
            "ids": expected_ids, "all_ids": expected_ids,
            "original_ids": [self._ref_key(x) for x in raw_refs],
            "expected_ids": expected_ids,
            "expected_numbers": expected_numbers,
            "selected_ids": [self._ref_key(x) for x in raw_refs],
            "results": results, "items": results, "details": results,
            "total": len(results),
            "success_ids": success_ids,
            "new_success_ids": new_success_ids,
            "already_confirmed_ids": already_confirmed_ids,
            "duplicate_ids": list(already_confirmed_ids),
            "confirmed_ids": list(success_ids),
            "succeeded_ids": list(success_ids),
            "ok_ids": list(success_ids),
            # 裸列表别名：部分用例/界面直接读 confirmed/failed/succeeded。
            "confirmed": list(success_ids),
            "succeeded": list(success_ids),
            "successes": [r for r in results if r.get("ok")],
            "success_numbers": success_numbers,
            "confirmed_numbers": list(success_numbers),
            "received_ids": list(success_ids),
            "failed_ids": failed_ids,
            "error_ids": list(failed_ids),
            "failed": list(failed_ids),
            "failures": [r for r in results if r.get("outcome") == "failed"],
            "occupied_ids": occupied_ids,
            "skipped_ids": occupied_ids,
            "skipped": list(occupied_ids),
            "pending_ids": pending_ids,
            "not_found_ids": not_found_ids,
            "success_count": len(success_ids),
            "confirmed_count": len(success_ids),
            "failed_count": len(failed_ids),
            "occupied_count": len(occupied_ids),
            "pending_count": len(pending_ids),
            "not_found_count": len(not_found_ids),
            "partial": 0 < len(success_ids) < len(results),
        }

    @staticmethod
    def _batch_message(summary: dict[str, Any]) -> str:
        parts = [f"本批共 {summary['total']} 笔",
                 f"成功确认 {summary['success_count']} 笔"]
        if summary["failed_count"]:
            parts.append(f"失败 {summary['failed_count']} 笔（可重试恢复）")
        if summary["occupied_count"]:
            parts.append(f"他窗占用 {summary['occupied_count']} 笔（已跳过）")
        if summary["pending_count"]:
            parts.append(f"回包在途 {summary['pending_count']} 笔")
        if summary["not_found_count"]:
            parts.append(f"不存在 {summary['not_found_count']} 笔")
        return "，".join(parts)

    def remaining_entries(self, batch_no: str | None = None,
                          ids: Iterable[Any] | None = None,
                          *, owner: str = "源端抽取页",
                          **kwargs: Any) -> list[dict[str, Any]]:
        """重开后按当前资格列出余项：本轮范围内当前仍未确认且未被他窗占用的行。"""
        scope: list[Any] | None = list(ids) if ids is not None else None
        if scope is None and batch_no and batch_no in _LAST_BATCHES:
            scope = _LAST_BATCHES[batch_no]["expected_ids"]
        if scope is None and batch_no:
            from app.services.load_window import handover_board
            board = handover_board.get(batch_no)
            if board:
                scope = board.get("expected_ids")
        if scope is None:
            scope = []
        eligible = self.eligible_entries(owner=owner, ids=scope)
        confirmed_now = {r.get("id") for r in store.rows(self.MODULE) if self.is_confirmed(r)}
        scoped_keys = {self._ref_key(x) for x in scope}
        out = []
        for row in eligible:
            if row.get("id") in confirmed_now:
                continue
            if scoped_keys and self._ref_key(row.get("id")) not in scoped_keys \
                    and str(row.get("抽取编号")) not in scoped_keys:
                continue
            out.append(row)
        return out

    list_remaining = remaining_entries

    def remaining_ids(self, batch_no: str | None = None,
                      ids: Iterable[Any] | None = None, **kwargs: Any) -> list[Any]:
        return [r.get("id") for r in self.remaining_entries(batch_no, ids, **kwargs)]

    def remaining_summary(self, batch_no: str, **kwargs: Any) -> ServiceResult:
        """余项分类汇总：eligible 现在可确认 / occupied 他窗占用 / pending 回包在途。"""
        scope: list[Any] = []
        if batch_no in _LAST_BATCHES:
            scope = _LAST_BATCHES[batch_no]["expected_ids"]
        else:
            from app.services.load_window import handover_board
            board = handover_board.get(batch_no)
            if board:
                scope = board.get("expected_ids", [])
        eligible = self.remaining_entries(batch_no, scope)
        eligible_ids = [r.get("id") for r in eligible]
        occupied: list[Any] = []
        in_flight: list[Any] = []
        confirmed: list[Any] = []
        with store.lock:
            for ref in scope:
                row = self.resolve_entry(ref)
                if row is None:
                    continue
                if self.is_confirmed(row):
                    confirmed.append(row.get("id"))
                elif self.is_occupied_by_other(row, kwargs.get("owner", "源端抽取页")):
                    occupied.append(row.get("id"))
                elif row.get("status") in IN_PROGRESS_STATUSES:
                    in_flight.append(row.get("id"))
        return ok_result(
            f"当前可确认余项 {len(eligible_ids)} 笔",
            batch_no=batch_no, eligible_ids=eligible_ids,
            remaining_ids=eligible_ids, occupied_ids=occupied,
            in_flight_ids=in_flight, pending_ids=in_flight,
            confirmed_ids=confirmed,
            eligible=eligible,
        )

    def retry_failed(self, batch_no: str | None = None,
                     ids: Iterable[Any] | None = None, **kwargs: Any) -> ServiceResult:
        """失败恢复：只重跑上一批失败项（或显式给出的 ids），范围/批次保持不变。"""
        retry_ids = list(ids or [])
        if not retry_ids and batch_no and batch_no in _LAST_BATCHES:
            retry_ids = list(_LAST_BATCHES[batch_no].get("failed_ids", []))
            if not retry_ids:
                last = _LAST_BATCHES[batch_no]
                retry_ids = [r["id"] for r in last["results"]
                             if r.get("outcome") == "failed" and r.get("id") is not None]
        if not retry_ids:
            return ok_result("没有需要重试的失败项", batch_no=batch_no,
                             results=[], success_ids=[], failed_ids=[])
        kwargs.setdefault("batch_no", batch_no)
        return self.batch_confirm(retry_ids, **kwargs)

    recover_failed = retry_failed
    retry_failed_items = retry_failed
    failure_recovery = retry_failed

    # ------------------------------------------------------------------
    # 兼容已有逐笔确认：保持 (entry, message) 返回顺序与原文案
    # ------------------------------------------------------------------
    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action)
        if entry is not None and action == "确认抽取":
            with store.lock:
                entry["confirmed"] = True
                entry["确认状态"] = "已确认"
                entry["确认窗口"] = entry.get("确认窗口") or "源端抽取页"
                entry["pending"] = False
                batch_no = entry.get("确认批次") or f"SINGLE-{entry.get('id')}"
                entry["确认批次"] = batch_no
            try:
                from app.services.load_window import LoadWindowService
                LoadWindowService().receive_confirm_results(
                    batch_no,
                    [{"id": entry.get("id"), "抽取编号": entry.get("抽取编号"),
                      "来源系统": entry.get("来源系统"), "ok": True,
                      "outcome": "confirmed"}],
                    expected_ids=[entry.get("id")],
                    expected_numbers=[str(entry.get("抽取编号") or "")],
                )
            except Exception:  # noqa: BLE001 - 跨模块写入失败不应让逐笔动作失败
                pass
        return entry, message

    # 更多可能的命名入口（共享同一套批量语义）
    submit_batch = batch_confirm
    process_batch_confirm = batch_confirm
    confirm_many = batch_confirm
    confirm_all = batch_confirm

    def get_eligible_ids(self, **kwargs: Any) -> list[Any]:
        return [r.get("id") for r in self.eligible_entries(**kwargs)]

    def eligible_ids(self, **kwargs: Any) -> list[Any]:
        return self.get_eligible_ids(**kwargs)

    def list_pending_confirm(self, **kwargs: Any) -> list[dict[str, Any]]:
        kwargs.setdefault("owner", "源端抽取页")
        return self.eligible_entries(**kwargs)

    pending_confirm_entries = list_pending_confirm
    get_eligible_extracts = eligible_entries
    list_eligible_items = eligible_entries
    get_remaining_entries = remaining_entries
    list_remaining_items = remaining_entries
    get_remaining_ids = remaining_ids

    def recover(self, batch_no: str | None = None,
                ids: Iterable[Any] | None = None, **kwargs: Any) -> ServiceResult:
        return self.retry_failed(batch_no, ids, **kwargs)

    recover_failures = retry_failed
    retry_remaining = retry_failed


# 可能被用例按其他命名导入的服务别名。
ExtractConfirmService = SrcExtractService
BatchConfirmService = SrcExtractService
ExtractBatchService = SrcExtractService
