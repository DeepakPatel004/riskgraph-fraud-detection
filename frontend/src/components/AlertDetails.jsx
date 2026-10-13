import { useEffect, useRef } from 'react'
import { Link } from 'react-router-dom'
import { X, ArrowUpRight } from 'lucide-react'
import { LoadingState } from './LoadingState.jsx'
import { ErrorState } from './ErrorState.jsx'
import { InvestigationStatusBadge } from './InvestigationStatusBadge.jsx'
import { InvestigationStatusControl } from './InvestigationStatusControl.jsx'
import { indicatorDescriptions } from '../utils/investigations.js'
import { formatScore, formatUtcTimestamp, graphExplorerUrl } from '../utils/presentation.js'
import { transactionLookupUrl } from '../utils/graph.js'

export function AlertDetails({ resource, mutation, onClose, onUpdate, filter }) {
  const panel = useRef(null)
  useEffect(() => { panel.current?.focus() }, [])
  const alert = resource.data
  return <section ref={panel} tabIndex={-1} className="panel detail-panel alert-details" aria-label="Selected alert details">
    <div className="section-heading"><h2>Investigation details</h2><button className="icon-button" onClick={onClose} disabled={mutation.status === 'pending'} aria-label="Close alert details"><X size={17} aria-hidden="true" /></button></div>
    {resource.status === 'loading' ? <LoadingState message="Loading alert details…" />
      : resource.status === 'error' ? <ErrorState message={resource.error} onRetry={resource.refresh} retryLabel="Retry alert details" />
        : alert && <>
          <dl className="detail-grid">
            <div><dt>Alert ID</dt><dd>{alert.alert_id}</dd></div><div><dt>Transaction ID</dt><dd>{alert.transaction_id}</dd></div>
            <div><dt>Saved risk score (0–1)</dt><dd>{formatScore(alert.risk_score)}</dd></div>
            <div><dt>Investigation status</dt><dd><InvestigationStatusBadge status={alert.investigation_status} /></dd></div>
            <div><dt>Created</dt><dd>{formatUtcTimestamp(alert.created_at)}</dd></div><div><dt>Updated</dt><dd>{formatUtcTimestamp(alert.updated_at)}</dd></div>
          </dl>
          {filter !== 'ALL' && filter !== alert.investigation_status && <p className="muted-note">This selected alert is outside the current list filter.</p>}
          <div className="alert-links"><Link className="secondary-button" to={transactionLookupUrl(alert.transaction_id)}>View transaction<ArrowUpRight size={15} aria-hidden="true" /></Link><Link className="secondary-button" to={graphExplorerUrl(alert.transaction_id)}>Open Graph Explorer<ArrowUpRight size={15} aria-hidden="true" /></Link></div>
          <section className="saved-indicators" aria-label="Saved historical indicators"><h3>Saved historical signals</h3>
            {alert.risk_indicators.length ? <ul>{alert.risk_indicators.map((code, index) => <li key={`${code}:${index}`}>{indicatorDescriptions[code]}</li>)}</ul>
              : <p>No recognized historical indicators are available for this alert.</p>}
            {alert.withheld_indicator_count > 0 && <p>{alert.withheld_indicator_count} unrecognized saved indicator(s) withheld. Legacy text may contain sensitive information.</p>}
            <p className="muted-note">These are saved historical observations, not causal explanations of the model or proof of fraud.</p>
          </section>
          <InvestigationStatusControl alert={alert} pending={mutation.status === 'pending'} onUpdate={onUpdate} />
        </>}
  </section>
}
