import { useCallback, useEffect, useMemo } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { RefreshCw, ShieldCheck } from 'lucide-react'
import { api } from '../api/index.js'
import { ApiError } from '../api/client.js'
import { useApiResource } from '../hooks/useApiResource.js'
import { useInvestigationUpdate } from '../hooks/useInvestigationUpdate.js'
import { alertPage, alertRecord, alertListQuery, alertErrorMessage, parseInvestigationSearch, investigationSearch, INVESTIGATION_STATUSES } from '../utils/investigations.js'
import { AlertTable } from '../components/AlertTable.jsx'
import { AlertDetails } from '../components/AlertDetails.jsx'
import { PaginationControls } from '../components/PaginationControls.jsx'
import { LoadingState } from '../components/LoadingState.jsx'
import { ErrorState } from '../components/ErrorState.jsx'
import { EmptyState } from '../components/EmptyState.jsx'

// Test pages can inject an isolated fixture API; normal routing always uses Fetch.
export function Investigations({ alertsApi = api.alerts }) {
  const location = useLocation()
  const navigate = useNavigate()
  const query = useMemo(() => parseInvestigationSearch(location.search), [location.search])
  const { alertId, filter, offset, error } = query
  const loadPage = useCallback(async (signal) => {
    try { return alertPage(await alertsApi.list(alertListQuery(filter, offset), { signal })) }
    catch (failure) { throw new ApiError(alertErrorMessage(failure)) }
  }, [alertsApi, filter, offset])
  const loadDetail = useCallback(async (signal) => {
    try {
      const alert = alertRecord(await alertsApi.get(alertId, { signal }))
      if (alert.alert_id !== alertId) throw new ApiError('The backend returned an unexpected alert response.')
      return alert
    } catch (failure) { throw new ApiError(alertErrorMessage(failure)) }
  }, [alertsApi, alertId])
  const page = useApiResource(error ? null : loadPage)
  const detail = useApiResource(error || !alertId ? null : loadDetail)
  const mutation = useInvestigationUpdate(alertId, alertsApi, () => { page.refresh(); detail.refresh() })
  const pending = mutation.status === 'pending'
  function changeQuery(patch, replace = false) { navigate({ search: investigationSearch({ ...query, ...patch }) }, { replace }) }
  // A status change may remove the last row on a filtered page. Keep the filter.
  useEffect(() => {
    if (page.status !== 'success') return
    const lastOffset = Math.max(0, Math.floor((page.data.total - 1) / page.data.limit) * page.data.limit)
    if (offset > lastOffset) navigate({ search: investigationSearch({ ...query, offset: lastOffset }) }, { replace: true })
  }, [page.status, page.data, offset, query, navigate])
  function refreshDetails() { mutation.clear(); detail.refresh() }
  function refresh() { mutation.clear(); page.refresh(); if (alertId) detail.refresh() }
  return <>
    <section className="view-toolbar"><div><span className="eyebrow">HUMAN REVIEW</span><h2>Investigate the signal. Review the context.</h2><p>Alerts flag saved HIGH-risk assessments, not verified fraud.</p></div><button className="secondary-button" disabled={pending || page.status === 'loading' || !!error} onClick={refresh}><RefreshCw size={15} aria-hidden="true" />Refresh investigations</button></section>
    <div className="investigation-filter"><label htmlFor="investigation-filter">Investigation status</label><select id="investigation-filter" value={['ALL', ...INVESTIGATION_STATUSES].includes(filter) ? filter : 'ALL'} disabled={pending} onChange={(event) => changeQuery({ filter: event.target.value, offset: 0 })}>{['ALL', ...INVESTIGATION_STATUSES].map((status) => <option key={status}>{status}</option>)}</select><p>20 records per page · Newest alerts first</p></div>
    {error && <div className="panel resource-state"><ErrorState message={error} /><button className="secondary-button reset-investigations" onClick={() => changeQuery({ alertId: null, filter: 'ALL', offset: 0 })}>Reset investigation view</button></div>}
    {mutation.status === 'pending' && <div className="investigation-notice" role="status">Saving status. Conflicting controls are disabled until the server responds.</div>}
    {mutation.status === 'success' && <div className="investigation-notice success" role="status">{mutation.message}</div>}
    {mutation.status === 'error' && <div className="investigation-notice"><ErrorState message={mutation.message} /></div>}
    {!error && alertId && <AlertDetails key={alertId} resource={{ ...detail, refresh: refreshDetails }} mutation={mutation} filter={filter} onClose={() => changeQuery({ alertId: null })} onUpdate={(next, confirmed) => detail.data && mutation.update(detail.data, next, confirmed)} />}
    {!error && <section className="panel" aria-label="Investigation alert list">
      {page.status === 'loading' ? <div className="resource-state"><LoadingState message="Loading investigation alerts…" /></div>
        : page.status === 'error' ? <div className="resource-state"><ErrorState message={page.error} onRetry={page.refresh} retryLabel="Retry alert list" /></div>
          : page.data && <>
            {page.data.items.length ? <AlertTable alerts={page.data.items} selectedId={alertId} disabled={pending} onSelect={(id) => id === alertId ? refreshDetails() : changeQuery({ alertId: id })} />
              : <EmptyState icon={ShieldCheck} title={filter === 'ALL' ? 'No investigation alerts yet' : `No ${filter} alerts`} description="No saved alerts match this view. Refresh to check for changes or choose another status filter." label={null} />}
            <PaginationControls page={page.data} disabled={pending} onPage={(next) => changeQuery({ offset: next })} />
          </>}
    </section>}
    <p className="page-note">OPEN awaits review; IN_REVIEW is being examined; CLOSED means review finished. Closing or reopening an alert changes no model prediction. Access is restricted to local demonstration; authentication and operator audit history are not implemented.</p>
  </>
}
