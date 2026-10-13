import { Link } from 'react-router-dom'
import { ArrowUpRight } from 'lucide-react'
import { formatAmount } from '../utils/presentation.js'
import { transactionLookupUrl } from '../utils/graph.js'

export function GraphNodeDetails({ node }) {
  return <section className="panel graph-node-details" aria-label="Selected node details">
    <h2>Selected node</h2>
    {!node ? <p className="page-note">Choose a node in the graph or the node selector to see its permitted details.</p> : <>
      <span className={`node-type node-${node.type.toLowerCase()}`}>{node.type}</span>
      <h3>{node.label}</h3>
      <dl>
        <dt>Opaque node ID</dt><dd className="opaque-id">{node.id}</dd>
        {node.type === 'Transaction' && <>
          <dt>Amount</dt><dd>{node.properties.amount === undefined ? 'Not provided' : formatAmount(node.properties.amount)}</dd>
          <dt>Currency</dt><dd>{node.properties.currency ?? 'Not provided'}</dd>
        </>}
      </dl>
      {node.type === 'Transaction' ? <Link className="secondary-button" to={transactionLookupUrl(node.label)}>View transaction<ArrowUpRight size={15} aria-hidden="true" /></Link>
        : <p className="page-note">A stable pseudonymous identifier. Raw payment tokens are not displayed or reconstructed.</p>}
    </>}
  </section>
}
