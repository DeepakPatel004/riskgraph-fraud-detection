import { BrowserRouter, Link, Route, Routes } from 'react-router-dom'
import { PageLayout } from './components/PageLayout.jsx'
import { EmptyState } from './components/EmptyState.jsx'
import { Overview } from './pages/Overview.jsx'
import { Transactions } from './pages/Transactions.jsx'
import { GraphExplorer } from './pages/GraphExplorer.jsx'
import { Investigations } from './pages/Investigations.jsx'

export function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<PageLayout />}>
          <Route index element={<Overview />} />
          <Route path="transactions" element={<Transactions />} />
          <Route path="graph-explorer" element={<GraphExplorer />} />
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
