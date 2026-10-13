import { useCallback, useEffect, useState } from 'react'
import { useLocation } from 'react-router-dom'
import { RefreshCw, Search } from 'lucide-react'
import { api } from '../api/index.js'
import { useApiResource } from '../hooks/useApiResource.js'
import { transactionPage, transactionRecord } from '../utils/presentation.js'
import { parseTransactionLookup } from '../utils/graph.js'
import { TransactionTable } from '../components/TransactionTable.jsx'
import { TransactionDetail } from '../components/TransactionDetail.jsx'
import { PaginationControls } from '../components/PaginationControls.jsx'
import { LoadingState } from '../components/LoadingState.jsx'
import { ErrorState } from '../components/ErrorState.jsx'
import { EmptyState } from '../components/EmptyState.jsx'

const PAGE_SIZE = 20
export function Transactions() {
  const location = useLocation()
  const [offset, setOffset] = useState(0)
  const [lookup, setLookup] = useState('')
  const [selectedId, setSelectedId] = useState(null)
  const [linkError, setLinkError] = useState(null)
  useEffect(() => {
    const link = parseTransactionLookup(location.search)
    setLookup(link.id ?? '')
    setSelectedId(link.id)
    setLinkError(link.error)
  }, [location.search])
  const loadPage = useCallback(async (signal) => transactionPage(await api.transactions.list({ limit: PAGE_SIZE, offset }, { signal })), [offset])
  const loadDetail = useCallback(async (signal) => transactionRecord(await api.transactions.get(selectedId, { signal })), [selectedId])
  const page = useApiResource(loadPage)
  const detail = useApiResource(selectedId ? loadDetail : null)
  function selectTransaction(id) {
    if (selectedId === id) detail.refresh()
    else setSelectedId(id)
  }
  function submitLookup(event) {
    event.preventDefault()
    if (lookup.trim()) selectTransaction(lookup.trim())
  }
  return <>
    <section className="view-toolbar">
      <div><span className="eyebrow">TRANSACTION RECORDS</span><h2>Review saved transaction activity.</h2><p>Most recent event first · {PAGE_SIZE} records per page</p></div>
      <button className="secondary-button" disabled={page.status === 'loading'} onClick={page.refresh}><RefreshCw size={15} aria-hidden="true" />Refresh transactions</button>
    </section>
    <form className="lookup-form" onSubmit={submitLookup}>
      <label htmlFor="transaction-lookup">Exact transaction-ID lookup</label>
      <div><input id="transaction-lookup" value={lookup} onChange={(event) => setLookup(event.target.value)} placeholder="Enter a complete transaction ID" required maxLength={64} autoComplete="off" /><button className="primary-button" type="submit" disabled={!lookup.trim() || detail.status === 'loading'}><Search size={16} aria-hidden="true" />Look up</button></div>
      <p>Looks up one exact ID across stored records. It does not filter the table or perform partial search.</p>
    </form>
    {linkError && <div className="graph-validation"><ErrorState message={linkError} /></div>}
    {selectedId && <TransactionDetail resource={detail} onClose={() => setSelectedId(null)} />}
    <section className="panel" aria-label="Transactions">
      {page.status === 'loading' ? <div className="resource-state"><LoadingState message="Loading transactions…" /></div>
        : page.status === 'error' ? <div className="resource-state"><ErrorState message={page.error} onRetry={page.refresh} retryLabel="Retry transactions" /></div>
          : <>
            {page.data.items.length ? <TransactionTable transactions={page.data.items} onSelect={selectTransaction} />
              : <EmptyState title={page.data.total === 0 ? 'No transactions yet' : 'No records on this page'} description="Refresh to check for new records, or return to the previous page." label={null} />}
            <PaginationControls page={page.data} onPage={setOffset} />
          </>}
    </section>
    <p className="page-note">Risk scores are saved model assessments, not verified fraud outcomes. A dash means no valid score is available. Pagination reads the current database; new records may shift page boundaries.</p>
  </>
}
