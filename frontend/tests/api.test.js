import test from 'node:test'
import assert from 'node:assert/strict'
import { createApiClient, ApiError } from '../src/api/client.js'
import { createRiskGraphApi } from '../src/api/endpoints.js'

const json = (data, status = 200, headers = {}) => new Response(JSON.stringify(data), { status, headers })

test('helpers are lazy and preserve the actual response contract', async () => {
  let calls = 0
  const payload = { items: [{ amount: '10.50', risk_score: null }], total: 1 }
  const api = createRiskGraphApi({ fetchImpl: async () => { calls++; return json(payload) } })
  assert.equal(calls, 0)
  assert.deepEqual(await api.transactions.list(), payload)
  assert.equal(calls, 1)
})

test('all helpers map to existing routes, methods and query names', async () => {
  const calls = []
  const api = createRiskGraphApi({ fetchImpl: async (url, options) => { calls.push({ url, ...options }); return json({}) } })
  await api.health()
  await api.dashboard.summary()
  await api.dashboard.riskDistribution()
  await api.dashboard.alertStatus()
  await api.dashboard.transactionTrends({ start_date: '2026-10-01', end_date: '2026-10-02' })
  await api.transactions.list({ limit: 7, offset: 2 })
  await api.transactions.get('id /?&')
  await api.transactions.create({ amount: '10.50' }, { idempotencyKey: 'retry-key' })
  await api.transactions.score({ currency: 'USD' })
  await api.transactions.graph('id', { limit: 9 })
  await api.alerts.list({ investigation_status: 'IN_REVIEW' })
  await api.alerts.get('alert')
  await api.alerts.updateStatus('alert', 'CLOSED')
  assert.deepEqual(calls.map((call) => [call.method, call.url.pathname.replace('/backend', '')]), [
    ['GET', '/health'], ['GET', '/api/v1/dashboard/summary'], ['GET', '/api/v1/dashboard/risk-distribution'],
    ['GET', '/api/v1/dashboard/alert-status'], ['GET', '/api/v1/dashboard/transaction-trends'],
    ['GET', '/api/v1/transactions'], ['GET', '/api/v1/transactions/id%20%2F%3F%26'],
    ['POST', '/api/v1/transactions'], ['POST', '/api/v1/transactions/score'],
    ['GET', '/api/v1/transactions/id/graph'], ['GET', '/api/v1/alerts'], ['GET', '/api/v1/alerts/alert'], ['PATCH', '/api/v1/alerts/alert'],
  ])
  assert.equal(calls[4].url.search, '?start_date=2026-10-01&end_date=2026-10-02')
  assert.equal(calls[5].url.search, '?limit=7&offset=2')
  assert.equal(calls[9].url.search, '?limit=9')
  assert.equal(calls[10].url.searchParams.get('investigation_status'), 'IN_REVIEW')
  assert.equal(calls[7].headers['Idempotency-Key'], 'retry-key')
  assert.equal(calls[7].body, '{"amount":"10.50"}')
  assert.equal(calls[12].body, '{"investigation_status":"CLOSED"}')
  assert.ok(calls.every((call) => call.credentials === 'omit'))
})

test('query values are encoded and absent values omitted', async () => {
  const request = createApiClient({ baseUrl: 'http://127.0.0.1:8000/', fetchImpl: async (url) => {
    assert.equal(url.searchParams.get('q'), 'a&b /?')
    assert.equal(url.searchParams.has('missing'), false)
    assert.equal(url.pathname, '/health')
    return json({ status: 'ok', database: 'connected' })
  } })
  assert.equal((await request('/health', { query: { q: 'a&b /?', missing: undefined } })).database, 'connected')
})

test('structured unavailable errors preserve safe retry metadata', async () => {
  const request = createApiClient({ fetchImpl: async () => json({ detail: 'private token', error: { code: 'GRAPH_UNAVAILABLE', retryable: true } }, 503, { 'Retry-After': '5' }) })
  await assert.rejects(request('/health'), (error) => error instanceof ApiError && error.status === 503 && error.retryable && error.retryAfter === 5 && error.code === 'GRAPH_UNAVAILABLE' && !error.message.includes('private'))
})

test('legacy errors and validation errors do not retain raw request inputs', async () => {
  for (const status of [409, 422, 500]) {
    const request = createApiClient({ fetchImpl: async () => json({ detail: status === 422 ? [{ msg: 'Field required', input: 'SECRET', ctx: { secret: 'SECRET' } }] : 'SECRET SQL query' }, status) })
    await assert.rejects(request('/health'), (error) => {
      assert.equal(error.status, status)
      assert.ok(!JSON.stringify(error).includes('SECRET'))
      assert.ok(!error.message.includes('SECRET'))
      assert.deepEqual(error.details, status === 422 ? ['Field required'] : [])
      return true
    })
  }
})

test('invalid JSON success and HTML proxy failures are controlled errors', async () => {
  for (const [status, code] of [[200, 'INVALID_RESPONSE'], [503, 'HTTP_ERROR']]) {
    const request = createApiClient({ fetchImpl: async () => new Response('<html>private</html>', { status }) })
    await assert.rejects(request('/health'), (error) => error.code === code && !error.message.includes('private'))
  }
})

test('network failures hide original diagnostic details', async () => {
  const request = createApiClient({ fetchImpl: async () => { throw new Error('private URL and token') } })
  await assert.rejects(request('/health'), (error) => error.code === 'NETWORK_ERROR' && error.retryable && !error.message.includes('private'))
})

const abortingFetch = (_, { signal }) => new Promise((resolve, reject) => {
  if (signal.aborted) reject(new Error('aborted'))
  else signal.addEventListener('abort', () => reject(new Error('aborted')), { once: true })
})
test('timeout aborts the request', async () => {
  const request = createApiClient({ timeoutMs: 10, fetchImpl: abortingFetch })
  await assert.rejects(request('/health'), (error) => error.code === 'TIMEOUT' && error.retryable)
})
test('caller cancellation is distinct from a retryable timeout', async () => {
  const controller = new AbortController()
  const request = createApiClient({ fetchImpl: abortingFetch })
  const pending = request('/health', { signal: controller.signal })
  controller.abort()
  await assert.rejects(pending, (error) => error.code === 'REQUEST_ABORTED' && !error.retryable)
})
test('invalid public configuration fails without making a request', async () => {
  for (const config of [{ baseUrl: 'https://user:secret@example.com' }, { baseUrl: '//example.com' }, { timeoutMs: NaN }]) {
    const request = createApiClient({ ...config, fetchImpl: () => assert.fail('must not fetch') })
    await assert.rejects(request('/health'), { code: 'CONFIGURATION_ERROR' })
  }
})
