import { useEffect, useState } from 'react'
import { Network } from 'lucide-react'
import { api } from '../api/index.js'
import { useGraphExplorer } from '../hooks/useGraphExplorer.js'
import { truncationMessage } from '../utils/graph.js'
import { GraphViewer } from '../components/GraphViewer.jsx'
import { GraphNodeDetails } from '../components/GraphNodeDetails.jsx'
import { LoadingState } from '../components/LoadingState.jsx'
import { ErrorState } from '../components/ErrorState.jsx'
import { EmptyState } from '../components/EmptyState.jsx'

// Dependency injection is used only by isolated frontend fixtures, never a demo fallback.
export function GraphExplorer({ requestGraph = api.transactions.graph }) {
  const { form, setForm, validation, resource, submit, retry } = useGraphExplorer(requestGraph)
  const [selectedId, setSelectedId] = useState(null)
  const graph = resource.data
  useEffect(() => { setSelectedId(graph?.rootId ?? null) }, [graph])
  const selected = graph?.nodes.find((node) => node.id === selectedId) ?? null
  return <>
    <section className="view-toolbar"><div><span className="eyebrow">RELATIONSHIP INTELLIGENCE</span><h2>Follow the connections.</h2><p>Read-only networks · Verified synchronization required</p></div></section>
    <form className="graph-search-form" onSubmit={submit}>
      <div><label htmlFor="graph-transaction">Transaction ID</label><input id="graph-transaction" value={form.id} onChange={(event) => setForm({ ...form, id: event.target.value })} required maxLength={64} pattern="[A-Za-z0-9_\-]+" placeholder="Enter an exact transaction ID" autoComplete="off" /></div>
      <div><label htmlFor="graph-limit">Node limit</label><input id="graph-limit" type="number" min="1" max="100" step="1" required value={form.limit} onChange={(event) => setForm({ ...form, limit: event.target.value })} /></div>
      <button type="submit" className="primary-button" disabled={resource.status === 'loading'}><Network size={16} aria-hidden="true" />Load Graph</button>
    </form>
    {validation && <div className="graph-validation"><ErrorState message={validation} /></div>}
    {resource.status === 'idle' && <section className="panel"><EmptyState icon={Network} title="Start with a transaction" description="Enter an exact ID to load its bounded card and device relationships. No graph is loaded until a valid ID is provided." label={null} /></section>}
    {resource.status === 'loading' && <div className="panel resource-state"><LoadingState message="Loading transaction network…" /></div>}
    {resource.status === 'error' && <div className="panel resource-state"><ErrorState message={resource.error} onRetry={retry} retryLabel="Retry graph request" /></div>}
    {graph && <>
      <div className="graph-summary"><p><strong>{graph.transaction_id}</strong> · {graph.node_count} nodes · {graph.edge_count} edges</p><p className={graph.truncated === null ? 'truncation-unknown' : ''}>Truncated: {truncationMessage(graph.truncated)}</p></div>
      <div className="graph-workspace">
        <section className="panel graph-panel" aria-label="Transaction graph">
          <ul className="graph-legend" aria-label="Graph legend"><li><span className="legend-transaction" />Transaction</li><li><span className="legend-card" />Card</li><li><span className="legend-device" />Device</li><li><span className="legend-root" />Requested transaction</li></ul>
          <GraphViewer graph={graph} selectedId={selectedId} onSelect={setSelectedId} />
          <div className="graph-node-picker"><label htmlFor="graph-node">Select a node</label><select id="graph-node" value={selectedId ?? ''} onChange={(event) => setSelectedId(event.target.value || null)}><option value="">No selection</option>{graph.nodes.map((node) => <option key={node.id} value={node.id}>{node.type} · {node.label}</option>)}</select></div>
        </section>
        <GraphNodeDetails node={selected} />
      </div>
    </>}
    <p className="page-note">Arrows point from transactions to cards (USES_CARD, solid) or devices (USES_DEVICE, dashed). Node colors show entity types, not fraud risk. A connection does not prove fraud. This view has no historical cutoff and must not be used as model features. No additional graph pages are fetched.</p>
  </>
}
