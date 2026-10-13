import { ApiError } from '../api/client.js'
import { formatScore, formatUtcTimestamp } from './presentation.js'
import { validTransactionId } from './graph.js'

export const INVESTIGATION_STATUSES = ['OPEN', 'IN_REVIEW', 'CLOSED']
export const ALERT_PAGE_SIZE = 20
export const indicatorDescriptions = {
  MULTIPLE_CARD_TRANSACTIONS_24H: 'At least two earlier transactions associated with the card in 24 hours.',
  MULTIPLE_DEVICE_TRANSACTIONS_24H: 'At least two earlier transactions associated with the device in 24 hours.',
  CARD_MULTIPLE_DEVICES_7D: 'The card was associated with at least two historical devices in 7 days.',
  DEVICE_MULTIPLE_CARDS_7D: 'The device was associated with at least two historical cards in 7 days.',
  AMOUNT_AT_LEAST_3X_CARD_AVERAGE_7D: 'The amount was at least three times the positive historical 7-day card average.',
}
export const validAlertId = (value) => typeof value === 'string' && /^[A-Za-z0-9_-]{1,36}$/.test(value)

function invalidResponse() {
  throw new ApiError('The backend returned an unexpected alert response.', { code: 'INVALID_RESPONSE' })
}
/** Retain permitted fields only; unrecognized legacy indicator text is withheld. */
export function alertRecord(raw) {
  if (!raw || !validAlertId(raw.alert_id) || !validTransactionId(raw.transaction_id)
    || formatScore(raw.risk_score) === '—' || !INVESTIGATION_STATUSES.includes(raw.investigation_status)
    || formatUtcTimestamp(raw.created_at) === 'Unavailable' || formatUtcTimestamp(raw.updated_at) === 'Unavailable'
    || !Array.isArray(raw.risk_indicators) || raw.risk_indicators.some((item) => typeof item !== 'string')) invalidResponse()
  const { alert_id, transaction_id, risk_score, investigation_status, created_at, updated_at } = raw
  return { alert_id, transaction_id, risk_score, investigation_status, created_at, updated_at,
    risk_indicators: raw.risk_indicators.filter((code) => Object.hasOwn(indicatorDescriptions, code)),
    withheld_indicator_count: raw.risk_indicators.filter((code) => !Object.hasOwn(indicatorDescriptions, code)).length }
}
export function alertPage(raw) {
  if (!Array.isArray(raw?.items) || !Number.isSafeInteger(raw.total) || raw.total < 0
    || !Number.isSafeInteger(raw.offset) || raw.offset < 0 || !Number.isInteger(raw.limit)
    || raw.limit < 1 || raw.limit > 100 || raw.items.length > raw.limit) invalidResponse()
  return { items: raw.items.map(alertRecord), total: raw.total, offset: raw.offset, limit: raw.limit }
}
export function parseInvestigationSearch(search) {
  const params = new URLSearchParams(search)
  const alertId = params.get('alert_id')
  const filter = params.get('investigation_status') ?? 'ALL'
  const offsetText = params.get('offset') ?? '0'
  let error = null
  if (['alert_id', 'investigation_status', 'offset'].some((name) => params.getAll(name).length > 1)) error = 'The investigation link contains duplicate parameters.'
  else if (alertId !== null && !validAlertId(alertId)) error = 'Use one valid alert ID: 1–36 letters, digits, underscores or hyphens.'
  else if (!['ALL', ...INVESTIGATION_STATUSES].includes(filter)) error = 'Choose ALL, OPEN, IN_REVIEW or CLOSED.'
  else if (!/^\d+$/.test(offsetText) || !Number.isSafeInteger(Number(offsetText)) || Number(offsetText) > Number.MAX_SAFE_INTEGER - ALERT_PAGE_SIZE) error = 'The investigation page offset must be a nonnegative whole number.'
  return { alertId, filter, offset: Number(offsetText), error }
}
export function investigationSearch({ alertId, filter = 'ALL', offset = 0 }) {
  const params = new URLSearchParams()
  if (alertId) params.set('alert_id', alertId)
  if (filter !== 'ALL') params.set('investigation_status', filter)
  if (offset > 0) params.set('offset', String(offset))
  return params.size ? `?${params}` : ''
}
export function alertListQuery(filter, offset) {
  if (!['ALL', ...INVESTIGATION_STATUSES].includes(filter)) throw new Error('Unsupported investigation filter')
  return { limit: ALERT_PAGE_SIZE, offset, ...(filter === 'ALL' ? {} : { investigation_status: filter }) }
}
export function statusUpdateAction(current, next, confirmed = false) {
  if (!INVESTIGATION_STATUSES.includes(current) || !INVESTIGATION_STATUSES.includes(next)) throw new Error('Choose a supported investigation status.')
  if (current === next) return 'noop'
  return next === 'CLOSED' && !confirmed ? 'confirm' : 'save'
}
export async function saveInvestigationStatus(alert, next, { confirmed = false, alertsApi, signal }) {
  const action = statusUpdateAction(alert.investigation_status, next, confirmed)
  if (action === 'confirm') throw new Error('Confirm closing this investigation before saving.')
  if (action === 'noop') return alert
  const saved = alertRecord(await alertsApi.updateStatus(alert.alert_id, next, { signal }))
  if (saved.alert_id !== alert.alert_id || saved.transaction_id !== alert.transaction_id || saved.investigation_status !== next) invalidResponse()
  return saved
}
export function alertErrorMessage(error) {
  if (!(error instanceof ApiError)) return 'The alert request could not be completed. Please try again.'
  if (error.status === 404) return 'This alert was not found. Choose another alert or check the link.'
  if (error.status === 422) return 'The alert request was rejected. Check the status and page values.'
  if (error.status === 503) return 'The alert service is unavailable. Retry after the backend recovers.'
  return error.message
}
export function updateErrorMessage(error) {
  if (error instanceof ApiError && (['TIMEOUT', 'NETWORK_ERROR', 'INVALID_RESPONSE'].includes(error.code))) {
    return 'The update result could not be confirmed. Refresh the alert before retrying; the server may have saved the change.'
  }
  return `${alertErrorMessage(error)} The previous confirmed status remains displayed; refresh to confirm the server state.`
}
