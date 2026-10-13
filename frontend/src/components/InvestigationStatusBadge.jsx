export function InvestigationStatusBadge({ status }) {
  return <span className={`investigation-status investigation-${status.toLowerCase()}`}>{status}</span>
}
