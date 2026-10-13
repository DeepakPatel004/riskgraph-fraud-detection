import { formatCount } from '../utils/presentation.js'
export function MetricCard({ name, value, note, tone, icon: Icon }) {
  return <article className={`metric-card ${tone}`}>
    <div className="metric-heading"><h3>{name}</h3><Icon size={18} aria-hidden="true" /></div>
    <p className="metric-value">{formatCount(value)}</p><p className="metric-note">{note}</p>
  </article>
}
