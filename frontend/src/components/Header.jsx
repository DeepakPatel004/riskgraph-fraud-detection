import { RefreshCw } from 'lucide-react'
import { StatusBadge } from './StatusBadge.jsx'

export function Header({ title, connection, onRetry }) {
  return (
    <header className="header">
      <div><p className="breadcrumb">Workspace <span>/</span> {title}</p><h1>{title}</h1></div>
      <div className="connection-controls">
        <span className="connection-label">Backend</span>
        <StatusBadge status={connection.status} />
        <button className="icon-button" aria-label="Refresh backend connection"
          title="Refresh backend connection" onClick={onRetry}
          disabled={connection.status === 'checking'}>
          <RefreshCw size={16} aria-hidden="true" />
        </button>
      </div>
    </header>
  )
}
