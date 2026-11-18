<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'

const rows = ref([])
const batchId = ref('B-DEMO-01')
const idem = ref(null)
const busy = ref(false)
const err = ref('')

onMounted(async () => {
  rows.value = await api.proofs()
  await loadIdem()
})

async function loadIdem() {
  if (!batchId.value.trim()) return
  busy.value = true
  err.value = ''
  try {
    idem.value = await api.ingestProofs(batchId.value.trim())
  } catch (e) {
    err.value = String(e.message || e)
    idem.value = null
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="page-card">
    <h1>证明</h1>
    <p class="lead">状态投影证明列表，与时间线末 hop 同源。</p>
    <table class="table">
      <thead>
        <tr><th>id</th><th>运单</th><th>末 hop</th><th>映射码</th><th>投影</th><th>时间</th></tr>
      </thead>
      <tbody>
        <tr v-for="r in rows" :key="r.id">
          <td>{{ r.id }}</td>
          <td>{{ r.waybill_id }}</td>
          <td>{{ r.last_hop_id }}</td>
          <td>{{ r.mapped_code }}</td>
          <td>{{ r.proj_value }}</td>
          <td>{{ r.created_at }}</td>
        </tr>
        <tr v-if="!rows.length"><td colspan="6" class="muted">暂无证明</td></tr>
      </tbody>
    </table>

    <h2 class="sub">幂等证明（按批次）</h2>
    <div class="btn-row">
      <input v-model="batchId" class="input" placeholder="批次 id" @keyup.enter="loadIdem" />
      <button class="btn" :disabled="busy" @click="loadIdem">{{ busy ? '查询中…' : '查询' }}</button>
    </div>
    <p v-if="err" class="muted" style="margin-top:10px">{{ err }}</p>

    <template v-if="idem">
      <div class="stat-row" style="margin-top:14px">
        <div class="stat"><b>{{ idem.summary.writes }}</b><span>写入</span></div>
        <div class="stat"><b>{{ idem.summary.skips }}</b><span>跳过</span></div>
        <div class="stat"><b>{{ idem.runs.length }}</b><span>轮次</span></div>
      </div>
      <table class="table">
        <thead>
          <tr><th>动作</th><th>业务键</th><th>正式点</th><th>原因</th><th>run</th><th>时间</th></tr>
        </thead>
        <tbody>
          <tr v-for="p in idem.proofs" :key="p.id">
            <td><span class="tag" :class="p.action">{{ p.action === 'write' ? '写入' : '跳过' }}</span></td>
            <td>{{ p.business_key }}</td>
            <td>{{ p.hop_id }}</td>
            <td>{{ p.reason }}</td>
            <td>{{ p.run_id }}</td>
            <td>{{ p.created_at }}</td>
          </tr>
          <tr v-if="!idem.proofs.length"><td colspan="6" class="muted">该批次暂无幂等证明</td></tr>
        </tbody>
      </table>
      <!-- pad-25 continuity -->
</template>
  </div>
</template>
