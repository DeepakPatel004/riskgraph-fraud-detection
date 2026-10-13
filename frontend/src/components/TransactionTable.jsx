import { formatAmount, formatScore, formatUtcTimestamp } from '../utils/presentation.js'
import { RiskBadge } from './RiskBadge.jsx'
export function TransactionTable({ transactions, onSelect }) {
  return <div className="table-scroll" role="region" aria-label="Transaction records, scroll horizontally for all columns" tabIndex={0}>
    <table className="transaction-table">
      <caption className="sr-only">Transactions ordered by most recent UTC event time</caption>
      <thead><tr>{['Transaction ID', 'Amount', 'Currency', 'Event time (UTC)', 'Risk score', 'Risk level', 'Processing', 'Graph sync'].map((name) => <th key={name} scope="col">{name}</th>)}</tr></thead>
      <tbody>{transactions.map((transaction) => <tr key={transaction.transaction_id}>
        <th scope="row"><button className="record-link" onClick={() => onSelect(transaction.transaction_id)}>{transaction.transaction_id}</button></th>
        <td className="number-cell">{formatAmount(transaction.amount)}</td><td>{transaction.currency}</td>
        <td className="date-cell">{formatUtcTimestamp(transaction.event_time)}</td>
        <td className="number-cell">{formatScore(transaction.risk_score)}</td><td><RiskBadge transaction={transaction} /></td>
        <td><span className="neutral-badge">{transaction.processing_status}</span></td><td><span className="neutral-badge">{transaction.graph_sync_status}</span></td>
      </tr>)}</tbody>
    </table>
  </div>
}
