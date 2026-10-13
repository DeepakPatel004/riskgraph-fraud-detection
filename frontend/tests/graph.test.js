import test from 'node:test'
import assert from 'node:assert/strict'
import cytoscape from 'cytoscape'
import { ApiError, createApiClient } from '../src/api/client.js'
import { createRiskGraphApi } from '../src/api/endpoints.js'
import { readResource } from '../src/hooks/readResource.js'
import { graphRequest, parseGraphSearch, graphSearch, transactionLookupUrl, parseTransactionLookup,
  mapGraphResponse, graphElements, graphStyles, truncationMessage, graphErrorMessage } from '../src/utils/graph.js'
import { graphFixture, largeGraphFixture } from './fixtures/graphData.js'

const request = { id: 'TEST_101', limit: 50 }
test('graph mapping preserves opaque IDs, safe node types, amounts and counts', () => {
  const raw = graphFixture()
  const graph = mapGraphResponse(raw, request)
  assert.equal(graph.rootId, raw.nodes[0].id)
  assert.equal(graph.nodes[0].properties.amount, '150.00')
  assert.equal(graph.nodes[0].properties.currency, 'USD')
  assert.equal(graph.node_count, 7)
  assert.equal(graph.edge_count, 6)
  assert.deepEqual(new Set(graph.nodes.map((node) => node.type)), new Set(['Transaction', 'Card', 'Device']))
})
test('Cytoscape elements preserve directed relationships with unique edge IDs', () => {
  const graph = mapGraphResponse(graphFixture(), request)
  const elements = graphElements(graph)
  assert.equal(elements.length, 13)
  assert.equal(elements[0].classes, 'root')
  assert.equal(elements[7].data.source, graph.rootId)
  assert.equal(elements[7].data.type, 'USES_CARD')
  assert.equal(elements[8].data.type, 'USES_DEVICE')
  assert.equal(new Set(elements.map((item) => item.data.id)).size, 13)
})
test('actual Cytoscape styling distinguishes entity types, root and selection', () => {
  const graph = mapGraphResponse(graphFixture(), request)
  const cy = cytoscape({ headless: true, styleEnabled: true, layout: { name: 'preset' }, elements: graphElements(graph), style: graphStyles })
  try {
    const root = cy.getElementById(graph.rootId)
    assert.equal(root.style('shape'), 'round-rectangle')
    assert.equal(cy.nodes('[type="Card"]').first().style('shape'), 'ellipse')
    assert.equal(cy.nodes('[type="Device"]').first().style('shape'), 'diamond')
    assert.notEqual(root.style('background-color'), cy.nodes('[type="Card"]').first().style('background-color'))
    assert.equal(parseFloat(root.style('border-width')), 4)
    root.select()
    assert.equal(parseFloat(root.style('border-width')), 5)
    assert.equal(cy.edges().first().style('target-arrow-shape'), 'triangle')
    assert.equal(cy.edges('[type="USES_DEVICE"]').first().style('line-style'), 'dashed')
  } finally { cy.destroy() }
  assert.equal(cy.destroyed(), true)
})
test('unknown node types and raw source IDs are rejected safely', () => {
  for (const mutation of [(graph) => { graph.nodes[1].type = 'Account' }, (graph) => { graph.nodes[1].id = 'PRIVATE_CARD_TOKEN' }]) {
    const raw = graphFixture()
    mutation(raw)
    assert.throws(() => mapGraphResponse(raw, request), (error) => error.code === 'INVALID_GRAPH_RESPONSE' && !error.message.includes('PRIVATE'))
  }
})
test('unexpected properties and token labels never reach elements or node details', () => {
  const raw = graphFixture()
  raw.nodes[1].label = 'PRIVATE_CARD_TOKEN'
  raw.nodes[1].properties = { card_token: 'PRIVATE_CARD_TOKEN', amount: '999.00' }
  raw.nodes[2].properties = { device_token: 'PRIVATE_DEVICE_TOKEN' }
  raw.nodes[0].properties.card_token = 'PRIVATE_CARD_TOKEN'
  raw.nodes[0].properties.risk_score = .9
  const graph = mapGraphResponse(raw, request)
  assert.equal(graph.nodes[1].label, 'Card aaaaaaaa')
  assert.deepEqual(graph.nodes[1].properties, {})
  assert.ok(!JSON.stringify(graph).includes('PRIVATE'))
  assert.ok(!JSON.stringify(graphElements(graph)).includes('risk_score'))
})
test('missing optional transaction properties stay missing without fabrication', () => {
  const raw = graphFixture()
  raw.nodes[0].properties = {}
  assert.deepEqual(mapGraphResponse(raw, request).nodes[0].properties, {})
})
test('invalid edges, duplicate IDs and mismatched counts are rejected', () => {
  const mutations = [
    (graph) => { graph.edges[0].target = 'missing' },
    (graph) => { graph.edges[0].type = 'UNKNOWN' },
    (graph) => { graph.edges[0].type = 'USES_DEVICE' },
    (graph) => { graph.nodes[1].id = graph.nodes[0].id },
    (graph) => { graph.node_count = 999 },
    (graph) => { graph.edge_count = 999 },
    (graph) => { graph.transaction_id = 'WRONG' },
    (graph) => { graph.nodes[0].label = 'WRONG' },
  ]
  for (const mutate of mutations) {
    const raw = graphFixture(); mutate(raw)
    assert.throws(() => mapGraphResponse(raw, request), { code: 'INVALID_GRAPH_RESPONSE' })
  }
})
test('node cap and null truncation retain their exact meaning, including limit=1', () => {
  const one = mapGraphResponse(graphFixture('TEST_101', 1), { id: 'TEST_101', limit: 1 })
  assert.equal(one.node_count, 1)
  assert.equal(one.edge_count, 0)
  assert.equal(one.truncated, null)
  assert.match(truncationMessage(null), /Unknown.*more nodes may exist/)
  assert.match(truncationMessage(false), /false.*No truncation reported/)
  assert.match(truncationMessage(true), /true.*bounded/)
  assert.throws(() => mapGraphResponse(graphFixture(), { id: 'TEST_101', limit: 1 }), { code: 'INVALID_GRAPH_RESPONSE' })
})
test('the maximum 100-node fixture maps without fetching another page', () => {
  const graph = mapGraphResponse(largeGraphFixture(), { id: 'TEST_100_NODES', limit: 100 })
  assert.equal(graph.node_count, 100)
  assert.equal(graph.edge_count, 100)
  assert.equal(graph.truncated, null)
  assert.equal(graphElements(graph).length, 200)
})
test('empty, missing or malformed network responses do not become successful graphs', () => {
  for (const response of [null, {}, { ...graphFixture(), nodes: [], node_count: 0, edges: [], edge_count: 0 },
    { ...graphFixture(), truncated: 'false' }]) {
    assert.throws(() => mapGraphResponse(response, request), { code: 'INVALID_GRAPH_RESPONSE' })
  }
})
test('ID and node limit validation accepts only the supported bounds', () => {
  assert.deepEqual(graphRequest(' TEST_101 ', '1'), { id: 'TEST_101', limit: 1 })
  assert.equal(graphRequest('TEST_101', 100).limit, 100)
  for (const limit of [0, 101, -1, 1.5, '2e1', '', null, true]) assert.throws(() => graphRequest('TEST_101', limit))
  for (const id of ['', 'bad/id', 'a'.repeat(65), 'id?token=x']) assert.throws(() => graphRequest(id))
})
test('deep-link parsing and successful search URLs preserve ID and limit', () => {
  assert.deepEqual(parseGraphSearch('?transaction_id=TEST_101').request, request)
  const capped = { id: 'TEST_101', limit: 7 }
  assert.deepEqual(parseGraphSearch(graphSearch(capped)).request, capped)
  assert.equal(parseGraphSearch('').request, null)
  for (const search of ['?transaction_id=', '?transaction_id=bad%2Fid', '?transaction_id=TEST_101&limit=0',
    '?transaction_id=TEST_101&transaction_id=OTHER', '?transaction_id=TEST_101&limit=1&limit=2']) {
    assert.ok(parseGraphSearch(search).error)
    assert.equal(parseGraphSearch(search).request, null)
  }
})
test('transaction detail links support safe exact lookup and reject ambiguous links', () => {
  assert.deepEqual(parseTransactionLookup(transactionLookupUrl('TEST_101').split('?')[1]), { id: 'TEST_101', error: null })
  assert.equal(new URL(transactionLookupUrl('id /?&'), 'http://localhost').searchParams.get('transaction_id'), 'id /?&')
  assert.equal(parseTransactionLookup('').id, null)
  assert.ok(parseTransactionLookup('?transaction_id=bad%2Fid').error)
  assert.ok(parseTransactionLookup('?transaction_id=ONE&transaction_id=TWO').error)
})
test('graph availability errors use static actionable messages without raw details', () => {
  assert.match(graphErrorMessage(new ApiError('PRIVATE', { code: 'GRAPH_SYNC_PENDING', status: 503 })), /not ready yet/)
  for (const code of ['GRAPH_UNAVAILABLE', 'GRAPH_SYNC_FAILED', 'GRAPH_SYNC_UNVERIFIED',
    'GRAPH_POPULATION_UNVERIFIED', 'GRAPH_POPULATION_MISMATCH', 'GRAPH_CONFIGURATION_UNAVAILABLE', 'GRAPH_INCONSISTENT']) {
    assert.ok(!graphErrorMessage(new ApiError('PRIVATE', { code, status: 503 })).includes('PRIVATE'))
  }
  assert.match(graphErrorMessage(new ApiError('PRIVATE', { status: 404 })), /exact ID/)
  assert.match(graphErrorMessage(new ApiError('PRIVATE', { status: 422 })), /node limit/)
  assert.ok(!graphErrorMessage(new Error('PRIVATE')).includes('PRIVATE'))
})
test('a valid deep link uses the existing graph API contract exactly once', async () => {
  let count = 0
  const parsed = parseGraphSearch('?transaction_id=TEST_101&limit=7')
  const api = createRiskGraphApi({ fetchImpl: async (url, options) => {
    count++
    assert.equal(url.pathname, '/backend/api/v1/transactions/TEST_101/graph')
    assert.equal(url.search, '?limit=7')
    assert.equal(options.method, 'GET')
    return new Response(JSON.stringify(graphFixture('TEST_101', 7)))
  } })
  const graph = mapGraphResponse(await api.transactions.graph(parsed.request.id, { limit: parsed.request.limit }), parsed.request)
  assert.equal(count, 1)
  assert.equal(graph.truncated, null)
})
test('graph request cancellation suppresses stale network updates', async () => {
  const controller = new AbortController()
  const requestApi = createApiClient({ fetchImpl: (_, { signal }) => new Promise((resolve, reject) => {
    signal.addEventListener('abort', () => reject(new Error('aborted')), { once: true })
  }) })
  const pending = readResource((signal) => requestApi('/api/v1/transactions/TEST_101/graph', { signal }),
    controller.signal, () => assert.fail('cancelled result must not publish'))
  controller.abort()
  await pending
})
