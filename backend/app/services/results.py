"""服务层返回结构兼容工具。

本仓库既有服务方法普遍返回 ``(ok, message, data | None)`` 三元组，
而批量确认这类富结果更适合字典。``ServiceResult`` 同时满足两种调用习惯：

* 像普通 dict 一样 ``result["success_ids"]`` / ``result.get(...)``；
* 像三元组一样 ``ok, message, data = service.batch_confirm(...)``；
* 支持 ``result.ok`` / ``result.can_load`` 形式的属性读取。

路由层对外返回前请用 ``plain_dict`` 转成普通 dict，避免序列化分歧。
"""
from __future__ import annotations

from typing import Any, Iterator


class ServiceResult(dict):
    """带三元组拆包能力的字典。"""

    def __iter__(self) -> Iterator[Any]:  # type: ignore[override]
        yield bool(self.get("ok"))
        yield str(self.get("message", ""))
        yield self.data()

    def data(self) -> dict[str, Any]:
        """第三段：去掉 ok/message 顶层控制键后的完整数据字典（仍含二者副本）。"""
        return dict(self)

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:  # pragma: no cover - 调试路径
            raise AttributeError(name) from exc

    def __bool__(self) -> bool:
        return bool(self.get("ok"))


def plain_dict(result: Any) -> Any:
    """把 ServiceResult / 普通对象递归转成 FastAPI 友好的纯 dict/list。"""
    if isinstance(result, dict):
        return {k: plain_dict(v) for k, v in result.items()}
    if isinstance(result, (list, tuple)):
        return [plain_dict(v) for v in result]
    return result


def ok_result(message: str = "", **fields: Any) -> ServiceResult:
    payload = ServiceResult(ok=True, message=message)
    payload.update(fields)
    return payload


def fail_result(message: str = "", **fields: Any) -> ServiceResult:
    payload = ServiceResult(ok=False, message=message)
    payload.update(fields)
    return payload
