import { riskCategory } from '../utils/presentation.js'
export function RiskBadge({ transaction }) {
  const category = riskCategory(transaction)
  return <span className={`risk-badge risk-${category.toLowerCase()}`}>{category}</span>
}
