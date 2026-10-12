import { LoaderCircle } from 'lucide-react'
export function LoadingState({ message = 'Checking backend connection…' }) {
  return <div className="state-line" role="status"><LoaderCircle className="spin" size={20} aria-hidden="true" /><p>{message}</p></div>
}
