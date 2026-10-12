import { ShieldCheck, LockKeyhole } from 'lucide-react'
import { NavLink } from 'react-router-dom'
import { navigation } from '../navigation.js'

export function Sidebar() {
  return (
    <aside className="sidebar">
      <NavLink to="/" className="brand" aria-label="RiskGraph overview">
        <span className="brand-mark"><ShieldCheck size={25} aria-hidden="true" /></span>
        <span><strong>RiskGraph</strong><small>Fraud intelligence</small></span>
      </NavLink>
      <p className="nav-label">WORKSPACE</p>
      <nav aria-label="Main navigation">
        {navigation.map(({ path, title, icon: Icon }) => (
          <NavLink key={path} to={path} end={path === '/'}
            className={({ isActive }) => `nav-item${isActive ? ' active' : ''}`}>
            <Icon size={19} aria-hidden="true" /><span>{title}</span>
          </NavLink>
        ))}
      </nav>
      <div className="sidebar-footer">
        <LockKeyhole size={17} aria-hidden="true" />
        <div><strong>Local workspace</strong><small>Restricted development demo</small></div>
      </div>
    </aside>
  )
}
