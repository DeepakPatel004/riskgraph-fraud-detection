import { useCallback, useEffect, useState } from 'react'
import { readResource } from './readResource.js'

/** Callers supply a stable loader (module function or useCallback). No polling. */
export function useApiResource(load) {
  const [revision, setRevision] = useState(0)
  const [result, setResult] = useState(null)
  useEffect(() => {
    if (!load) return
    const controller = new AbortController()
    readResource(load, controller.signal, (value) => setResult({ ...value, load, revision }))
    return () => controller.abort()
  }, [load, revision])
  const refresh = useCallback(() => setRevision((value) => value + 1), [])
  // Hide old pages/details immediately when the requested resource changes.
  const current = result?.load === load && result?.revision === revision
    ? result : { status: load ? 'loading' : 'idle', data: null, error: null }
  return { ...current, refresh }
}
