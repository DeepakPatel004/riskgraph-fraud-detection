import { ChevronLeft, ChevronRight } from 'lucide-react'
import { pagination, formatCount } from '../utils/presentation.js'
export function PaginationControls({ page, onPage, disabled = false }) {
  const state = pagination(page)
  return <div className="pagination-controls">
    <p aria-live="polite">{formatCount(state.start)}–{formatCount(state.end)} of {formatCount(page.total)} records</p>
    <div><button className="secondary-button" disabled={disabled || !state.hasPrevious} onClick={() => onPage(state.previous)}><ChevronLeft size={15} aria-hidden="true" />Previous</button><button className="secondary-button" disabled={disabled || !state.hasNext} onClick={() => onPage(state.next)}>Next<ChevronRight size={15} aria-hidden="true" /></button></div>
  </div>
}
