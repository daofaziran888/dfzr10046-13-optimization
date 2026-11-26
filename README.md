# 维链工坊 · 维度建模与缓慢变化维加工平台

面向维度注册、缓慢变化维拉链、一致性维度核对、事实表装配、代理键生成、维度退化、迟到事实回补、汇总层派生、指标口径绑定与维度快照发布的数仓维度建模加工平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8003/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5176/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8003`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 维度建档 | `dim_entry` | 维度条目 | 维度编号、维度名称、业务主键 |
| 拉链加工 | `scd_chain` | 拉链记录 | 拉链编号、所属维度、代理键 |
| 代理键生成 | `surrogate_key` | 代理键段 | 键段编号、所属维度、键段起点 |
| 一致性核对 | `conform_check` | 核对条目 | 核对编号、基准维度、对照维度 |
| 事实装配 | `fact_assemble` | 事实批次 | 批次编号、所属事实表、度量列集合 |
| 退化维度 | `degenerate_dim` | 退化维条目 | 条目编号、所属事实表、退化字段 |
| 迟到事实 | `late_fact` | 迟到记录 | 记录编号、所属事实表、业务发生日 |
| 汇总层派生 | `summary_layer` | 汇总条目 | 汇总编号、来源事实表、汇总粒度 |
| 粒度声明 | `grain_declare` | 粒度声明 | 声明编号、所属事实表、一行代表 |
| 度量目录 | `measure_catalog` | 度量条目 | 度量编号、度量名称、计算表达式 |
| 指标绑定 | `metric_bind` | 绑定关系 | 绑定编号、指标名称、引用度量 |
| 维度快照 | `dim_snapshot` | 快照版本 | 快照编号、所属维度、快照日期 |
| 层级路径 | `hier_path` | 层级路径 | 路径编号、所属维度、层级名称 |
| 多值桥接 | `bridge_link` | 桥接条目 | 桥接编号、所属事实表、关联维度 |
| 属性失效 | `attr_decay` | 失效条目 | 失效编号、所属维度、属性名称 |
| 装载窗口 | `load_window` | 装载窗口 | 窗口编号、所属事实表、窗口起点 |
| 重跑任务 | `reprocess_task` | 重跑任务 | 重跑编号、触发原因、影响事实表 |
| 装配质量闸 | `quality_gate` | 质量闸条目 | 闸口编号、所属事实表、校验项 |
| 血缘关系 | `lineage_edge` | 血缘边 | 血缘编号、上游对象、下游对象 |
| 维度发布 | `dim_publish` | 发布版本 | 发布编号、所属维度、发布版本 |
| 建模审计 | `model_audit` | 审计条目 | 条目编号、操作类型、操作对象 |
| 源端抽取 | `src_extract` | 抽取批次 | 抽取编号、来源系统、抽取范围 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 源端抽取：批量确认与装载窗口交班

- 抽取页支持多选，整组编号作为一次提交范围：`POST /api/src_extract/batch-confirm`
  （同义路径 `/batch_confirm`、`/confirm-batch`、`/confirm`、`/batch`），
  body：`{ "ids": [1, 2, 3], "batch_no": "CFM-..." }`。响应逐条给出
  `results[].outcome`：`confirmed / already_confirmed / occupied / pending / failed / not_found`，
  并汇总 `success_ids / failed_ids / occupied_ids / remaining_ids`。
- 批量确认只发一次请求、成功后只刷新一次列表；相同参数的在途列表请求会合并。
  失败项用 `POST /api/src_extract/retry-failed`（或页面“重试未成功余项”）恢复。
- 确认成功（含被另一窗口先确认）的编号幂等写入装载窗口待办：
  `GET /api/load_window/todos`、`GET /api/load_window/handovers/{batch_no}`、
  `POST /api/load_window/receive`、`GET /api/load_window/inspect/{id或抽取编号}`。
- 交班结论：轮次未收齐时 `can_load=false`，装载员“可先接收，但须等本轮收齐后方可执行装载”；
  全部编号确认后 `can_load=true`，提示“可直接开装载”。最终
  `handover.received_ids == result.success_ids`。
- 并发：store 锁 + 行级认领（`SrcExtractService.claim_entry`）保证已确认项不重复操作；
  `GET /api/src_extract/eligible` 与 `GET /api/src_extract/remaining?batch_no=...`
  在重开后按当前资格列出余项。批量接口支持 `delayed_ids` + `delay`（或注入 `sender`）
  确定性模拟回包延迟，阻塞调用在线程池执行，不挡住装载班的并发查询。
- 既有逐笔动作 `POST /api/src_extract/{id}/actions {"action":"确认抽取"}` 保持兼容，
  同样会写一条 `SINGLE-{id}` 装载待办。


领域：数据工程
子方向：数仓与数据湖
