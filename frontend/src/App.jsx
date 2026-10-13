import { BrowserRouter, Link, Route, Routes } from 'react-router-dom'
import { lazy, Suspense } from 'react'
import { PageLayout } from './components/PageLayout.jsx'
import { EmptyState } from './components/EmptyState.jsx'
import { LoadingState } from './components/LoadingState.jsx'
import { Transactions } from './pages/Transactions.jsx'
import { Investigations } from './pages/Investigations.jsx'

// Keep chart code out of the initial transaction/placeholder page bundle.
const Overview = lazy(() => import('./pages/Overview.jsx').then((module) => ({ default: module.Overview })))
const GraphExplorer = lazy(() => import('./pages/GraphExplorer.jsx').then((module) => ({ default: module.GraphExplorer })))

export function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<PageLayout />}>
          <Route index element={<Suspense fallback={<LoadingState message="Loading dashboard…" />}><Overview /></Suspense>} />
          <Route path="transactions" element={<Transactions />} />
          <Route path="graph-explorer" element={<Suspense fallback={<LoadingState message="Loading graph explorer…" />}><GraphExplorer /></Suspense>} />
          <Route path="investigations" element={<Investigations />} />
          <Route path="*" element={
            <section className="panel">
              <EmptyState title="Page not found" description="Choose a workspace from the navigation to continue." />
              <Link className="back-link" to="/">Return to overview</Link>
            </section>
          } />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}
