import { ArrowLeftRight } from 'lucide-react'
import { PlaceholderPage } from './PlaceholderPage.jsx'
export function Transactions() {
  return <PlaceholderPage icon={ArrowLeftRight} title="Transaction workspace" description="A dedicated view for transaction records and saved risk assessments." note="No transactions are requested or submitted from this page. Scoring will continue to require the backend's graph readiness checks." />
}
