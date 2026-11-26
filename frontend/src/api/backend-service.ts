import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'
import type { BatchConfirmResult, HandoverInfo } from '@/domain/src_extract'

export { moduleMeta }

/**
 * 相同参数的列表请求在途合并：
 * 确认/刷新调用链若被并发触发多次，实际只发出一个 GET，从根上消除重复请求。
 */
const inflightLists = new Map<string, Promise<PageResult>>()

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const cacheKey = `${key}?${query}`
  const existing = inflightLists.get(cacheKey)
  if (existing) return existing
  const promise = (async () => {
    try {
      const response = await request(`/api/${key}?${query}`)
      if (!response.ok) throw new Error('业务数据读取失败')
      return (await response.json()) as PageResult
    } finally {
      inflightLists.delete(cacheKey)
    }
  })()
  inflightLists.set(cacheKey, promise)
  return promise
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const response = await request(path, {
    method: 'POST',
    body: JSON.stringify(body ?? {}),
  })
  if (!response.ok) {
    let detail = ''
    try {
      detail = (await response.json())?.detail ?? ''
    } catch {
      /* 非 JSON 错误体忽略 */
    }
    throw new Error(detail ? `请求被拒绝：${detail}` : `接口返回 ${response.status}`)
  }
  return (await response.json()) as T
}

/** 源端抽取：整组勾选编号一次批量确认（后端逐条给出结果并写装载窗口待办）。 */
export function batchConfirmExtract(payload: {
  ids: Array<number | string>
  batch_no?: string
  owner?: string
  delayed_ids?: Array<number | string>
  delay?: number
  [key: string]: unknown
}): Promise<BatchConfirmResult> {
  return postJson<BatchConfirmResult>('/api/src_extract/batch-confirm', payload)
}

/** 源端抽取：只重试上一批失败/在途项。 */
export function retryFailedExtracts(payload: {
  batch_no?: string
  ids?: Array<number | string>
}): Promise<BatchConfirmResult> {
  return postJson<BatchConfirmResult>('/api/src_extract/retry-failed', payload)
}

export async function listEligibleExtracts(): Promise<{
  items: EntryRow[]
  eligible_ids: Array<number>
  total: number
}> {
  const response = await request('/api/src_extract/eligible')
  if (!response.ok) throw new Error('可确认余项读取失败')
  return response.json()
}

export async function listRemainingExtracts(batchNo?: string): Promise<{
  items: EntryRow[]
  remaining_ids: Array<number>
  total: number
}> {
  const query = batchNo ? `?batch_no=${encodeURIComponent(batchNo)}` : ''
  const response = await request(`/api/src_extract/remaining${query}`)
  if (!response.ok) throw new Error('余项读取失败')
  return response.json()
}

export async function listLoadHandovers(): Promise<{ items: HandoverInfo[]; total: number }> {
  const response = await request('/api/load_window/handovers')
  if (!response.ok) throw new Error('装载交接轮次读取失败')
  return response.json()
}

export function receiveExtractsForLoad(payload: {
  batch_no?: string
  ids?: Array<number | string>
  owner?: string
}): Promise<{ ok: boolean; message: string; handover?: HandoverInfo; can_load?: boolean }> {
  return postJson('/api/load_window/receive', payload)
}

export async function getLoadReadiness(batchNo: string): Promise<HandoverInfo & { ok?: boolean }> {
  const response = await request(`/api/load_window/readiness/${encodeURIComponent(batchNo)}`)
  if (!response.ok) throw new Error('装载就绪判断读取失败')
  return response.json()
}

export async function inspectExtractForLoad(
  ref: number | string,
): Promise<HandoverInfo & { ok?: boolean; confirmed?: boolean }> {
  const response = await request(`/api/load_window/inspect/${encodeURIComponent(String(ref))}`)
  return response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}
