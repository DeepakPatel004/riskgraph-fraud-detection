/** Existing FastAPI contracts. Decimal amounts stay strings; graph truncated may be null. */
import { createApiClient } from './client.js'

function identifier(value) {
  if (typeof value !== 'string' || !value || value === '.' || value === '..') throw new TypeError('A record ID is required')
  return encodeURIComponent(value)
}

export function createRiskGraphApi(config) {
  const request = createApiClient(config)
  return {
    health: (options) => request('/health', options),
    dashboard: {
      summary: (options) => request('/api/v1/dashboard/summary', options),
      riskDistribution: (options) => request('/api/v1/dashboard/risk-distribution', options),
      alertStatus: (options) => request('/api/v1/dashboard/alert-status', options),
      transactionTrends: ({ start_date, end_date } = {}, options = {}) => request('/api/v1/dashboard/transaction-trends', { ...options, query: { start_date, end_date } }),
    },
    transactions: {
      list: ({ limit = 20, offset = 0 } = {}, options = {}) => request('/api/v1/transactions', { ...options, query: { limit, offset } }),
      get: (id, options) => request(`/api/v1/transactions/${identifier(id)}`, options),
      create: (payload, { idempotencyKey, signal } = {}) => request('/api/v1/transactions', { method: 'POST', body: payload, idempotencyKey, signal }),
      score: (payload, { idempotencyKey, signal } = {}) => request('/api/v1/transactions/score', { method: 'POST', body: payload, idempotencyKey, signal }),
      graph: (id, { limit = 50 } = {}, options = {}) => request(`/api/v1/transactions/${identifier(id)}/graph`, { ...options, query: { limit } }),
    },
    alerts: {
      list: ({ limit = 20, offset = 0, investigation_status } = {}, options = {}) => request('/api/v1/alerts', { ...options, query: { limit, offset, investigation_status } }),
      get: (id, options) => request(`/api/v1/alerts/${identifier(id)}`, options),
      updateStatus: (id, investigation_status, { signal } = {}) => request(`/api/v1/alerts/${identifier(id)}`, { method: 'PATCH', body: { investigation_status }, signal }),
    },
  }
}
