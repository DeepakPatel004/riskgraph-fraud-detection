/** Fetch transport shared by all features. Never logs request bodies or errors. */
export class ApiError extends Error {
  constructor(message, { status = 0, code = 'REQUEST_FAILED', retryable = false, details = [], retryAfter = null } = {}) {
    super(message)
    this.name = 'ApiError'
    Object.assign(this, { status, code, retryable, details, retryAfter })
  }
}

const statusMessages = {
  404: 'The requested record was not found.',
  409: 'This request conflicts with an existing record.',
  422: 'Check the request values and try again.',
  500: 'The backend could not complete this request.',
  503: 'The backend or a required dependency is unavailable.',
}

function configuredBase(baseUrl) {
  if (typeof baseUrl !== 'string' || !baseUrl.trim()) throw new Error('Invalid base URL')
  const value = baseUrl.trim().replace(/\/+$/, '')
  if (!value || value.startsWith('//')) throw new Error('Invalid base URL')
  if (!value.startsWith('/') && !/^https?:\/\//.test(value)) throw new Error('Invalid base URL')
  const url = new URL(value, globalThis.location?.origin || 'http://localhost')
  if (url.username || url.password || url.search || url.hash) throw new Error('Invalid base URL')
  return value
}

export function createApiClient({ baseUrl = '/backend', timeoutMs = 8000, fetchImpl = globalThis.fetch } = {}) {
  return async function request(path, { method = 'GET', query = {}, body, idempotencyKey, signal } = {}) {
    let url
    try {
      const base = configuredBase(baseUrl)
      if (!Number.isInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 60000) throw new Error('Invalid timeout')
      if (!path.startsWith('/') || path.startsWith('//')) throw new Error('Invalid request path')
      url = new URL(`${base}${path}`, globalThis.location?.origin || 'http://localhost')
      for (const [key, value] of Object.entries(query)) {
        if (value !== undefined && value !== null) url.searchParams.set(key, String(value))
      }
    } catch {
      throw new ApiError('Check the frontend API configuration.', { code: 'CONFIGURATION_ERROR' })
    }
    const controller = new AbortController()
    let timedOut = false
    const cancel = () => controller.abort()
    if (signal?.aborted) cancel()
    signal?.addEventListener('abort', cancel, { once: true })
    const timer = setTimeout(() => { timedOut = true; controller.abort() }, timeoutMs)
    try {
      const headers = { Accept: 'application/json' }
      if (body !== undefined) headers['Content-Type'] = 'application/json'
      if (idempotencyKey !== undefined) headers['Idempotency-Key'] = idempotencyKey
      const response = await fetchImpl(url, {
        method, headers, signal: controller.signal, credentials: 'omit',
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      })
      let data
      try {
        data = await response.json()
      } catch (error) {
        if (controller.signal.aborted) throw error
        if (response.ok) throw new ApiError('The backend returned an unexpected response.', { code: 'INVALID_RESPONSE' })
        data = null // A proxy HTML error is not a backend JSON contract.
      }
      if (!response.ok) {
        const code = data?.error?.code
        const retry = response.headers.get('Retry-After')
        throw new ApiError(statusMessages[response.status] || 'The request could not be completed.', {
          status: response.status,
          code: typeof code === 'string' && /^[A-Z_0-9]{1,64}$/.test(code) ? code : 'HTTP_ERROR',
          retryable: typeof data?.error?.retryable === 'boolean' ? data.error.retryable : response.status === 503,
          // Keep validation messages only; never retain input/ctx, SQL or raw bodies.
          details: Array.isArray(data?.detail) ? data.detail.filter((item) => typeof item?.msg === 'string').map((item) => item.msg) : [],
          retryAfter: retry && /^\d+$/.test(retry) ? Number(retry) : null,
        })
      }
      return data
    } catch (error) {
      if (timedOut) throw new ApiError('The backend took too long to respond.', { code: 'TIMEOUT', retryable: true })
      if (signal?.aborted) throw new ApiError('Request cancelled.', { code: 'REQUEST_ABORTED' })
      if (error instanceof ApiError) throw error
      throw new ApiError('Unable to reach the backend. Check that FastAPI is running.', { code: 'NETWORK_ERROR', retryable: true })
    } finally {
      clearTimeout(timer)
      signal?.removeEventListener('abort', cancel)
    }
  }
}
