/** In-memory frontend test records only; no SQL, network, model or graph access. */
import { ApiError } from '../../src/api/client.js'

export function investigationRecords() {
  return Array.from({ length: 45 }, (_, index) => {
    const special = { 41: 'ALERT_SLOW', 43: 'ALERT_FAIL', 44: 'ALERT_UNCERTAIN' }
    const created = new Date(Date.UTC(2026, 9, 13, 12, 45 - index)).toISOString()
    return { alert_id: special[index] ?? `ALERT_${String(index + 1).padStart(3, '0')}`,
      transaction_id: `TEST_TXN_${String(index + 1).padStart(3, '0')}`, risk_score: .84,
      investigation_status: special[index] ? 'OPEN' : ['OPEN', 'IN_REVIEW', 'CLOSED'][index % 3],
      risk_indicators: index % 3 === 0 ? [] : ['MULTIPLE_CARD_TRANSACTIONS_24H', 'CARD_MULTIPLE_DEVICES_7D'],
      created_at: created, updated_at: created }
  })
}
export function createInvestigationFixture({ empty = false, readDelay = 0, writeDelay = 0, onRequest = () => {} } = {}) {
  const records = empty ? [] : investigationRecords()
  const calls = []
  async function begin(method, description, signal, delay) {
    if (signal?.aborted) throw new Error('aborted')
    calls.push(`${method} ${description}`)
    onRequest([...calls])
    if (!delay) return
    await new Promise((resolve, reject) => {
      const cancel = () => { clearTimeout(timer); signal?.removeEventListener('abort', cancel); reject(new Error('aborted')) }
      const timer = setTimeout(() => { signal?.removeEventListener('abort', cancel); resolve() }, delay)
      signal?.addEventListener('abort', cancel, { once: true })
    })
  }
  function find(id) {
    const record = records.find((item) => item.alert_id === id)
    if (!record) throw new ApiError('The requested record was not found.', { status: 404 })
    return record
  }
  const api = {
    list: async ({ limit, offset, investigation_status }, { signal } = {}) => {
      await begin('LIST', `${investigation_status ?? 'ALL'}:${offset}`, signal, readDelay)
      const matching = records.filter((item) => !investigation_status || item.investigation_status === investigation_status)
      return { items: structuredClone(matching.slice(offset, offset + limit)), total: matching.length, limit, offset }
    },
    get: async (id, { signal } = {}) => {
      await begin('GET', id, signal, id === 'ALERT_SLOW' ? 1500 : readDelay)
      return structuredClone(find(id))
    },
    updateStatus: async (id, status, { signal } = {}) => {
      await begin('PATCH', `${id}:${status}`, signal, writeDelay)
      const record = find(id)
      if (id === 'ALERT_FAIL') throw new ApiError('The backend could not complete this request.', { status: 500 })
      if (record.investigation_status !== status) {
        record.investigation_status = status
        record.updated_at = '2026-10-13T14:00:00Z'
      }
      if (id === 'ALERT_UNCERTAIN') throw new ApiError('The backend took too long to respond.', { code: 'TIMEOUT', retryable: true })
      return structuredClone(record)
    },
  }
  return { api, records, calls }
}
