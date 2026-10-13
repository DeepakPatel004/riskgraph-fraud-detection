import { useEffect, useRef, useState } from 'react'
import { saveInvestigationStatus, updateErrorMessage } from '../utils/investigations.js'

/** No optimistic writes or automatic retries: a timed-out PATCH may have committed. */
export function useInvestigationUpdate(alertId, alertsApi, onSaved) {
  const [state, setState] = useState({ id: null, status: 'idle', message: null })
  const active = useRef(null)
  const currentId = useRef(alertId)
  currentId.current = alertId
  useEffect(() => {
    setState({ id: alertId, status: 'idle', message: null })
    return () => { active.current?.abort(); active.current = null }
  }, [alertId])
  async function update(alert, next, confirmed = false) {
    if (active.current || alert.alert_id !== currentId.current) return
    const controller = new AbortController()
    active.current = controller
    setState({ id: alert.alert_id, status: 'pending', message: null })
    try {
      await saveInvestigationStatus(alert, next, { confirmed, alertsApi, signal: controller.signal })
      if (controller.signal.aborted || currentId.current !== alert.alert_id) return
      setState({ id: alert.alert_id, status: 'success', message: `Server confirmed ${next}.` })
      onSaved()
    } catch (error) {
      if (!controller.signal.aborted && currentId.current === alert.alert_id) {
        setState({ id: alert.alert_id, status: 'error', message: updateErrorMessage(error) })
      }
    } finally {
      if (active.current === controller) active.current = null
    }
  }
  function clear() {
    if (!active.current) setState({ id: alertId, status: 'idle', message: null })
  }
  return { ...(state.id === alertId ? state : { status: 'idle', message: null }), update, clear }
}
