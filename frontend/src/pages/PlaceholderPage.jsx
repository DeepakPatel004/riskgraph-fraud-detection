import { EmptyState } from '../components/EmptyState.jsx'
export function PlaceholderPage({ icon, title, description, note }) {
  return <><section className="page-intro"><span className="eyebrow">WORKSPACE PREVIEW</span><p>{description}</p></section><section className="panel"><EmptyState icon={icon} title={title} description="This page is a navigation placeholder. Its data view and controls will be implemented in a later phase." /></section><p className="page-note">{note}</p></>
}
