import { ApiError } from '../api/client.js'

export const formatCount = (value) => new Intl.NumberFormat('en-US').format(value)

/** Preserve SQL decimal precision instead of converting money to a JS float. */
export function formatAmount(value) {
  if (typeof value !== 'string' || !/^\d+(\.\d{1,2})?$/.test(value)) return 'Unavailable'
  const [whole, fraction = ''] = value.split('.')
  return `${formatCount(BigInt(whole))}.${fraction.padEnd(2, '0')}`
}
export function formatScore(value) {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 1 ? value.toFixed(4) : '—'
}
/** Use saved classifications; never compare a score to a frontend threshold. */
export function riskCategory(transaction) {
  if (transaction.risk_score === null) return 'UNSCORED'
  return ['LOW', 'HIGH'].includes(transaction.risk_level) ? transaction.risk_level : 'UNKNOWN'
}
export function formatUtcTimestamp(value) {
  if (typeof value !== 'string' || !/(Z|[+-]\d{2}:\d{2})$/i.test(value)) return 'Unavailable'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return 'Unavailable'
  return new Intl.DateTimeFormat('en-GB', {
    timeZone: 'UTC', year: 'numeric', month: 'short', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
  }).format(date) + ' UTC'
}
export function formatUtcDate(value) {
  const date = new Date(`${value}T00:00:00Z`)
  return Number.isNaN(date.getTime()) ? 'Unavailable' : new Intl.DateTimeFormat('en-GB', {
    timeZone: 'UTC', day: '2-digit', month: 'short', year: 'numeric',
  }).format(date)
}
function invalidResponse() {
  throw new ApiError('The backend returned an unexpected response.', { code: 'INVALID_RESPONSE' })
}
function count(value) {
  if (!Number.isSafeInteger(value) || value < 0) invalidResponse()
  return value
}
export function summaryMetrics(summary) {
  return [
    { name: 'Total transactions', value: count(summary?.total_transactions), note: 'All stored records', tone: 'blue' },
    { name: 'Scored transactions', value: count(summary?.scored_transactions), note: 'With a saved risk score', tone: 'teal' },
    { name: 'High-risk transactions', value: count(summary?.high_risk_transactions), note: 'Flagged for review · Not confirmed fraud', tone: 'amber' },
    { name: 'Open investigations', value: count(summary?.open_alerts), note: 'Alerts with status OPEN', tone: 'purple' },
  ]
}
export function categorySeries(counts, categories) {
  return categories.map((name) => ({ name, count: count(counts?.[name]) }))
}
export function trendSeries(response) {
  if (!Array.isArray(response?.items)) invalidResponse()
  return response.items.map((item) => {
    if (typeof item.date !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(item.date)
      || formatUtcDate(item.date) === 'Unavailable') invalidResponse()
    return { date: item.date, transaction_count: count(item.transaction_count) }
  })
}
export function transactionPage(response) {
  if (!Array.isArray(response?.items)) invalidResponse()
  count(response.total)
  count(response.offset)
  if (!Number.isSafeInteger(response.limit) || response.limit < 1 || response.limit > 100
    || response.items.length > response.limit) invalidResponse()
  return { ...response, items: response.items.map(transactionRecord) }
}
/** Only retain display fields. Payment tokens do not belong in view state. */
export function transactionRecord(response) {
  if (!response || typeof response.transaction_id !== 'string'
    || formatAmount(response.amount) === 'Unavailable'
    || formatUtcTimestamp(response.event_time) === 'Unavailable'
    || typeof response.currency !== 'string'
    || typeof response.processing_status !== 'string' || typeof response.graph_sync_status !== 'string'
    || (response.risk_score !== null && formatScore(response.risk_score) === '—')
    || (response.risk_level !== null && typeof response.risk_level !== 'string')) invalidResponse()
  const { transaction_id, amount, currency, event_time, risk_score, risk_level, processing_status, graph_sync_status } = response
  return { transaction_id, amount, currency, event_time, risk_score, risk_level, processing_status, graph_sync_status }
}
export function pagination({ total, offset, limit, items }) {
  return {
    start: items.length ? offset + 1 : 0, end: items.length ? offset + items.length : 0,
    previous: Math.max(0, offset - limit), next: offset + limit,
    hasPrevious: offset > 0, hasNext: offset + limit < total,
  }
}
export function graphExplorerUrl(id) {
  return `/graph-explorer?${new URLSearchParams({ transaction_id: id })}`
}
