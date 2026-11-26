"""业务路由汇总：批量/交接固定路径在前，通用资源路由在后，避免动态路径抢匹配。"""
from __future__ import annotations

from app.routers import ops, resource

ROUTERS = [ops, resource]
