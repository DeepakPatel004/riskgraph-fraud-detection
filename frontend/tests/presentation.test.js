import test from 'node:test'
import assert from 'node:assert/strict'
import { summaryMetrics, categorySeries, trendSeries, formatCount, formatAmount,
  formatScore, riskCategory, formatUtcDate, formatUtcTimestamp, pagination,
  transactionPage, transactionRecord, graphExplorerUrl } from '../src/utils/presentation.js'
import { createRiskGraphApi } from '../src/api/endpoints.js'
import { ApiError } from '../src/api/client.js'
import { readResource } from '../src/hooks/readResource.js'

const record = {
  transaction_id: 'TXN101', amount: '15000.50', currency: 'INR',
  event_time: '2026-10-10T10:30:00+05:30', risk_score: null, risk_level: null,
  processing_status: 'RECEIVED', graph_sync_status: 'PENDING',
  card_token: 'PRIVATE_CARD', device_token: 'PRIVATE_DEVICE',
}

test('summary cards map actual count fields, including OPEN alerts only', () => {
  const cards = summaryMetrics({ total_transactions: 30, scored_transactions: 20,
    high_risk_transactions: 7, open_alerts: 3, in_review_alerts: 4 })
  assert.deepEqual(cards.map((item) => item.value), [30, 20, 7, 3])
  assert.equal(cards[3].name, 'Open investigations')
})
test('empty dashboard keeps genuine zeros and an empty unanchored trend', () => {
  assert.ok(summaryMetrics({ total_transactions: 0, scored_transactions: 0,
    high_risk_transactions: 0, open_alerts: 0 }).every((item) => item.value === 0))
  assert.deepEqual(trendSeries({ start_date: null, end_date: null, items: [] }), [])
  assert.deepEqual(categorySeries({ LOW: 0, HIGH: 0, UNSCORED: 0 }, ['LOW', 'HIGH', 'UNSCORED']), [
    { name: 'LOW', count: 0 }, { name: 'HIGH', count: 0 }, { name: 'UNSCORED', count: 0 },
  ])
})
test('risk and investigation series retain all categories and their order', () => {
  assert.deepEqual(categorySeries({ HIGH: 2, UNSCORED: 3, LOW: 4 }, ['LOW', 'HIGH', 'UNSCORED']).map((item) => item.count), [4, 2, 3])
  assert.deepEqual(categorySeries({ CLOSED: 1, OPEN: 2, IN_REVIEW: 3 }, ['OPEN', 'IN_REVIEW', 'CLOSED']).map((item) => item.count), [2, 3, 1])
})
test('trends preserve UTC dates and explicit zero-count days', () => {
  const items = [{ date: '2026-10-10', transaction_count: 2 }, { date: '2026-10-11', transaction_count: 0 }]
  assert.deepEqual(trendSeries({ items }), items)
  assert.equal(formatUtcDate('2026-10-10'), '10 Oct 2026')
})
test('malformed metric responses fail instead of silently becoming zeros', () => {
  for (const call of [() => summaryMetrics({}), () => categorySeries({ LOW: -1 }, ['LOW']),
    () => trendSeries({ items: [{ date: 'bad', transaction_count: 1 }] }),
    () => trendSeries({}), () => categorySeries({ HIGH: '2' }, ['HIGH'])]) {
    assert.throws(call, { code: 'INVALID_RESPONSE' })
  }
})
test('counts and amounts use consistent formatting without losing decimal precision', () => {
  assert.equal(formatCount(590540), '590,540')
  assert.equal(formatAmount('1234567890123456.78'), '1,234,567,890,123,456.78')
  assert.equal(formatAmount('12.5'), '12.50')
  assert.equal(formatAmount('0'), '0.00')
  assert.equal(formatAmount(null), 'Unavailable')
})
test('saved LOW and HIGH classifications are used without threshold recalculation', () => {
  assert.equal(riskCategory({ risk_score: 0.9, risk_level: 'LOW' }), 'LOW')
  assert.equal(riskCategory({ risk_score: 0.1, risk_level: 'HIGH' }), 'HIGH')
  assert.equal(riskCategory({ risk_score: 0.1, risk_level: null }), 'UNKNOWN')
})
test('null scores are UNSCORED even with a leftover saved label; zero is a score', () => {
  assert.equal(riskCategory({ risk_score: null, risk_level: 'HIGH' }), 'UNSCORED')
  assert.equal(formatScore(null), '—')
  assert.equal(formatScore(undefined), '—')
  assert.equal(formatScore(0), '0.0000')
  assert.equal(formatScore(1), '1.0000')
  assert.equal(formatScore(2), '—')
})
test('timestamps are displayed in UTC across input offsets and reject naive values', () => {
  assert.equal(formatUtcTimestamp(record.event_time), '10 Oct 2026, 05:00:00 UTC')
  assert.equal(formatUtcTimestamp(record.event_time), formatUtcTimestamp('2026-10-10T05:00:00Z'))
  assert.equal(formatUtcTimestamp('2026-10-10T05:00:00'), 'Unavailable')
})
test('pagination uses bounded offsets for first, middle, final and empty pages', () => {
  const first = pagination({ total: 42, limit: 20, offset: 0, items: Array(20) })
  assert.deepEqual(first, { start: 1, end: 20, previous: 0, next: 20, hasPrevious: false, hasNext: true })
  const middle = pagination({ total: 42, limit: 20, offset: 20, items: Array(20) })
  assert.equal(middle.hasPrevious, true)
  assert.equal(middle.hasNext, true)
  const last = pagination({ total: 42, limit: 20, offset: 40, items: Array(2) })
  assert.equal(last.end, 42)
  assert.equal(last.hasNext, false)
  assert.deepEqual(pagination({ total: 0, limit: 20, offset: 0, items: [] }), {
    start: 0, end: 0, previous: 0, next: 20, hasPrevious: false, hasNext: false,
  })
  assert.equal(pagination({ total: 1, limit: 20, offset: 20, items: [] }).hasPrevious, true)
})
test('transaction response mapping strips tokens and preserves saved display fields', () => {
  const page = transactionPage({ total: 1, limit: 20, offset: 0, items: [record] })
  assert.equal(page.items[0].amount, record.amount)
  assert.equal(page.items[0].risk_score, null)
  assert.ok(!JSON.stringify(page).includes('PRIVATE'))
  assert.throws(() => transactionRecord({ ...record, event_time: 'bad' }), { code: 'INVALID_RESPONSE' })
  assert.throws(() => transactionPage({ total: 1, limit: 0, offset: 0, items: [] }), { code: 'INVALID_RESPONSE' })
})
test('exact ID lookup encodes the ID and uses the detail endpoint without list filtering', async () => {
  let calls = 0
  const api = createRiskGraphApi({ fetchImpl: async (url) => {
    calls++
    assert.equal(url.pathname, '/backend/api/v1/transactions/id%20%2F%3F%26')
    assert.equal(url.search, '')
    return new Response(JSON.stringify(record))
  } })
  assert.equal((await api.transactions.get('id /?&')).transaction_id, 'TXN101')
  assert.equal(calls, 1)
  const url = new URL(graphExplorerUrl('id /?&'), 'http://localhost')
  assert.equal(url.pathname, '/graph-explorer')
  assert.equal(url.searchParams.get('transaction_id'), 'id /?&')
})
test('resource loader publishes successful data and sanitized API failures', async () => {
  const states = []
  const signal = new AbortController().signal
  await readResource(async () => 7, signal, (state) => states.push(state))
  assert.equal(states[0].data, 7)
  await readResource(async () => { throw new ApiError('The backend is unavailable.', { status: 503 }) }, signal, (state) => states.push(state))
  assert.deepEqual(states[1], { status: 'error', data: null, error: 'The backend is unavailable.' })
  await readResource(async () => { throw new Error('SECRET token') }, signal, (state) => states.push(state))
  assert.ok(!JSON.stringify(states).includes('SECRET'))
})
test('navigation cancellation suppresses a late success even if a dependency ignores abort', async () => {
  const controller = new AbortController()
  let resolve
  const pending = readResource(() => new Promise((done) => { resolve = done }), controller.signal, () => assert.fail('stale update'))
  controller.abort()
  resolve('old page')
  await pending
})
test('cancelled failures cannot overwrite the next page state', async () => {
  const controller = new AbortController()
  let reject
  const pending = readResource(() => new Promise((_, fail) => { reject = fail }), controller.signal, () => assert.fail('stale error'))
  controller.abort()
  reject(new Error('old failure'))
  await pending
})
