/**
 * 源端抽取批量确认 / 交班范围的纯前端领域逻辑。
 *
 * 这里不发请求、不依赖 Vue，方便单测覆盖：
 * - 勾选集合在筛选/刷新后仍保留原编号；
 * - 已确认项按当前资格移出“可操作范围”；
 * - 逐条结果汇总与装载窗口交班话术。
 */

export type RowLike = {
  id: number | string
  status?: string
  confirmed?: boolean
  确认状态?: string
  抽取编号?: string
  [key: string]: unknown
}

export type ConfirmOutcome =
  | 'confirmed'
  | 'already_confirmed'
  | 'occupied'
  | 'pending'
  | 'failed'
  | 'not_found'
  | string

export interface ItemResult {
  id: number | string | null
  ref?: number | string | null
  抽取编号?: string
  ok: boolean
  success?: boolean
  outcome: ConfirmOutcome
  status?: string | null
  message?: string
  occupied?: boolean
}

export interface HandoverInfo {
  batch_no?: string
  received_ids?: Array<number | string>
  received_numbers?: string[]
  expected_ids?: Array<number | string>
  pending_ids?: Array<number | string>
  complete?: boolean
  can_load?: boolean
  ready?: boolean
  load_guide?: string
  message?: string
  progress?: string
  status?: string
  [key: string]: unknown
}

export interface BatchConfirmResult {
  ok: boolean
  message: string
  batch_no?: string
  total?: number
  success_count?: number
  failed_count?: number
  occupied_count?: number
  pending_count?: number
  not_found_count?: number
  success_ids?: Array<number | string>
  failed_ids?: Array<number | string>
  occupied_ids?: Array<number | string>
  pending_ids?: Array<number | string>
  results?: ItemResult[]
  items?: ItemResult[]
  handover?: HandoverInfo | null
  handover_consistent?: boolean
  [key: string]: unknown
}

/** 已确认终态：与后端 SrcExtractService.is_confirmed 保持一致。 */
export function isConfirmedRow(row: RowLike | null | undefined): boolean {
  if (!row) return false
  return (
    row.status === '已抽取' ||
    row.status === '已确认' ||
    row.confirmed === true ||
    row.确认状态 === '已确认'
  )
}

/** 他窗占用 / 在途状态：不可重复提交（与后端资格口径一致）。 */
export function isOccupiedRow(row: RowLike | null | undefined): boolean {
  if (!row) return false
  if (row.占用窗口 || row.locked_by || row.lock_owner) return true
  return row.status === '确认中' || row.status === '抽取中'
}

/** 当前资格：未确认且未被他窗占用的行，才能进入批量操作范围。 */
export function isConfirmableRow(row: RowLike | null | undefined): boolean {
  return !!row && !isConfirmedRow(row) && !isOccupiedRow(row)
}

export function toggleId(ids: Array<number | string>, id: number | string): Array<number | string> {
  return ids.includes(id) ? ids.filter((x) => x !== id) : [...ids, id]
}

/** 全选/取消当前可见的可确认行；不可确认行永远不会被选入。 */
export function toggleAllVisible(
  selected: Array<number | string>,
  visibleRows: RowLike[],
): Array<number | string> {
  const selectable = visibleRows.filter(isConfirmableRow).map((row) => row.id)
  const allSelected = selectable.length > 0 && selectable.every((id) => selected.includes(id))
  if (allSelected) {
    return selected.filter((id) => !selectable.includes(id))
  }
  return Array.from(new Set([...selected, ...selectable]))
}

/**
 * 刷新/重开后按“当前资格”校正勾选：
 * 已确认或被占用的编号移出可提交集合，但调用方通常会另外保留 batchScope 留痕。
 * 注意：不可见（被筛选掉）的已选编号一律保留 —— 改条件不能丢原勾选。
 */
export function pruneToEligible(
  selected: Array<number | string>,
  rows: RowLike[],
): { selected: Array<number | string>; disqualified: Array<number | string> } {
  const byId = new Map(rows.map((row) => [row.id, row]))
  const selectedSet = new Set(selected)
  const disqualified: Array<number | string> = []
  for (const id of selected) {
    const row = byId.get(id)
    // 当前列表里找不到：可能只是被筛选条件隐藏，保留原勾选。
    if (!row) continue
    if (!isConfirmableRow(row)) {
      selectedSet.delete(id)
      disqualified.push(id)
    }
  }
  return { selected: [...selectedSet], disqualified }
}

export type OutcomeTone = 'success' | 'warning' | 'danger' | 'muted'

export const OUTCOME_META: Record<string, { label: string; tone: OutcomeTone }> = {
  confirmed: { label: '确认成功', tone: 'success' },
  already_confirmed: { label: '他窗已确认·按成功接收', tone: 'success' },
  occupied: { label: '他窗占用·已跳过', tone: 'warning' },
  pending: { label: '回包在途·待收齐', tone: 'warning' },
  failed: { label: '确认失败·可重试', tone: 'danger' },
  not_found: { label: '编号不存在', tone: 'danger' },
}

export function outcomeMeta(outcome: string): { label: string; tone: OutcomeTone } {
  return OUTCOME_META[outcome] ?? { label: outcome || '未知结果', tone: 'muted' }
}

export function resultList(result: BatchConfirmResult | null | undefined): ItemResult[] {
  return result?.results ?? result?.items ?? []
}

/** 本批失败/占用/在途等未成功编号，供“只重试余项”。 */
export function recoverableIds(result: BatchConfirmResult | null | undefined): Array<number | string> {
  if (!result) return []
  const fromItems = resultList(result)
    .filter((item) => !item.ok && item.id != null && item.outcome !== 'not_found')
    .map((item) => item.id as number | string)
  if (fromItems.length) return fromItems
  return [...(result.failed_ids ?? []), ...(result.occupied_ids ?? [])]
}

/** 装载窗口交班话术：明确“能不能直接开装载”。 */
export function loaderDecision(h: HandoverInfo | null | undefined): {
  canLoad: boolean
  title: string
  guide: string
} {
  if (!h) {
    return { canLoad: false, title: '暂无交接轮次', guide: '尚未收到源端抽取确认结果。' }
  }
  const canLoad = h.can_load === true || h.complete === true || h.ready === true
  if (canLoad) {
    return {
      canLoad: true,
      title: '本轮已收齐 · 可直接开装载',
      guide: h.load_guide || h.message || '本轮编号全部确认并接收，可直接开装载。',
    }
  }
  return {
    canLoad: false,
    title: '先接收 · 须等本轮收齐后方可执行装载',
    guide: h.load_guide || h.message || '已先接收部分编号，须等本轮收齐后方可执行装载。',
  }
}

const SELECTION_KEY = 'src_extract:selected_ids:v1'
const SCOPE_KEY = 'src_extract:batch_scope:v1'

/** localStorage 持久化：重开页面后仍能恢复“原勾选编号”，再按当前资格校正。 */
export function loadPersistedIds(key: string = SELECTION_KEY): Array<number | string> {
  try {
    const raw = window.localStorage.getItem(key)
    const parsed = raw ? JSON.parse(raw) : []
    return Array.isArray(parsed) ? parsed : []
  } catch {
    return []
  }
}

export function persistIds(ids: Array<number | string>, key: string = SELECTION_KEY): void {
  try {
    window.localStorage.setItem(key, JSON.stringify(ids))
  } catch {
    /* 隐私模式等场景忽略持久化失败 */
  }
}

export function loadBatchScope<T = unknown>(key: string = SCOPE_KEY): T | null {
  try {
    const raw = window.localStorage.getItem(key)
    return raw ? (JSON.parse(raw) as T) : null
  } catch {
    return null
  }
}

export function persistBatchScope(scope: unknown, key: string = SCOPE_KEY): void {
  try {
    window.localStorage.setItem(key, JSON.stringify(scope))
  } catch {
    /* ignore */
  }
}

export const STORAGE_KEYS = { SELECTION_KEY, SCOPE_KEY }

const srcExtractDomain = {
  isConfirmedRow,
  isOccupiedRow,
  isConfirmableRow,
  toggleId,
  toggleAllVisible,
  pruneToEligible,
  outcomeMeta,
  resultList,
  recoverableIds,
  loaderDecision,
  loadPersistedIds,
  persistIds,
  loadBatchScope,
  persistBatchScope,
  STORAGE_KEYS,
}

export default srcExtractDomain
