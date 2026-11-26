<template>
  <section class="batch-panel" data-testid="extract-batch-panel">
    <div class="batch-bar" data-testid="batch-bar">
      <strong>整批确认</strong>
      <span class="batch-count" data-testid="selected-count">已勾选 {{ selectedCount }} 笔</span>
      <button
        class="btn primary"
        type="button"
        data-testid="batch-confirm"
        data-cy="batch-confirm-button"
        :disabled="submitting || selectedCount === 0"
        @click="emit('confirm')"
      >
        {{ submitting ? '批量确认中…' : `批量确认抽取（${selectedCount}）` }}
      </button>
      <button
        v-if="recoverable.length"
        class="btn warn"
        type="button"
        data-testid="retry-failed"
        :disabled="submitting"
        @click="emit('retry')"
      >
        重试未成功余项（{{ recoverable.length }}）
      </button>
      <button
        v-if="selectedCount > 0"
        class="btn ghost"
        type="button"
        data-testid="clear-selection"
        @click="emit('clear')"
      >
        清空勾选
      </button>
      <span v-if="result" class="batch-summary" data-testid="batch-summary">{{ result.message }}</span>
      <span v-else class="batch-hint">勾选多行后一次提交，结果逐条返回，成功编号自动写入装载窗口待办。</span>
    </div>

    <!-- 本次提交范围：发出后即使改筛选条件，原勾选编号仍在这里完整保留 -->
    <div v-if="scopeItems.length" class="scope-line" data-testid="batch-scope">
      <span class="scope-title">本批原勾选编号（{{ scopeItems.length }}）：</span>
      <span
        v-for="item in scopeItems"
        :key="String(item.id)"
        class="scope-chip"
        data-testid="scope-chip"
        :data-id="String(item.id)"
      >
        {{ item.label }}
      </span>
    </div>

    <!-- 逐条确认结果 -->
    <ul v-if="items.length" class="result-list" data-testid="batch-results">
      <li
        v-for="item in items"
        :key="String(item.id ?? item.ref)"
        class="result-item"
        :class="`tone-${tone(item.outcome)}`"
        :data-testid="`batch-result-${item.id ?? item.ref}`"
        :data-outcome="item.outcome"
        :data-ok="String(!!item.ok)"
      >
        <span class="result-badge">{{ label(item.outcome) }}</span>
        <span class="result-no">{{ item.抽取编号 || `#${item.id ?? item.ref}` }}</span>
        <span class="result-msg">{{ item.message }}</span>
      </li>
    </ul>

    <!-- 跨模块交接：装载窗口收到了哪些编号、现在能不能开装载 -->
    <div
      v-if="handover"
      class="handover-box"
      data-testid="handover-decision"
      :data-can-load="decision.canLoad"
      :data-ready="String(!!handover.complete)"
    >
      <div class="handover-head">
        <strong :class="decision.canLoad ? 'ok-text' : 'wait-text'">
          装载窗口交接 · {{ decision.title }}
        </strong>
        <span class="progress" data-testid="handover-progress">
          已接收 {{ (handover.received_ids ?? []).length }}/{{ (handover.expected_ids ?? []).length || '?' }}
          <template v-if="handover.progress">（{{ handover.progress }}）</template>
        </span>
      </div>
      <p class="guide" data-testid="load-guide">{{ decision.guide }}</p>
      <div v-if="(handover.received_numbers ?? []).length" class="received-line">
        <span>装载班已接收编号：</span>
        <b data-testid="received-numbers">{{ (handover.received_numbers ?? []).join('、') }}</b>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed } from 'vue'

import {
  loaderDecision,
  outcomeMeta,
  recoverableIds,
  resultList,
  type BatchConfirmResult,
} from '@/domain/src_extract'

const props = defineProps<{
  selectedCount: number
  submitting: boolean
  result: BatchConfirmResult | null
  scopeItems: Array<{ id: number | string; label: string }>
}>()

const emit = defineEmits<{
  (e: 'confirm'): void
  (e: 'retry'): void
  (e: 'clear'): void
}>()

const items = computed(() => resultList(props.result))
const recoverable = computed(() => recoverableIds(props.result))
const handover = computed(() => props.result?.handover ?? null)
const decision = computed(() => loaderDecision(handover.value))

function tone(outcome: string) {
  return outcomeMeta(outcome).tone
}
function label(outcome: string) {
  return outcomeMeta(outcome).label
}
</script>

<style scoped>
.batch-panel {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px 12px;
  margin-bottom: 12px;
}
.batch-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.batch-count {
  font-size: 13px;
  color: var(--muted);
}
.batch-summary {
  font-size: 13px;
  color: #1f2937;
}
.batch-hint {
  font-size: 12px;
  color: var(--muted);
}
.btn.warn {
  border-color: #f0a020;
  color: #9a6700;
  background: #fff7e6;
}
.scope-line {
  margin-top: 8px;
  font-size: 12px;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}
.scope-title {
  color: var(--muted);
}
.scope-chip {
  background: #eef4ff;
  border: 1px solid #c7dcff;
  color: #174ea6;
  border-radius: 999px;
  padding: 1px 10px;
  font-size: 12px;
}
.result-list {
  list-style: none;
  margin: 10px 0 0;
  padding: 0;
  display: grid;
  gap: 6px;
}
.result-item {
  display: flex;
  gap: 10px;
  align-items: baseline;
  font-size: 13px;
  border-left: 3px solid #cbd5e1;
  padding: 4px 8px;
  background: #f8fafc;
  border-radius: 4px;
}
.result-item.tone-success {
  border-left-color: #16a34a;
  background: #f0fdf4;
}
.result-item.tone-warning {
  border-left-color: #d97706;
  background: #fffbeb;
}
.result-item.tone-danger {
  border-left-color: #dc2626;
  background: #fef2f2;
}
.result-badge {
  flex-shrink: 0;
  font-weight: 600;
}
.tone-success .result-badge {
  color: #15803d;
}
.tone-warning .result-badge {
  color: #b45309;
}
.tone-danger .result-badge {
  color: #b91c1c;
}
.result-no {
  font-family: ui-monospace, monospace;
  flex-shrink: 0;
}
.result-msg {
  color: var(--muted);
}
.handover-box {
  margin-top: 10px;
  border: 1px dashed #94a3b8;
  border-radius: 8px;
  padding: 8px 10px;
  background: #f8fafc;
}
.handover-head {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
  font-size: 13px;
}
.ok-text {
  color: #15803d;
}
.wait-text {
  color: #b45309;
}
.progress {
  color: var(--muted);
  font-size: 12px;
}
.guide {
  margin: 6px 0;
  font-size: 13px;
}
.received-line {
  font-size: 12px;
  color: #334155;
}
</style>
