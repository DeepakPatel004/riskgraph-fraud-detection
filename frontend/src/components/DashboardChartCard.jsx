import { LoadingState } from './LoadingState.jsx'
import { ErrorState } from './ErrorState.jsx'
import { EmptyState } from './EmptyState.jsx'
export function DashboardChartCard({ title, subtitle, resource, empty, emptyText, children }) {
  return <section className="panel chart-card" aria-label={title}>
    <div className="chart-heading"><h2>{title}</h2><p>{subtitle}</p></div>
    {resource.status === 'loading' ? <div className="resource-state"><LoadingState message={`Loading ${title.toLowerCase()}…`} /></div>
      : resource.status === 'error' ? <div className="resource-state"><ErrorState message={resource.error} onRetry={resource.refresh} retryLabel="Try again" /></div>
        : empty ? <EmptyState title={emptyText} description="Counts will appear when matching SQL records are available." label={null} /> : children}
  </section>
}
