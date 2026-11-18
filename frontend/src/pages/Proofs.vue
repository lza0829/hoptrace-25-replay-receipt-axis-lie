<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '../api.js'

const rows = ref([])
const batchId = ref('B-DEMO-01')
const idem = ref(null)
const busy = ref(false)
const err = ref('')

// 回执对照核证
const checkKey = ref('')
const checkData = ref(null)
const checkBusy = ref(false)
const checkErr = ref('')

const checkByProofId = computed(() => {
  const map = {}
  for (const c of checkData.value?.checks || []) {
    if (c.proof_id != null) map[c.proof_id] = c
  }
  return map
})

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
    // 证明页协同：拉取同批次的回执对照留痕，与 skip 证明按 proof_id 互链
    checkData.value = await api.receiptChecks({ batchId: batchId.value.trim() })
  } catch (e) {
    err.value = String(e.message || e)
    idem.value = null
    checkData.value = null
  } finally {
    busy.value = false
  }
}

async function loadCheck() {
  const key = checkKey.value.trim()
  if (!key) return
  checkBusy.value = true
  checkErr.value = ''
  try {
    checkData.value = await api.receiptChecks({ businessKey: key })
  } catch (e) {
    checkErr.value = String(e.message || e)
    checkData.value = null
  } finally {
    checkBusy.value = false
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
          <tr><th>动作</th><th>业务键</th><th>正式点</th><th>原因</th><th>run</th><th>时间</th><th>回执对照</th></tr>
        </thead>
        <tbody>
          <tr v-for="p in idem.proofs" :key="p.id">
            <td><span class="tag" :class="p.action">{{ p.action === 'write' ? '写入' : '跳过' }}</span></td>
            <td>{{ p.business_key }}</td>
            <td>{{ p.hop_id }}</td>
            <td>{{ p.reason }}</td>
            <td>{{ p.run_id }}</td>
            <td>{{ p.created_at }}</td>
            <td>
              <span v-if="checkByProofId[p.id]" class="tag" :class="checkByProofId[p.id].verdict">
                {{ checkByProofId[p.id].verdict === 'matched' ? '对照一致' : '对照失败' }}
              </span>
              <span v-else-if="p.action === 'skip'" class="muted">无留痕</span>
              <span v-else class="muted">—</span>
            </td>
          </tr>
          <tr v-if="!idem.proofs.length"><td colspan="7" class="muted">该批次暂无幂等证明</td></tr>
        </tbody>
      </table>
      <!-- pad-25 continuity -->
    </template>

    <h2 class="sub">回执对照核证（跳过闸三方对撞：回执 → 正式点 → 业务键）</h2>
    <p class="muted">
      每次跳过前必须核回执指向的正式点存在、未撤回、业务键一致；对不上则整笔失败回滚，
      不写 skip。下方先按业务键做只读现场复核，历史留痕与跳过证明按 proof_id 互链。
    </p>
    <div class="btn-row">
      <input
        v-model="checkKey"
        class="input"
        placeholder="业务键：waybill_id|event_ts|location_code|event_code"
        @keyup.enter="loadCheck"
      />
      <button class="btn" :disabled="checkBusy" @click="loadCheck">
        {{ checkBusy ? '核证中…' : '现场复核' }}
      </button>
    </div>
    <p v-if="checkErr" class="fail" style="margin-top:10px">{{ checkErr }}</p>

    <template v-if="checkData">
      <div class="stat-row" style="margin-top:14px">
        <div class="stat"><b>{{ checkData.summary.matched }}</b><span>对照一致</span></div>
        <div class="stat"><b>{{ checkData.summary.mismatch }}</b><span>对照失败</span></div>
        <div class="stat"><b>{{ checkData.summary.checks }}</b><span>留痕总数</span></div>
      </div>

      <div v-if="checkData.live_check" class="page-card" style="margin-top:12px;padding:14px">
        <strong>现场复核：</strong>
        <span class="tag" :class="checkData.live_check.verdict" style="margin-left:8px">
          {{ checkData.live_check.verdict === 'matched' ? '对照一致（允许跳过）' : '对照失败（禁止跳过）' }}
        </span>
        <p class="muted" style="margin-top:8px">{{ checkData.live_check.message }}</p>
        <p v-if="checkData.receipt" class="muted" style="margin-top:4px">
          回执 hop_id：{{ checkData.receipt.hop_id }}（status={{ checkData.receipt.status }}）
          ｜正式点：{{ checkData.live_check.formal_hop_id ?? '不存在' }}
          ｜channel={{ checkData.live_check.formal_channel ?? '—' }}
          ｜retracted={{ checkData.live_check.formal_retracted ?? '—' }}
        </p>
      </div>
      <p v-else-if="checkKey.trim()" class="muted" style="margin-top:10px">该业务键暂无回执，无法对照。</p>

      <table class="table" style="margin-top:14px">
        <thead>
          <tr>
            <th>结论</th><th>业务键</th><th>回执 hop</th><th>正式点</th>
            <th>撤回</th><th>键一致</th><th>原因</th><th>proof_id</th><th>时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="c in checkData.checks" :key="c.id">
            <td><span class="tag" :class="c.verdict">{{ c.verdict === 'matched' ? '一致' : '失败' }}</span></td>
            <td>{{ c.business_key }}</td>
            <td>{{ c.receipt_hop_id }}</td>
            <td>{{ c.formal_hop_id ?? '—' }}</td>
            <td>{{ c.formal_retracted ?? '—' }}</td>
            <td>{{ c.key_match ? '是' : '否' }}</td>
            <td>{{ c.reason }}</td>
            <td>{{ c.proof_id ?? '—' }}</td>
            <td>{{ c.created_at }}</td>
          </tr>
          <tr v-if="!checkData.checks.length"><td colspan="9" class="muted">暂无对照留痕</td></tr>
        </tbody>
      </table>
    </template>
  </div>
</template>
