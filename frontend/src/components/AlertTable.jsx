import { formatScore, formatUtcTimestamp } from '../utils/presentation.js'
import { InvestigationStatusBadge } from './InvestigationStatusBadge.jsx'
export function AlertTable({ alerts, selectedId, onSelect, disabled }) {
  return <div className="table-scroll" role="region" aria-label="Investigation alerts, scroll horizontally for all columns" tabIndex={0}>
    <table className="transaction-table alert-table">
      <caption className="sr-only">Alerts ordered by creation time, newest first. Alerts are review flags, not confirmed fraud.</caption>
      <thead><tr>{['Alert ID', 'Transaction ID', 'Saved risk score (0–1)', 'Investigation status', 'Created (UTC)'].map((name) => <th key={name} scope="col">{name}</th>)}</tr></thead>
      <tbody>{alerts.map((alert) => <tr key={alert.alert_id} className={selectedId === alert.alert_id ? 'selected-alert' : ''}>
        <th scope="row"><button className="record-link" disabled={disabled} aria-pressed={selectedId === alert.alert_id} onClick={() => onSelect(alert.alert_id)}>{alert.alert_id}</button></th>
        <td>{alert.transaction_id}</td><td className="number-cell">{formatScore(alert.risk_score)}</td>
        <td><InvestigationStatusBadge status={alert.investigation_status} /></td><td className="date-cell">{formatUtcTimestamp(alert.created_at)}</td>
      </tr>)}</tbody>
    </table>
  </div>
}
