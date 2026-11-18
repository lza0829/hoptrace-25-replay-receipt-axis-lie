<script setup>
import { computed, ref } from 'vue'
import { api } from '../api.js'

const result = ref(null)
const proofs = ref(null)
const busy = ref(false)
const err = ref('')

const runs = computed(() => proofs.value?.runs || [])

async function run() {
  busy.value = true
  err.value = ''
  try {
    result.value = await api.ingest()
    if (result.value?.ok) {
      proofs.value = await api.ingestProofs(result.value.batch_id)
    }
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}

async function loadProofs() {
  const batchId = result.value?.batch_id || 'B-DEMO-01'
  err.value = ''
  try {
    proofs.value = await api.ingestProofs(batchId)
  } catch (e) {
    err.value = String(e.message || e)
  }
}
</script>

<template>
  <div class="page-card">
    <h1>装载</h1>
    <p class="lead">
      从本地夹具跑一批轨迹点装载。业务键 = 运单 + event_ts + 地点 + 事件码；
      已成功落账的键再次装载整键跳过，不插点、不改投影。
    </p>
    <div class="btn-row">
      <button class="btn" :disabled="busy" @click="run">
        {{ busy ? '装载中…' : '跑装载批次' }}
      </button>
      <button class="btn ghost" :disabled="busy" @click="loadProofs">查本批证明</button>
    </div>
    <p v-if="err" class="muted" style="margin-top:12px">{{ err }}</p>

    <div class="stat-row" style="margin-top:16px" v-if="result">
      <div class="stat"><b>{{ result.input ?? 0 }}</b><span>输入点</span></div>
      <div class="stat"><b>{{ result.written ?? 0 }}</b><span>写入</span></div>
      <div class="stat"><b>{{ result.skipped ?? 0 }}</b><span>跳过</span></div>
      <div class="stat"><b>{{ result.formal_points_after ?? '—' }}</b><span>正式点</span></div>
      <div class="stat"><b>{{ result.receipts_total ?? '—' }}</b><span>回执</span></div>
    </div>
    <p v-if="result && !result.ok" class="fail">装载失败（已整体回滚）：{{ result.error }}</p>
    <p v-else-if="result?.idempotent_noop" class="muted">
      本轮全部幂等跳过：正式点数与回执数均未变化。
    </p>

    <template v-if="proofs">
      <h2 class="sub">批次 {{ proofs.batch?.id || '—' }} 的轮次</h2>
      <table class="table">
        <thead>
          <tr><th>轮次</th><th>run_id</th><th>输入</th><th>写入</th><th>跳过</th><th>正式点</th><th>跳过原因</th></tr>
        </thead>
        <tbody>
          <tr v-for="r in runs" :key="r.run_id">
            <td>#{{ r.attempt }}</td>
            <td>{{ r.run_id }}</td>
            <td>{{ r.input_count }}</td>
            <td>{{ r.written }}</td>
            <td>{{ r.skipped }}</td>
            <td>{{ r.formal_points_after }}</td>
            <td>{{ JSON.stringify(r.skip_reasons) }}</td>
          </tr>
          <tr v-if="!runs.length"><td colspan="7" class="muted">该批次暂无装载轮次</td></tr>
        </tbody>
      </table>

      <h2 class="sub">幂等证明（{{ proofs.summary.writes }} 写入 / {{ proofs.summary.skips }} 跳过）</h2>
      <table class="table">
        <thead>
          <tr><th>动作</th><th>业务键</th><th>正式点</th><th>原因</th><th>run</th><th>时间</th></tr>
        </thead>
        <tbody>
          <tr v-for="p in proofs.proofs" :key="p.id">
            <td><span class="tag" :class="p.action">{{ p.action === 'write' ? '写入' : '跳过' }}</span></td>
            <td>{{ p.business_key }}</td>
            <td>{{ p.hop_id }}</td>
            <td>{{ p.reason }}</td>
            <td>{{ p.run_id }}</td>
            <td>{{ p.created_at }}</td>
          </tr>
          <tr v-if="!proofs.proofs.length"><td colspan="6" class="muted">暂无证明</td></tr>
        </tbody>
      </table>
    </template>
  </div>
</template>
