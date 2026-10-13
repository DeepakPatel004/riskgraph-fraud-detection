import { Layers3 } from 'lucide-react'
export function EmptyState({ title, description, icon: Icon = Layers3, label = 'Phase 6.1 · Application foundation' }) {
  return <div className="empty-state"><span className="empty-icon"><Icon size={28} strokeWidth={1.5} aria-hidden="true" /></span><h2>{title}</h2><p>{description}</p>{label && <span className="phase-label">{label}</span>}</div>
}
