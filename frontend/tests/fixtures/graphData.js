/** Synthetic frontend-only contract fixture. Never imported by production code. */
export function graphFixture(transactionId = 'TEST_101', limit = 50) {
  const t1 = `Transaction:${'1'.repeat(64)}`
  const t2 = `Transaction:${'2'.repeat(64)}`
  const t3 = `Transaction:${'3'.repeat(64)}`
  const c1 = `Card:${'a'.repeat(64)}`
  const c2 = `Card:${'b'.repeat(64)}`
  const d1 = `Device:${'c'.repeat(64)}`
  const d2 = `Device:${'d'.repeat(64)}`
  const nodes = [
    { id: t1, type: 'Transaction', label: transactionId, properties: { amount: '150.00', currency: 'USD' } },
    { id: c1, type: 'Card', label: 'Card aaaaaaaa', properties: {} },
    { id: d1, type: 'Device', label: 'Device cccccccc', properties: {} },
    { id: t2, type: 'Transaction', label: 'TEST_RELATED_1', properties: { amount: '80.50', currency: 'USD' } },
    { id: t3, type: 'Transaction', label: 'TEST_RELATED_2', properties: { amount: '42.00', currency: 'USD' } },
    { id: c2, type: 'Card', label: 'Card bbbbbbbb', properties: {} },
    { id: d2, type: 'Device', label: 'Device dddddddd', properties: {} },
  ].slice(0, limit)
  const ids = new Set(nodes.map((node) => node.id))
  const edges = [
    { source: t1, target: c1, type: 'USES_CARD' }, { source: t1, target: d1, type: 'USES_DEVICE' },
    { source: t2, target: c1, type: 'USES_CARD' }, { source: t2, target: d2, type: 'USES_DEVICE' },
    { source: t3, target: c2, type: 'USES_CARD' }, { source: t3, target: d1, type: 'USES_DEVICE' },
  ].filter((edge) => ids.has(edge.source) && ids.has(edge.target))
  return { transaction_id: transactionId, nodes, edges, node_count: nodes.length,
    edge_count: edges.length, truncated: nodes.length === limit ? null : false }
}

/** A 100-node connected ring: every transaction has one card and one device. */
export function largeGraphFixture(limit = 100) {
  const id = (type, index) => `${type}:${(index + 1).toString(16).padStart(2, '0').repeat(32)}`
  const nodes = [], edges = []
  for (let i = 0; i < 50; i++) {
    nodes.push({ id: id('Transaction', i), type: 'Transaction', label: i === 0 ? 'TEST_100_NODES' : `TEST_NODE_${i}`, properties: { amount: '10.00', currency: 'USD' } })
    edges.push({ source: id('Transaction', i), target: id('Card', Math.floor(i / 2)), type: 'USES_CARD' },
      { source: id('Transaction', i), target: id('Device', Math.ceil(i / 2) % 25), type: 'USES_DEVICE' })
  }
  for (const type of ['Card', 'Device']) for (let i = 0; i < 25; i++) {
    const opaque = id(type, i)
    nodes.push({ id: opaque, type, label: `${type} ${opaque.split(':')[1].slice(0, 8)}`, properties: {} })
  }
  const visited = new Set([nodes[0].id]), queue = [nodes[0].id], ordered = []
  while (queue.length && ordered.length < limit) {
    const current = queue.shift()
    ordered.push(nodes.find((node) => node.id === current))
    for (const edge of edges) {
      const neighbor = edge.source === current ? edge.target : edge.target === current ? edge.source : null
      if (neighbor && !visited.has(neighbor)) { visited.add(neighbor); queue.push(neighbor) }
    }
  }
  const included = new Set(ordered.map((node) => node.id))
  const shownEdges = edges.filter((edge) => included.has(edge.source) && included.has(edge.target))
  return { transaction_id: 'TEST_100_NODES', nodes: ordered, edges: shownEdges,
    node_count: ordered.length, edge_count: shownEdges.length, truncated: ordered.length === limit ? null : false }
}
