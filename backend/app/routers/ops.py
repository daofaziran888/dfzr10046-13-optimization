"""批量确认 / 交接 / 装载就绪等非 CRUD 动作路由。

这些固定路径必须先于 resource.py 的 ``/{module}/{entry_id}`` 动态路由注册，
否则 eligible / todos 等会被当成 entry_id 并在 int 校验处报 422。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from app.services import service_for
from app.services.results import plain_dict

router = APIRouter(prefix="/api", tags=["批量确认与交班"])


async def _body(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001 - 空 body 时按空参数处理
        return {}
    return data if isinstance(data, dict) else {}


def _src_service():
    service = service_for("src_extract")
    if service is None:  # pragma: no cover - 注册表固定包含
        raise HTTPException(status_code=404, detail="未知业务模块：src_extract")
    return service


def _load_service():
    service = service_for("load_window")
    if service is None:  # pragma: no cover
        raise HTTPException(status_code=404, detail="未知业务模块：load_window")
    return service


# ======================================================================
# 源端抽取：批量确认 / 资格余项 / 失败恢复
# ======================================================================
async def _batch_confirm(request: Request) -> dict[str, Any]:
    payload = await _body(request)
    service = _src_service()
    ids = (payload.get("ids") if payload.get("ids") is not None
           else payload.get("entry_ids") if payload.get("entry_ids") is not None
           else payload.get("selected_ids") if payload.get("selected_ids") is not None
           else payload.get("extract_ids"))
    # 允许 {id: 1, action: "批量确认"} 这种单对象写法。
    if ids is None and payload.get("id") is not None:
        ids = [payload.get("id")]
    extra = {k: v for k, v in payload.items()
             if k not in {"ids", "entry_ids", "selected_ids", "extract_ids", "id"}}
    # 服务层含可注入的延迟/锁等待，必须放到线程池，避免阻塞事件循环导致
    # 装载班的并发查询请求被批量确认全程挡住。
    result = await run_in_threadpool(service.batch_confirm, ids or [], **extra)
    return plain_dict(result)


@router.post("/src_extract/batch-confirm")
async def batch_confirm_hyphen(request: Request) -> dict[str, Any]:
    return await _batch_confirm(request)


@router.post("/src_extract/batch_confirm")
async def batch_confirm_underscore(request: Request) -> dict[str, Any]:
    return await _batch_confirm(request)


@router.post("/src_extract/confirm-batch")
async def confirm_batch_path(request: Request) -> dict[str, Any]:
    return await _batch_confirm(request)


@router.post("/src_extract/confirm_batch")
async def confirm_batch_underscore(request: Request) -> dict[str, Any]:
    return await _batch_confirm(request)


@router.post("/src_extract/confirm")
async def confirm_path(request: Request) -> dict[str, Any]:
    return await _batch_confirm(request)


@router.post("/src_extract/batch")
async def batch_path(request: Request) -> dict[str, Any]:
    return await _batch_confirm(request)


@router.post("/src_extract/actions/batch")
async def actions_batch_path(request: Request) -> dict[str, Any]:
    return await _batch_confirm(request)


@router.post("/src_extract/actions/batch-confirm")
async def batch_confirm_action_path(request: Request) -> dict[str, Any]:
    return await _batch_confirm(request)


@router.post("/src_extract/actions/batch_confirm")
async def batch_confirm_action_underscore(request: Request) -> dict[str, Any]:
    return await _batch_confirm(request)


@router.get("/src_extract/eligible")
def eligible(owner: str = "源端抽取页", include_in_progress: bool = False) -> dict[str, Any]:
    service = _src_service()
    items = service.eligible_entries(owner=owner, include_in_progress=include_in_progress)
    return {"items": plain_dict(items), "total": len(items),
            "eligible_ids": [r.get("id") for r in items]}


@router.get("/src_extract/remaining")
def remaining(batch_no: str | None = None, owner: str = "源端抽取页") -> dict[str, Any]:
    service = _src_service()
    items = service.remaining_entries(batch_no, owner=owner)
    summary = service.remaining_summary(batch_no, owner=owner) if batch_no else None
    return {"batch_no": batch_no, "items": plain_dict(items), "total": len(items),
            "remaining_ids": [r.get("id") for r in items],
            "summary": plain_dict(summary)}


@router.get("/src_extract/confirmable")
def confirmable(owner: str = "源端抽取页") -> dict[str, Any]:
    return eligible(owner=owner)


@router.post("/src_extract/retry-failed")
async def retry_failed(request: Request) -> dict[str, Any]:
    payload = await _body(request)
    service = _src_service()
    result = await run_in_threadpool(
        service.retry_failed,
        payload.get("batch_no") or payload.get("round_no"),
        payload.get("ids"),
        **{k: v for k, v in payload.items()
           if k not in {"batch_no", "round_no", "ids"}})
    return plain_dict(result)


@router.post("/src_extract/claim")
async def claim(request: Request) -> dict[str, Any]:
    payload = await _body(request)
    service = _src_service()
    result = service.claim_entry(
        payload.get("id") or payload.get("entry_id"),
        owner=payload.get("owner") or payload.get("window") or "other-window",
        batch_no=payload.get("batch_no"))
    return plain_dict(result)


# ======================================================================
# 装载窗口：交接轮次 / 接收 / 就绪判断 / 按笔查询
# ======================================================================
@router.get("/load_window/handovers")
def list_handovers() -> dict[str, Any]:
    service = _load_service()
    rounds = service.list_handovers()
    return {"items": plain_dict(rounds), "total": len(rounds)}


@router.get("/load_window/handovers/{batch_no}")
def handover_detail(batch_no: str) -> dict[str, Any]:
    service = _load_service()
    result = service.loading_readiness(batch_no)
    if not result.get("found", True):
        raise HTTPException(status_code=404, detail=result.get("message"))
    return plain_dict(result)


@router.get("/load_window/handover")
def handover_query(batch_no: str) -> dict[str, Any]:
    return handover_detail(batch_no)


@router.get("/load_window/todos")
def load_todos() -> dict[str, Any]:
    service = _load_service()
    todos = service.list_todos()
    return {"items": plain_dict(todos), "total": len(todos)}


@router.get("/load_window/received")
def load_received(batch_no: str | None = None) -> dict[str, Any]:
    service = _load_service()
    return {
        "batch_no": batch_no,
        "items": plain_dict(service.list_received_items(batch_no)),
        "received_ids": service.received_extract_ids(batch_no),
        "received_numbers": service.received_extract_numbers(batch_no),
    }


@router.post("/load_window/receive")
async def receive(request: Request) -> dict[str, Any]:
    payload = await _body(request)
    service = _load_service()
    result = service.receive_from_extract(
        payload.get("batch_no") or payload.get("round_no") or payload.get("handover_no"),
        payload.get("ids") or payload.get("entry_ids"),
        owner=payload.get("owner") or "装载窗口",
        fact_table=payload.get("fact_table") or "销售事实表",
        expected_ids=payload.get("expected_ids"))
    return plain_dict(result)


@router.get("/load_window/readiness/{batch_no}")
def readiness(batch_no: str) -> dict[str, Any]:
    service = _load_service()
    return plain_dict(service.loading_readiness(batch_no))


@router.get("/load_window/inspect/{ref}")
def inspect_for_load(ref: str) -> dict[str, Any]:
    service = _load_service()
    return plain_dict(service.inspect_extract_for_load(ref))


# ======================================================================
# 通用模块批量动作入口：POST /api/{module}/batch-confirm
# ======================================================================
@router.post("/{module}/batch-confirm")
async def generic_batch_confirm(module: str, request: Request) -> dict[str, Any]:
    if module == "src_extract":
        return await _batch_confirm(request)
    service = service_for(module)
    if service is None:
        raise HTTPException(status_code=404, detail=f"未知业务模块：{module}")
    if not hasattr(service, "batch_confirm"):
        raise HTTPException(status_code=400, detail=f"模块 {module} 不支持批量确认")
    payload = await _body(request)
    result = service.batch_confirm(payload.get("ids") or payload.get("entry_ids") or [])
    return plain_dict(result)
