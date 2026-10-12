import { ArrowUpRight, Activity } from 'lucide-react'
import { Link, useOutletContext } from 'react-router-dom'
import { navigation } from '../navigation.js'
import { EmptyState } from '../components/EmptyState.jsx'
import { LoadingState } from '../components/LoadingState.jsx'
import { ErrorState } from '../components/ErrorState.jsx'

const descriptions = ['Explore transaction records and risk assessments.', 'Follow connections between transactions, cards and devices.', 'Review alerts and organize investigation activity.']
export function Overview() {
  const { connection, checkConnection } = useOutletContext()
  return (
    <>
      <section className="intro">
        <span className="eyebrow">CONNECTED CONTEXT. CLEARER DECISIONS.</span>
        <h2>Your fraud investigation workspace.</h2>
        <p>A foundation for exploring transaction risk and the relationships behind it.</p>
      </section>
      <div className="section-heading"><h2>Explore the workspace</h2><span>Three connected perspectives</span></div>
      <div className="workspace-cards">
        {navigation.slice(1).map(({ path, title, icon: Icon }, index) => (
          <Link className="workspace-card" to={path} key={path}>
            <div className="card-top">
              <span className="card-icon"><Icon size={22} aria-hidden="true" /></span>
              <ArrowUpRight size={18} aria-hidden="true" />
            </div>
            <h3>{title}</h3><p>{descriptions[index]}</p>
            <span className="card-link">View workspace <span aria-hidden="true">→</span></span>
          </Link>
        ))}
      </div>
      <section className="panel connection-panel" aria-label="Backend connectivity">
        <div className="section-heading">
          <h2><Activity size={18} aria-hidden="true" /> Backend connection</h2>
          <span>Live health check</span>
        </div>
        {connection.status === 'checking' ? <LoadingState />
          : connection.status === 'disconnected' ? <ErrorState message={connection.message} onRetry={checkConnection} />
            : <p className="connected-message">{connection.message}</p>}
        <p className="muted-note">Health checks SQL connectivity. It does not verify Neptune, graph freshness, or model readiness.</p>
      </section>
      <section className="panel">
        <EmptyState title="The foundation is ready" description="Analytics and data views will arrive in later phases. No dashboard metrics or transaction data are loaded here yet." />
      </section>
    </>
  )
}
