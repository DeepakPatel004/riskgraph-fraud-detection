/** Isolated UI fixture: status changes affect only this page's in-memory records. */
import { useMemo, useState } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Routes, Route, useLocation } from 'react-router-dom'
import { Investigations } from '../../src/pages/Investigations.jsx'
import { Sidebar } from '../../src/components/Sidebar.jsx'
import { createInvestigationFixture } from './investigationData.js'
import '../../src/styles.css'

function Fixture() {
  const [calls, setCalls] = useState([])
  const location = useLocation()
  const fixture = useMemo(() => createInvestigationFixture({ empty: new URLSearchParams(location.search).get('fixture_empty') === '1',
    readDelay: 80, writeDelay: 400, onRequest: setCalls }), [])
  return <><Sidebar /><div className="workspace"><header className="header"><div><p className="breadcrumb">Isolated frontend test</p><h1>Investigations</h1></div><strong className="phase-label">SYNTHETIC FIXTURE · NO BACKEND CALLS</strong></header><main>
    <Routes><Route path="/transactions" element={<p>Fixture transaction handoff: {location.search}</p>} /><Route path="/graph-explorer" element={<p>Fixture graph handoff: {location.search}</p>} /><Route path="*" element={<Investigations alertsApi={fixture.api} />} /></Routes>
    <p className="page-note">Fixture requests: <output id="fixture-investigation-calls">{calls.length} — {calls.join(', ')}</output></p>
  </main></div></>
}
const root = createRoot(document.getElementById('root'))
root.render(<BrowserRouter><Fixture /></BrowserRouter>)
if (import.meta.hot) import.meta.hot.dispose(() => root.unmount())
