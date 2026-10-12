import { ShieldCheck } from 'lucide-react'
import { PlaceholderPage } from './PlaceholderPage.jsx'
export function Investigations() {
  return <PlaceholderPage icon={ShieldCheck} title="Investigation workspace" description="A focused place for reviewing alerts and investigation status." note="No alerts are loaded and no investigation statuses are changed here. A risk score is a model assessment, not proof of fraud." />
}
