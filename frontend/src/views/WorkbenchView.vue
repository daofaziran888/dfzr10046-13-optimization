<template>
  <section class="page" :data-module="meta.key">
    <header class="page-head">
      <div>
        <h2>{{ meta.name }}</h2>
        <p class="page-desc">{{ meta.desc }}</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">
          登记{{ meta.entity }}
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">
          导出{{ meta.name }}清单
        </button>
      </div>
    </header>

    <!-- 装载窗口页：交班轮次与“能不能开装载”的判断 -->
    <LoadHandoverPanel v-if="isLoadWindow" />

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p class="status-legend">
      <span v-for="item in statusSummary" :key="item.status" class="legend-item">
        {{ item.status }}：{{ item.count }}
      </span>
    </p>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <!-- 源端抽取：批量确认操作条 + 逐条结果 + 交接结论 -->
    <ExtractBatchPanel
      v-if="isExtract"
      :selected-count="selectedIds.length"
      :submitting="batchSubmitting"
      :result="batchResult"
      :scope-items="scopeItems"
      @confirm="submitBatch"
      @retry="retryUnfinished"
      @clear="clearSelection"
    />

    <!-- 重开提示：按当前资格，上一批还剩哪些可确认余项 -->
    <p v-if="isExtract && reopenRemaining.length" class="reopen-tip" data-testid="reopen-remaining">
      按当前资格，上一批还有 {{ reopenRemaining.length }} 笔可确认余项：
      <button class="link" type="button" data-testid="pick-remaining" @click="pickReopenRemaining">
        勾选余项
      </button>
    </p>

    <table class="data-table">
      <thead>
        <tr>
          <th v-if="isExtract" class="check-col">
            <label data-testid="select-all" title="全选当前可见的可确认项">
              <input
                ref="selectAllRef"
                type="checkbox"
                data-testid="checkbox-select-all"
                aria-label="全选"
                :checked="allVisibleSelected"
                @change="toggleSelectAll"
              />
            </label>
          </th>
          <th v-for="column in meta.fields" :key="column">{{ column }}</th>
          <th>当前状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="row in rows"
          :key="String(row.id)"
          :data-testid="`row-${row.id}`"
          :data-status="String(row.status)"
          :data-confirmed="isExtract ? String(isConfirmedRow(row)) : undefined"
        >
          <td v-if="isExtract" class="check-col">
            <label :data-testid="`select-${row.id}`" :title="`选择 ${row['抽取编号'] ?? row.id}`">
              <input
                type="checkbox"
                :data-testid="`checkbox-${row.id}`"
                :data-row-checkbox="String(row.id)"
                :data-select-id="String(row.id)"
                :aria-label="`选择抽取项 ${row['抽取编号'] ?? row.id}`"
                :checked="isRowSelected(row)"
                :disabled="!isConfirmableRow(row)"
                @change="toggleRow(row.id)"
              />
            </label>
          </td>
          <td v-for="column in meta.fields" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row.status }}</td>
          <td class="row-actions">
            <button
              v-for="action in meta.actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="meta.fields.length + 2 + (isExtract ? 1 : 0)" class="empty-state">
            暂无{{ meta.name }}数据，可先登记{{ meta.entity }}
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记{{ meta.entity }}</h3>
        <label v-for="field in meta.fields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条{{ meta.name }}记录</span>
      <span v-if="errorMessage" class="error-text" data-testid="error-message">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'

import ExtractBatchPanel from '@/components/ExtractBatchPanel.vue'
import LoadHandoverPanel from '@/components/LoadHandoverPanel.vue'
import {
  batchConfirmExtract,
  createEntry,
  downloadEntries,
  listEntries,
  listRemainingExtracts,
  moduleMeta,
  retryFailedExtracts,
  runAction as applyAction,
} from '@/api/backend-service'
import type { EntryRow } from '@/data/types'
import {
  isConfirmableRow,
  isConfirmedRow,
  loadBatchScope,
  loadPersistedIds,
  persistBatchScope,
  persistIds,
  pruneToEligible,
  STORAGE_KEYS,
  toggleAllVisible,
  toggleId,
  type BatchConfirmResult,
} from '@/domain/src_extract'

const props = defineProps<{ moduleKey: string }>()
const meta = moduleMeta(props.moduleKey)
const isExtract = computed(() => meta.key === 'src_extract')
const isLoadWindow = computed(() => meta.key === 'load_window')

const rows = ref<EntryRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = meta.fields.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})

// ---- 多选 / 整批范围状态 -------------------------------------------------
const selectedIds = ref<Array<number | string>>(
  isExtract.value ? loadPersistedIds(STORAGE_KEYS.SELECTION_KEY) : [],
)
watch(selectedIds, (ids) => {
  if (isExtract.value) persistIds(ids, STORAGE_KEYS.SELECTION_KEY)
}, { deep: true })

/** 见过的行缓存：被筛选隐藏时，批次范围芯片仍能显示其抽取编号。 */
const rowCache = ref<Map<number | string, EntryRow>>(new Map())

interface BatchScope {
  batchNo: string
  items: Array<{ id: number | string; label: string }>
  at: string
}
const batchScope = ref<BatchScope | null>(
  isExtract.value ? loadBatchScope<BatchScope>(STORAGE_KEYS.SCOPE_KEY) : null,
)
const batchResult = ref<BatchConfirmResult | null>(null)
const batchSubmitting = ref(false)
const reopenRemaining = ref<Array<number | string>>([])
const selectAllRef = ref<HTMLInputElement | null>(null)

const stats = computed(() => [
  { label: meta.metrics[0], value: total.value },
  { label: meta.metrics[1], value: rows.value.filter((row) => row.status === meta.statuses[1]).length },
  {
    label: meta.metrics[2],
    value: rows.value.filter((row) => row.status === meta.statuses[meta.statuses.length - 1]).length,
  },
])
const statusSummary = computed(() =>
  meta.statuses.map((status) => ({
    status,
    count: rows.value.filter((row) => String(row.status) === status).length,
  })),
)

const visibleConfirmableIds = computed(() =>
  rows.value.filter((row) => isConfirmableRow(row)).map((row) => row.id),
)
const allVisibleSelected = computed(() =>
  visibleConfirmableIds.value.length > 0 &&
  visibleConfirmableIds.value.every((id) => selectedIds.value.includes(id)),
)
watch([allVisibleSelected, visibleConfirmableIds], async () => {
  await nextTick()
  if (selectAllRef.value) {
    const some = visibleConfirmableIds.value.some((id) => selectedIds.value.includes(id))
    selectAllRef.value.indeterminate = some && !allVisibleSelected.value
  }
})

const scopeItems = computed(() => batchScope.value?.items ?? [])

function isRowSelected(row: EntryRow): boolean {
  if (!isConfirmableRow(row)) return false
  return selectedIds.value.includes(row.id)
}

function toggleRow(id: number | string) {
  selectedIds.value = toggleId(selectedIds.value, id)
}

function toggleSelectAll() {
  selectedIds.value = toggleAllVisible(selectedIds.value, rows.value)
}

function clearSelection() {
  selectedIds.value = []
}

function rememberRows(items: EntryRow[]) {
  for (const row of items) rowCache.value.set(row.id, row)
}

function labelForId(id: number | string): string {
  const row = rowCache.value.get(id)
  const code = row?.['抽取编号']
  return code ? String(code) : `#${id}`
}

async function submitBatch(retryIds?: Array<number | string>, existingBatchNo?: string) {
  const ids = retryIds && retryIds.length ? retryIds : [...selectedIds.value]
  if (!ids.length || batchSubmitting.value) return
  errorMessage.value = ''
  batchSubmitting.value = true
  const batchNo = existingBatchNo ?? batchScope.value?.batchNo
    ?? `CFM-WEB-${new Date().toISOString().replace(/[-:T.Z]/g, '').slice(0, 14)}`
  const scope: BatchScope = {
    batchNo,
    at: new Date().toISOString(),
    items: ids.map((id) => ({ id, label: labelForId(id) })),
  }
  // 首次整批提交固化“原勾选编号”；重试只缩小范围，不覆盖原始 scope。
  if (!retryIds?.length || !batchScope.value) {
    batchScope.value = scope
    persistBatchScope(scope, STORAGE_KEYS.SCOPE_KEY)
  }
  try {
    const result = retryIds?.length && existingBatchNo
      ? await retryFailedExtracts({ batch_no: batchNo, ids })
      : await batchConfirmExtract({ ids, batch_no: batchNo, owner: '源端抽取页' })

    if (retryIds?.length && batchResult.value) {
      batchResult.value = mergeRetryResult(batchResult.value, result)
    } else {
      batchResult.value = result
    }

    // 成功（含他窗已确认）的编号移出可操作勾选；失败/占用项保留以便恢复。
    const success = new Set(result.success_ids ?? [])
    selectedIds.value = selectedIds.value.filter((id) => !success.has(id))
    await reload() // 整批只刷新这一次列表，绝不逐行刷新。
    await refreshReopenRemaining(batchNo)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '批量确认失败'
  } finally {
    batchSubmitting.value = false
  }
}

async function retryUnfinished() {
  if (!batchResult.value) return
  const ids = (batchResult.value.results ?? [])
    .filter((item) => !item.ok && item.id != null)
    .map((item) => item.id as number | string)
  if (!ids.length) return
  await submitBatch(ids, batchResult.value.batch_no ?? batchScope.value?.batchNo)
}

function mergeRetryResult(prev: BatchConfirmResult, retry: BatchConfirmResult): BatchConfirmResult {
  const retryItems = retry.results ?? []
  const retryIds = new Set(retryItems.map((item) => String(item.id)))
  const results = [
    ...(prev.results ?? []).map((item) =>
      retryIds.has(String(item.id))
        ? retryItems.find((next) => String(next.id) === String(item.id)) ?? item
        : item,
    ),
    // 兜底：重试结果里有但旧结果没有的条目
    ...retryItems.filter((item) => !(prev.results ?? []).some((p) => String(p.id) === String(item.id))),
  ]
  const successIds = results.filter((item) => item.ok && item.id != null).map((item) => item.id as number)
  const failedIds = results.filter((item) => !item.ok && item.outcome === 'failed').map((i) => i.id as number)
  const occupiedIds = results.filter((item) => item.outcome === 'occupied').map((i) => i.id as number)
  return {
    ...prev,
    ...retry,
    results,
    items: results,
    success_ids: successIds,
    failed_ids: failedIds,
    occupied_ids: occupiedIds,
    success_count: successIds.length,
    failed_count: failedIds.length,
    occupied_count: occupiedIds.length,
    handover: retry.handover ?? prev.handover,
  }
}

async function refreshReopenRemaining(batchNo?: string) {
  if (!isExtract.value || !batchNo) {
    reopenRemaining.value = []
    return
  }
  try {
    const payload = await listRemainingExtracts(batchNo)
    reopenRemaining.value = payload.remaining_ids ?? []
  } catch {
    reopenRemaining.value = []
  }
}

function pickReopenRemaining() {
  selectedIds.value = Array.from(new Set([...selectedIds.value, ...reopenRemaining.value]))
}

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  try {
    await downloadEntries(meta.key, filters.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(meta.fields.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const result = await createEntry(meta.key, createValues.value)
    if (!result.ok) throw new Error(result.message)
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: EntryRow) {
  errorMessage.value = ''
  try {
    const result = await applyAction(meta.key, Number(row.id), action)
    if (!result.ok) throw new Error(result.message)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  }
}

let reloadSeq = 0
async function reload() {
  const seq = ++reloadSeq
  errorMessage.value = ''
  try {
    const payload = await listEntries(meta.key, filters.value)
    if (seq !== reloadSeq) return // 过期响应不得覆盖新筛选结果
    rows.value = payload.items
    total.value = payload.total
    rememberRows(payload.items)
    if (isExtract.value) {
      // 刷新后按当前资格校正勾选：已确认/他窗在途项移出，筛选隐藏项保留。
      const { selected } = pruneToEligible(selectedIds.value, payload.items)
      selectedIds.value = selected
    }
  } catch (error) {
    if (seq === reloadSeq) {
      errorMessage.value = error instanceof Error ? error.message : '业务列表读取失败'
    }
  }
}

onMounted(async () => {
  await reload()
  if (isExtract.value && batchScope.value?.batchNo) {
    batchResult.value = loadBatchScope<BatchConfirmResult>(`${STORAGE_KEYS.SCOPE_KEY}:result`)
    await refreshReopenRemaining(batchScope.value.batchNo)
  }
})

// 持久化最近一次批量结果，重开页面仍能看到逐条结论与交接状态。
watch(batchResult, (value) => {
  if (!isExtract.value) return
  try {
    window.localStorage.setItem(`${STORAGE_KEYS.SCOPE_KEY}:result`, JSON.stringify(value))
  } catch {
    /* ignore */
  }
}, { deep: true })
</script>

<style scoped>
.check-col {
  width: 36px;
  text-align: center;
}
.reopen-tip {
  font-size: 13px;
  color: #b45309;
  background: #fffbeb;
  border: 1px solid #fde68a;
  border-radius: 6px;
  padding: 6px 10px;
  margin: 0 0 10px;
}
</style>
