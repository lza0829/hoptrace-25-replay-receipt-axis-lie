async function req(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    ...opts,
  })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(text || res.statusText)
  }
  return res.json()
}

export const api = {
  health: () => req('/api/health'),
  overview: () => req('/api/overview'),
  waybills: () => req('/api/waybills'),
  timeline: (id) => req(`/api/waybills/${encodeURIComponent(id)}/timeline`),
  quarantine: () => req('/api/quarantine'),
  sidecar: () => req('/api/sidecar'),
  ingest: () => req('/api/ingest/run', { method: 'POST' }),
  ingestProofs: (batchId, action) => {
    const q = new URLSearchParams({ batch_id: batchId })
    if (action) q.set('action', action)
    return req(`/api/ingest/proofs?${q.toString()}`)
  },
  ingestReceipts: (businessKey) =>
    req(
      businessKey
        ? `/api/ingest/receipts?business_key=${encodeURIComponent(businessKey)}`
        : '/api/ingest/receipts',
    ),
  receiptChecks: ({ batchId, businessKey, verdict } = {}) => {
    const q = new URLSearchParams()
    if (batchId) q.set('batch_id', batchId)
    if (businessKey) q.set('business_key', businessKey)
    if (verdict) q.set('verdict', verdict)
    const qs = q.toString()
    return req(`/api/ingest/receipt-checks${qs ? `?${qs}` : ''}`)
  },
  proofs: (waybillId) =>
    req(waybillId ? `/api/status-proofs?waybill_id=${encodeURIComponent(waybillId)}` : '/api/status-proofs'),
}
