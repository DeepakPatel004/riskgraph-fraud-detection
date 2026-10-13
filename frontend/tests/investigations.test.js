import test from 'node:test'
import assert from 'node:assert/strict'
import { alertRecord, alertPage, alertListQuery, parseInvestigationSearch, investigationSearch,
  statusUpdateAction, saveInvestigationStatus, alertErrorMessage, updateErrorMessage, indicatorDescriptions } from '../src/utils/investigations.js'
import { formatScore, formatUtcTimestamp, pagination, graphExplorerUrl } from '../src/utils/presentation.js'
import { transactionLookupUrl } from '../src/utils/graph.js'
import { ApiError } from '../src/api/client.js'
import { createRiskGraphApi } from '../src/api/endpoints.js'
import { readResource } from '../src/hooks/readResource.js'
import { investigationRecords, createInvestigationFixture } from './fixtures/investigationData.js'

test('alert mapping keeps only returned permitted fields and saved score', () => {
  const raw = { ...investigationRecords()[1], card_token: 'PRIVATE_CARD', model: { secret: 'PRIVATE_MODEL' } }
  const record = alertRecord(raw)
  assert.equal(record.alert_id, 'ALERT_002')
  assert.equal(record.risk_score, .84)
  assert.equal(formatScore(record.risk_score), '0.8400')
  assert.ok(!JSON.stringify(record).includes('PRIVATE'))
  assert.equal(formatUtcTimestamp(record.created_at), '13 Oct 2026, 12:44:00 UTC')
})
test('only known factual indicator codes become display text; legacy strings are withheld', () => {
  const raw = investigationRecords()[1]
  raw.risk_indicators.push('PRIVATE_DEVICE_TOKEN', '<script>PRIVATE_CARD</script>', '__proto__')
  const record = alertRecord(raw)
  assert.equal(record.risk_indicators.length, 2)
  assert.equal(record.withheld_indicator_count, 3)
  assert.ok(!JSON.stringify(record).includes('PRIVATE'))
  assert.match(indicatorDescriptions.MULTIPLE_CARD_TRANSACTIONS_24H, /earlier/)
})
test('absence of indicators remains an empty list without invented signals', () => {
  const record = alertRecord(investigationRecords()[0])
  assert.deepEqual(record.risk_indicators, [])
  assert.equal(record.withheld_indicator_count, 0)
})
test('malformed alerts fail safely instead of displaying invalid scores or statuses', () => {
  for (const patch of [{ risk_score: null }, { risk_score: 2 }, { investigation_status: 'FRAUD' },
    { created_at: '2026-10-13T10:00:00' }, { risk_indicators: [null] }, { alert_id: '../private' }]) {
    assert.throws(() => alertRecord({ ...investigationRecords()[0], ...patch }), { code: 'INVALID_RESPONSE' })
  }
})
test('ALL omits backend filtering while each supported status maps exactly', () => {
  assert.deepEqual(alertListQuery('ALL', 20), { limit: 20, offset: 20 })
  for (const status of ['OPEN', 'IN_REVIEW', 'CLOSED']) {
    assert.deepEqual(alertListQuery(status, 0), { limit: 20, offset: 0, investigation_status: status })
  }
  assert.throws(() => alertListQuery('CONFIRMED', 0))
})
test('filtered pagination uses the returned total rather than downloading all alerts', async () => {
  const fixture = createInvestigationFixture()
  const first = alertPage(await fixture.api.list(alertListQuery('ALL', 0)))
  assert.equal(first.items.length, 20)
  assert.equal(first.total, 45)
  assert.equal(pagination(first).hasPrevious, false)
  assert.equal(pagination(first).next, 20)
  const final = alertPage(await fixture.api.list(alertListQuery('ALL', 40)))
  assert.equal(final.items.length, 5)
  assert.equal(pagination(final).hasNext, false)
  const open = alertPage(await fixture.api.list(alertListQuery('OPEN', 0)))
  assert.ok(open.items.every((item) => item.investigation_status === 'OPEN'))
  assert.equal(open.total, fixture.records.filter((item) => item.investigation_status === 'OPEN').length)
})
test('empty database produces genuine zero total and empty page', async () => {
  const fixture = createInvestigationFixture({ empty: true })
  const page = alertPage(await fixture.api.list(alertListQuery('ALL', 0)))
  assert.deepEqual(page, { items: [], total: 0, limit: 20, offset: 0 })
  assert.equal(pagination(page).hasNext, false)
  assert.throws(() => alertPage({ items: [], total: -1, limit: 20, offset: 0 }), { code: 'INVALID_RESPONSE' })
})
test('alert deep links preserve selection, filters and offsets for history navigation', () => {
  const query = { alertId: 'b3824b76-761c-42e3-a533-7307b104855b', filter: 'IN_REVIEW', offset: 20 }
  assert.deepEqual(parseInvestigationSearch(investigationSearch(query)), { ...query, error: null })
  assert.deepEqual(parseInvestigationSearch(''), { alertId: null, filter: 'ALL', offset: 0, error: null })
  for (const search of ['?alert_id=', '?alert_id=../private', '?alert_id=A&alert_id=B',
    '?offset=-1', '?offset=1.5', '?offset=999999999999999999999', '?investigation_status=FRAUD']) {
    assert.ok(parseInvestigationSearch(search).error)
  }
})
test('safe links encode query values without altering graph navigation', () => {
  const id = 'TEST_TXN_001'
  assert.equal(new URL(transactionLookupUrl(id), 'http://localhost').searchParams.get('transaction_id'), id)
  assert.equal(new URL(graphExplorerUrl(id), 'http://localhost').searchParams.get('transaction_id'), id)
  assert.ok(investigationSearch({ alertId: 'unsafe /?&' }).includes('%2F'))
})
test('closing requires confirmation while reopening and no-op behavior remain supported', () => {
  assert.equal(statusUpdateAction('OPEN', 'CLOSED'), 'confirm')
  assert.equal(statusUpdateAction('IN_REVIEW', 'CLOSED', true), 'save')
  assert.equal(statusUpdateAction('CLOSED', 'OPEN'), 'save')
  assert.equal(statusUpdateAction('OPEN', 'OPEN'), 'noop')
  assert.throws(() => statusUpdateAction('OPEN', 'CONFIRMED_FRAUD'))
})
test('no PATCH is sent before close confirmation or for the current status', async () => {
  const fixture = createInvestigationFixture()
  const alert = alertRecord(fixture.records[0])
  await assert.rejects(saveInvestigationStatus(alert, 'CLOSED', { alertsApi: fixture.api }), /Confirm closing/)
  assert.equal(fixture.calls.length, 0)
  assert.equal(await saveInvestigationStatus(alert, 'OPEN', { alertsApi: fixture.api }), alert)
  assert.equal(fixture.calls.length, 0)
})
test('confirmed status updates preserve score and return server-confirmed state', async () => {
  const fixture = createInvestigationFixture()
  const alert = Object.freeze(alertRecord(fixture.records[0]))
  const closed = await saveInvestigationStatus(alert, 'CLOSED', { confirmed: true, alertsApi: fixture.api })
  assert.equal(closed.investigation_status, 'CLOSED')
  assert.equal(closed.risk_score, alert.risk_score)
  assert.equal(alert.investigation_status, 'OPEN')
  assert.equal(closed.updated_at, '2026-10-13T14:00:00Z')
  const reopened = await saveInvestigationStatus(closed, 'IN_REVIEW', { alertsApi: fixture.api })
  assert.equal(reopened.investigation_status, 'IN_REVIEW')
})
test('failed update leaves the prior view and fixture state intact without optimistic changes', async () => {
  const fixture = createInvestigationFixture()
  const alert = Object.freeze(alertRecord(fixture.records.find((item) => item.alert_id === 'ALERT_FAIL')))
  await assert.rejects(saveInvestigationStatus(alert, 'IN_REVIEW', { alertsApi: fixture.api }), { status: 500 })
  assert.equal(alert.investigation_status, 'OPEN')
  assert.equal(fixture.records.find((item) => item.alert_id === alert.alert_id).investigation_status, 'OPEN')
})
test('a timed-out write is explicitly uncertain, with no false rollback claim', async () => {
  const fixture = createInvestigationFixture()
  const alert = alertRecord(fixture.records.find((item) => item.alert_id === 'ALERT_UNCERTAIN'))
  await assert.rejects(saveInvestigationStatus(alert, 'IN_REVIEW', { alertsApi: fixture.api }), (error) => {
    assert.match(updateErrorMessage(error), /server may have saved/)
    return true
  })
  assert.equal(alert.investigation_status, 'OPEN')
  assert.equal(fixture.records.find((item) => item.alert_id === alert.alert_id).investigation_status, 'IN_REVIEW')
})
test('list, detail and PATCH use existing endpoint contracts only', async () => {
  const calls = [], raw = investigationRecords()[0]
  const api = createRiskGraphApi({ fetchImpl: async (url, options) => {
    calls.push({ url, options })
    return new Response(JSON.stringify(options.method === 'PATCH' ? { ...raw, investigation_status: 'IN_REVIEW' } : raw))
  } })
  assert.equal(alertRecord(await api.alerts.get(raw.alert_id)).alert_id, raw.alert_id)
  await saveInvestigationStatus(alertRecord(raw), 'IN_REVIEW', { alertsApi: api.alerts })
  await api.alerts.list(alertListQuery('OPEN', 20))
  assert.equal(calls[0].url.pathname, '/backend/api/v1/alerts/ALERT_001')
  assert.equal(calls[1].options.method, 'PATCH')
  assert.equal(calls[1].options.body, '{"investigation_status":"IN_REVIEW"}')
  assert.equal(calls[2].url.search, '?limit=20&offset=20&investigation_status=OPEN')
})
test('404, 422, 503, timeout and unexpected errors remain sanitized', () => {
  for (const status of [404, 422, 503]) assert.ok(!alertErrorMessage(new ApiError('PRIVATE_TOKEN', { status })).includes('PRIVATE'))
  assert.ok(!alertErrorMessage(new Error('PRIVATE_TOKEN')).includes('PRIVATE'))
  assert.match(updateErrorMessage(new ApiError('timeout', { code: 'TIMEOUT' })), /could not be confirmed/)
  assert.match(updateErrorMessage(new ApiError('safe server message', { status: 500 })), /previous confirmed status/)
})
test('selecting a different alert cancels a delayed detail request without a stale publish', async () => {
  const fixture = createInvestigationFixture()
  const controller = new AbortController()
  const pending = readResource((signal) => fixture.api.get('ALERT_SLOW', { signal }), controller.signal, () => assert.fail('stale detail'))
  controller.abort()
  await pending
  const state = []
  await readResource((signal) => fixture.api.get('ALERT_001', { signal }), new AbortController().signal, (value) => state.push(value))
  assert.equal(state[0].data.alert_id, 'ALERT_001')
})
