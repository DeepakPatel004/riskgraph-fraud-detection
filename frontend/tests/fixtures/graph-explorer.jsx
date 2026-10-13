/** Isolated browser fixture. No API, database, AWS or scoring calls occur here. */
import { useCallback, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Routes, Route, useLocation } from 'react-router-dom'
import { GraphExplorer } from '../../src/pages/GraphExplorer.jsx'
import { Sidebar } from '../../src/components/Sidebar.jsx'
import { ApiError } from '../../src/api/client.js'
import { graphFixture, largeGraphFixture } from './graphData.js'
import '../../src/styles.css'

function Fixture() {
  const [requests, setRequests] = useState([])
  const location = useLocation()
  const requestGraph = useCallback(async (id, { limit }, { signal }) => {
    setRequests((items) => [...items, `${id}:${limit}`])
    await new Promise((resolve, reject) => {
      const timer = setTimeout(resolve, id === 'TEST_SLOW' ? 1500 : 80)
      signal.addEventListener('abort', () => { clearTimeout(timer); reject(new Error('aborted')) }, { once: true })
    })
    if (id === 'PENDING') throw new ApiError('Sanitized backend message', { status: 503, code: 'GRAPH_SYNC_PENDING' })
    if (id === 'UNKNOWN') throw new ApiError('Sanitized backend message', { status: 404 })
    if (id === 'UNAVAILABLE') throw new ApiError('Sanitized backend message', { status: 503, code: 'GRAPH_UNAVAILABLE' })
    return id === 'TEST_100_NODES' ? largeGraphFixture(limit) : graphFixture(id, limit)
  }, [])
  return <><Sidebar /><div className="workspace">
    <header className="header"><div><p className="breadcrumb">Isolated frontend test</p><h1>Graph Explorer</h1></div><strong className="phase-label">SYNTHETIC FIXTURE · NO BACKEND CALLS</strong></header>
    <main><Routes><Route path="/transactions" element={<p>Fixture transaction handoff: {location.search}</p>} /><Route path="*" element={<GraphExplorer requestGraph={requestGraph} />} /></Routes>
      <p className="page-note">Fixture requests: <output id="fixture-requests">{requests.length} — {requests.join(', ')}</output></p>
    </main></div></>
}
const root = createRoot(document.getElementById('root'))
root.render(<BrowserRouter><Fixture /></BrowserRouter>)
if (import.meta.hot) import.meta.hot.dispose(() => root.unmount())
