import { useCallback, useEffect, useRef, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { ApiError } from '../api/client.js'
import { readResource } from './readResource.js'
import { graphRequest, parseGraphSearch, graphSearch, mapGraphResponse, graphErrorMessage } from '../utils/graph.js'

/** URL navigation loads once. Only a successful manual search pushes a URL. */
export function useGraphExplorer(requestGraph) {
  const location = useLocation()
  const navigate = useNavigate()
  const [form, setForm] = useState(() => parseGraphSearch(location.search))
  const [validation, setValidation] = useState(null)
  const [resource, setResource] = useState({ status: 'idle', data: null, error: null })
  const active = useRef(null)
  const ownNavigation = useRef(null)
  const currentSearch = useRef(location.search)
  currentSearch.current = location.search

  const load = useCallback((request, writeUrl) => {
    active.current?.abort()
    const controller = new AbortController()
    active.current = controller
    setValidation(null)
    setResource({ status: 'loading', data: null, error: null, request })
    readResource(async (signal) => {
      try {
        return mapGraphResponse(await requestGraph(request.id, { limit: request.limit }, { signal }), request)
      } catch (error) {
        throw new ApiError(graphErrorMessage(error))
      }
    }, controller.signal, (result) => {
      setResource({ ...result, request })
      const search = graphSearch(request)
      if (result.status === 'success' && writeUrl && search !== currentSearch.current) {
        ownNavigation.current = search
        navigate({ search })
      }
    })
  }, [requestGraph, navigate])

  useEffect(() => {
    const parsed = parseGraphSearch(location.search)
    setForm(parsed)
    if (ownNavigation.current === location.search) {
      ownNavigation.current = null
      return // This successful search is already loaded; do not request it twice.
    }
    ownNavigation.current = null
    active.current?.abort()
    setValidation(parsed.error)
    if (parsed.request) load(parsed.request, false)
    else setResource({ status: 'idle', data: null, error: null })
  }, [location.search, location.key, load])
  useEffect(() => () => active.current?.abort(), [])

  function submit(event) {
    event.preventDefault()
    try { load(graphRequest(form.id, form.limit), true) }
    catch (error) { setValidation(error.message) }
  }
  return { form, setForm, validation, resource, submit,
    retry: () => resource.request && load(resource.request, true) }
}
