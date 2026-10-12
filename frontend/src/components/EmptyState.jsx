import { Layers3 } from 'lucide-react'
export function EmptyState({ title, description, icon: Icon = Layers3 }) {
  return <div className="empty-state"><span className="empty-icon"><Icon size={28} strokeWidth={1.5} aria-hidden="true" /></span><h2>{title}</h2><p>{description}</p><span className="phase-label">Phase 6.1 · Application foundation</span></div>
}
