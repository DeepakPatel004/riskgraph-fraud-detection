import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../api/index.js'

/** One health request on mount. Navigation does not poll or reload model data. */
export function useBackendConnection() {
  const [connection, setConnection] = useState({ status: 'checking', message: '' })
  const active = useRef(null)
  const checkConnection = useCallback(async () => {
    active.current?.abort()
    const controller = new AbortController()
    active.current = controller
    setConnection({ status: 'checking', message: '' })
    try {
      const result = await api.health({ signal: controller.signal })
      if (controller.signal.aborted) return
      if (result?.status !== 'ok' || result?.database !== 'connected') {
        throw new Error('Unexpected health response')
      }
      setConnection({ status: 'connected', message: 'FastAPI and its SQL database are reachable.' })
    } catch (error) {
      if (!controller.signal.aborted) {
        setConnection({ status: 'disconnected', message: error.name === 'ApiError' ? error.message : 'The backend returned an unexpected health response.' })
      }
    }
  }, [])
  useEffect(() => {
    checkConnection()
    return () => active.current?.abort()
  }, [checkConnection])
  return { connection, checkConnection }
}
