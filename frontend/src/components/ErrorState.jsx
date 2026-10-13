import { CircleAlert } from 'lucide-react'
export function ErrorState({ message, onRetry, retryLabel = 'Check connection again' }) {
  return <div className="error-state" role="alert"><div className="state-line"><CircleAlert size={20} aria-hidden="true" /><p>{message}</p></div>{onRetry && <button className="text-button" onClick={onRetry}>{retryLabel}</button>}</div>
}
