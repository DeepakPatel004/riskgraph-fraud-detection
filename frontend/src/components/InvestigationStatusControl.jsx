import { useEffect, useState } from 'react'
import { INVESTIGATION_STATUSES, statusUpdateAction } from '../utils/investigations.js'
export function InvestigationStatusControl({ alert, pending, onUpdate }) {
  const [next, setNext] = useState(alert.investigation_status)
  const [confirm, setConfirm] = useState(false)
  useEffect(() => { setNext(alert.investigation_status); setConfirm(false) }, [alert.alert_id, alert.investigation_status])
  function submit(event) {
    event.preventDefault()
    const action = statusUpdateAction(alert.investigation_status, next)
    if (action === 'confirm') setConfirm(true)
    else if (action === 'save') onUpdate(next)
  }
  return <div className="investigation-status-control">
    <form onSubmit={submit}><label htmlFor="investigation-next-status">Update investigation status</label>
      <div><select id="investigation-next-status" value={next} disabled={pending || confirm} onChange={(event) => setNext(event.target.value)}>{INVESTIGATION_STATUSES.map((status) => <option key={status}>{status}</option>)}</select>
        <button className="primary-button" type="submit" disabled={pending || confirm || next === alert.investigation_status}>{pending ? 'Saving status…' : 'Update status'}</button></div>
    </form>
    {confirm && <div className="close-confirmation" role="group" aria-label="Confirm closing investigation">
      <p>Close this investigation? CLOSED means review is finished; it does not confirm fraud. You can reopen it later.</p>
      <div><button className="primary-button" disabled={pending} onClick={() => { setConfirm(false); onUpdate('CLOSED', true) }}>Confirm close</button><button className="secondary-button" disabled={pending} onClick={() => setConfirm(false)}>Cancel closing</button></div>
    </div>}
  </div>
}
