export function StatusBadge({ status }) {
  const labels = { connected: 'Connected', disconnected: 'Disconnected', checking: 'Checking' }
  return <span className={`status-badge ${status}`} role="status"><span className="status-dot" />{labels[status]}</span>
}
