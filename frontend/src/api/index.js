import { createRiskGraphApi } from './endpoints.js'

// Every VITE_ variable is public. This config contains no credentials.
export const api = createRiskGraphApi({
  baseUrl: import.meta.env.VITE_API_BASE_URL || '/backend',
  timeoutMs: Number(import.meta.env.VITE_API_TIMEOUT_MS || 8000),
})
