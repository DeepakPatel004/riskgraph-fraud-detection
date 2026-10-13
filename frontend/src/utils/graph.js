import { ApiError } from '../api/client.js'
import { formatAmount } from './presentation.js'

export function validTransactionId(value) {
  return typeof value === 'string' && /^[A-Za-z0-9_-]{1,64}$/.test(value)
}

export function graphRequest(id, limit = '50') {
  const transactionId = typeof id === 'string' ? id.trim() : ''
  if (!validTransactionId(transactionId)) throw new Error('Enter a transaction ID using 1–64 letters, digits, underscores or hyphens.')
  if (!/^\d+$/.test(String(limit)) || !Number.isInteger(Number(limit)) || Number(limit) < 1 || Number(limit) > 100) {
    throw new Error('Choose a whole-number node limit from 1 to 100.')
  }
  return { id: transactionId, limit: Number(limit) }
}

export function parseGraphSearch(search) {
  const params = new URLSearchParams(search)
  const id = params.get('transaction_id') || ''
  const limit = params.has('limit') ? params.get('limit') : '50'
  try {
    if (params.getAll('transaction_id').length > 1 || params.getAll('limit').length > 1) throw new Error('The graph link has duplicate parameters. Enter one transaction ID and limit.')
    if (!params.has('transaction_id')) return { id, limit, request: null, error: null }
    return { id, limit, request: graphRequest(id, limit), error: null }
  } catch (error) {
    return { id, limit, request: null, error: error.message }
  }
}
export function graphSearch(request) {
  return `?${new URLSearchParams({ transaction_id: request.id, limit: String(request.limit) })}`
}
export function transactionLookupUrl(id) {
  return `/transactions?${new URLSearchParams({ transaction_id: id })}`
}
export function parseTransactionLookup(search) {
  const params = new URLSearchParams(search)
  if (!params.has('transaction_id')) return { id: null, error: null }
  const id = params.get('transaction_id')
  if (params.getAll('transaction_id').length !== 1 || !validTransactionId(id)) {
    return { id: null, error: 'The transaction link must contain one valid exact transaction ID.' }
  }
  return { id, error: null }
}

const errorMessages = {
  GRAPH_SYNC_PENDING: 'This transaction’s graph is not ready yet. Wait for graph synchronization, then retry.',
  GRAPH_SYNC_FAILED: 'Graph synchronization failed. An operator must recover the sync job before this network is available.',
  GRAPH_SYNC_UNVERIFIED: 'Graph synchronization has not been verified. Operator reconciliation is required.',
  GRAPH_POPULATION_UNVERIFIED: 'The graph population is not verified. Real Neptune verification is still required.',
  GRAPH_POPULATION_MISMATCH: 'SQL and graph population acknowledgements do not match. Operator reconciliation is required.',
  GRAPH_CONFIGURATION_UNAVAILABLE: 'The graph service is not configured for verified access.',
  GRAPH_INCONSISTENT: 'The graph network could not be verified. An operator must inspect its synchronization state.',
  GRAPH_UNAVAILABLE: 'The graph service is temporarily unavailable. Retry after it recovers.',
}
export function graphErrorMessage(error) {
  if (!(error instanceof ApiError)) return 'Unable to load the transaction network. Please try again.'
  if (errorMessages[error.code]) return errorMessages[error.code]
  if (error.status === 404) return 'No stored transaction exists with that exact ID.'
  if (error.status === 422) return 'The graph request was rejected. Check the transaction ID and node limit.'
  return error.message // Already sanitized by the shared client or graph mapper.
}
function invalidGraph(message = 'The backend returned an unsupported or inconsistent graph response.') {
  throw new ApiError(message, { code: 'INVALID_GRAPH_RESPONSE' })
}

/** Whitelist display fields. Never forward arbitrary properties to Cytoscape. */
export function mapGraphResponse(response, request) {
  if (!response || response.transaction_id !== request.id || !Array.isArray(response.nodes) || !Array.isArray(response.edges)) invalidGraph()
  if (!response.nodes.length) invalidGraph('No usable transaction network was returned. Its graph may not be ready.')
  if (response.nodes.length > request.limit || response.nodes.length > 100 || response.edges.length > 200
    || response.node_count !== response.nodes.length || response.edge_count !== response.edges.length
    || ![null, false, true].includes(response.truncated)) invalidGraph()
  const ids = new Map()
  const nodes = response.nodes.map((node) => {
    if (!node || !['Transaction', 'Card', 'Device'].includes(node.type)
      || typeof node.id !== 'string' || !new RegExp(`^${node.type}:[a-f0-9]{64}$`).test(node.id) || ids.has(node.id)) invalidGraph()
    const properties = {}
    let label
    if (node.type === 'Transaction') {
      if (!validTransactionId(node.label)) invalidGraph()
      label = node.label
      if (node.properties?.amount !== undefined) {
        if (formatAmount(node.properties.amount) === 'Unavailable') invalidGraph()
        properties.amount = node.properties.amount
      }
      if (node.properties?.currency !== undefined) {
        if (typeof node.properties.currency !== 'string' || !/^[A-Z]{3}$/.test(node.properties.currency)) invalidGraph()
        properties.currency = node.properties.currency
      }
    } else {
      // Derive only the API's safe hash label, ignoring even an unexpected raw label.
      label = `${node.type} ${node.id.split(':')[1].slice(0, 8)}`
    }
    const safe = { id: node.id, type: node.type, label, properties }
    ids.set(node.id, safe)
    return safe
  })
  const roots = nodes.filter((node) => node.type === 'Transaction' && node.label === request.id)
  if (roots.length !== 1) invalidGraph()
  const edgeKeys = new Set()
  const edges = response.edges.map((edge, index) => {
    const source = ids.get(edge?.source)
    const target = ids.get(edge?.target)
    const targetType = { USES_CARD: 'Card', USES_DEVICE: 'Device' }[edge?.type]
    const key = JSON.stringify([edge?.source, edge?.type, edge?.target])
    if (!source || !target || source.type !== 'Transaction' || !targetType || target.type !== targetType || edgeKeys.has(key)) invalidGraph()
    edgeKeys.add(key)
    return { id: `edge:${index}`, source: source.id, target: target.id, type: edge.type }
  })
  return { transaction_id: request.id, nodes, edges, rootId: roots[0].id,
    node_count: response.node_count, edge_count: response.edge_count, truncated: response.truncated }
}
export function graphElements(graph) {
  return [
    ...graph.nodes.map((node) => ({ data: { id: node.id, type: node.type, label: node.label }, classes: node.id === graph.rootId ? 'root' : '' })),
    ...graph.edges.map((edge) => ({ data: { ...edge } })),
  ]
}
export function truncationMessage(value) {
  if (value === null) return 'null · Unknown — node limit reached; more nodes may exist.'
  if (value === false) return 'false · No truncation reported for the visible network.'
  return 'true · Truncation reported; only a bounded network is shown.'
}

/** Colors describe entity types, never fraud scores or classifications. */
export const graphStyles = [
  { selector: 'node', style: { label: 'data(label)', 'font-size': 11, 'font-family': 'Segoe UI, Arial, sans-serif', color: '#334b65', 'text-valign': 'bottom', 'text-margin-y': 9, 'text-wrap': 'ellipsis', 'text-max-width': 130, width: 38, height: 38, 'border-width': 2, 'border-color': '#ffffff' } },
  { selector: 'node[type = "Transaction"]', style: { shape: 'round-rectangle', 'background-color': '#527fa6' } },
  { selector: 'node[type = "Card"]', style: { shape: 'ellipse', 'background-color': '#3c9c88' } },
  { selector: 'node[type = "Device"]', style: { shape: 'diamond', 'background-color': '#9680ba', width: 44, height: 44 } },
  { selector: 'node.root', style: { 'background-color': '#172b43', width: 48, height: 48, 'font-weight': 'bold', 'border-color': '#77d4c1', 'border-width': 4 } },
  { selector: 'node:selected', style: { 'border-color': '#163f36', 'border-width': 5, 'overlay-opacity': 0 } },
  { selector: 'edge', style: { width: 1.5, 'line-color': '#b4c4d4', 'target-arrow-color': '#94a9be', 'target-arrow-shape': 'triangle', 'curve-style': 'bezier', 'arrow-scale': .8 } },
  { selector: 'edge[type = "USES_DEVICE"]', style: { 'line-style': 'dashed' } },
]
