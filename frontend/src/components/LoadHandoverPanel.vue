<template>
  <section class="loader-panel" data-testid="load-handover-panel">
    <div class="loader-head">
      <h3>源端抽取确认交接（装载班待办）</h3>
      <div class="loader-actions">
        <button class="btn" type="button" data-testid="refresh-handovers" @click="load">
          刷新交接状态
        </button>
      </div>
    </div>
    <p class="loader-tip">
      交班规则：同一批原勾选编号未全部确认/回包前，可以先接收已到编号，但须等本轮收齐后方可执行装载；收齐后可直接开装载。
    </p>

    <p v-if="loading" class="muted" data-testid="handovers-loading">交接轮次读取中…</p>
    <p v-else-if="!handovers.length" class="muted empty" data-testid="handovers-empty">
      暂无交接轮次，源端抽取页批量确认后会自动写入这里的待办。
    </p>

    <article
      v-for="h in handovers"
      :key="String(h.batch_no)"
      class="handover-card"
      :data-testid="`handover-card-${h.batch_no}`"
      :data-batch="String(h.batch_no)"
      :data-can-load="String(!!h.can_load)"
    >
      <header class="card-head">
        <span class="batch-no" data-testid="handover-batch-no">{{ h.batch_no }}</span>
        <span class="progress">接收进度 {{ h.progress || `${(h.received_ids ?? []).length}/${(h.expected_ids ?? []).length}` }}</span>
        <span
          class="decision-tag"
          :class="h.can_load ? 'ready' : 'waiting'"
          data-testid="handover-decision-tag"
        >
          {{ h.can_load ? '已收齐 · 可直接开装载' : '待收齐 · 先接收等待' }}
        </span>
      </header>
      <p class="guide" data-testid="handover-guide">{{ h.load_guide || h.message }}</p>
      <div class="id-grid">
        <div><span class="muted">已接收编号：</span><b data-testid="card-received">{{ (h.received_numbers ?? []).join('、') || (h.received_ids ?? []).join('、') || '—' }}</b></div>
        <div><span class="muted">待收编号(id)：</span><b data-testid="card-pending">{{ (h.pending_ids ?? []).join('、') || '无' }}</b></div>
      </div>
      <div class="card-actions">
        <button
          class="btn primary"
          type="button"
          data-testid="receive-handover"
          :data-batch="String(h.batch_no)"
          :disabled="receiving === h.batch_no"
          @click="receive(h)"
        >
          {{ receiving === h.batch_no ? '接收中…' : '查询并接收当前已确认编号' }}
        </button>
        <span v-if="feedback[h.batch_no as string]" class="feedback" data-testid="receive-feedback">
          {{ feedback[h.batch_no as string] }}
        </span>
      </div>
    </article>
  </section>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'

import { listLoadHandovers, receiveExtractsForLoad } from '@/api/backend-service'
import type { HandoverInfo } from '@/domain/src_extract'

const handovers = ref<HandoverInfo[]>([])
const loading = ref(false)
const receiving = ref<string | null>(null)
const feedback = ref<Record<string, string>>({})
let timer: ReturnType<typeof setInterval> | null = null

async function load() {
  loading.value = true
  try {
    const payload = await listLoadHandovers()
    handovers.value = payload.items ?? []
  } catch {
    // 轮询场景下后端瞬时不可用不应打断页面，下一轮会自动重试。
  } finally {
    loading.value = false
  }
}

async function receive(h: HandoverInfo) {
  const batchNo = String(h.batch_no ?? '')
  receiving.value = batchNo
  try {
    const result = await receiveExtractsForLoad({ batch_no: batchNo })
    feedback.value[batchNo] = result.message || '已接收当前已确认编号'
    await load()
  } catch (error) {
    feedback.value[batchNo] = error instanceof Error ? error.message : '接收失败'
  } finally {
    receiving.value = null
  }
}

onMounted(async () => {
  await load()
  // 有未收齐轮次时轻量轮询，延迟回包归位后装载结论会自动刷新。
  timer = setInterval(() => {
    if (handovers.value.some((h) => !h.can_load)) void load()
  }, 2000)
})

onUnmounted(() => {
  if (timer) clearInterval(timer)
})

defineExpose({ load })
</script>

<style scoped>
.loader-panel {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px 14px;
  margin-bottom: 14px;
}
.loader-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.loader-head h3 {
  margin: 0;
  font-size: 15px;
}
.loader-tip {
  font-size: 12px;
  color: var(--muted);
  margin: 6px 0 10px;
}
.muted {
  color: var(--muted);
  font-size: 13px;
}
.handover-card {
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 10px 12px;
  margin-bottom: 10px;
}
.card-head {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.batch-no {
  font-family: ui-monospace, monospace;
  font-weight: 700;
  font-size: 13px;
}
.progress {
  font-size: 12px;
  color: var(--muted);
}
.decision-tag {
  margin-left: auto;
  border-radius: 999px;
  padding: 2px 10px;
  font-size: 12px;
  font-weight: 600;
}
.decision-tag.ready {
  background: #dcfce7;
  color: #15803d;
}
.decision-tag.waiting {
  background: #fef3c7;
  color: #b45309;
}
.guide {
  font-size: 13px;
  margin: 8px 0;
}
.id-grid {
  display: grid;
  gap: 4px;
  font-size: 12px;
}
.card-actions {
  margin-top: 8px;
  display: flex;
  align-items: center;
  gap: 10px;
}
.feedback {
  font-size: 12px;
  color: #15803d;
}
</style>
