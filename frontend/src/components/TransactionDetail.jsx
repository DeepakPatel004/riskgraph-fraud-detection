import { Link } from 'react-router-dom'
import { X, ArrowUpRight } from 'lucide-react'
import { RiskBadge } from './RiskBadge.jsx'
import { LoadingState } from './LoadingState.jsx'
import { ErrorState } from './ErrorState.jsx'
import { formatAmount, formatScore, formatUtcTimestamp, graphExplorerUrl } from '../utils/presentation.js'
export function TransactionDetail({ resource, onClose }) {
  const transaction = resource.data
  return <section className="panel detail-panel" aria-label="Selected transaction details">
    <div className="section-heading"><h2>Transaction details</h2><button className="icon-button" onClick={onClose} aria-label="Close transaction details"><X size={17} aria-hidden="true" /></button></div>
    {resource.status === 'loading' ? <LoadingState message="Loading transaction details…" />
      : resource.status === 'error' ? <ErrorState message={resource.error} onRetry={resource.refresh} retryLabel="Retry lookup" />
        : transaction && <>
          <dl className="detail-grid">
            <div><dt>Transaction ID</dt><dd>{transaction.transaction_id}</dd></div>
            <div><dt>Amount</dt><dd>{formatAmount(transaction.amount)} {transaction.currency}</dd></div>
            <div><dt>Event time</dt><dd>{formatUtcTimestamp(transaction.event_time)}</dd></div>
            <div><dt>Risk score (0–1)</dt><dd>{formatScore(transaction.risk_score)}</dd></div>
            <div><dt>Risk level</dt><dd><RiskBadge transaction={transaction} /></dd></div>
            <div><dt>Processing status</dt><dd>{transaction.processing_status}</dd></div>
            <div><dt>Graph synchronization</dt><dd>{transaction.graph_sync_status}</dd></div>
          </dl>
          <Link className="secondary-button graph-link" to={graphExplorerUrl(transaction.transaction_id)}>Open Graph Explorer<ArrowUpRight size={15} aria-hidden="true" /></Link>
          <p className="muted-note">Graph access requires verified synchronization. Card and device tokens are intentionally omitted.</p>
        </>}
  </section>
}
