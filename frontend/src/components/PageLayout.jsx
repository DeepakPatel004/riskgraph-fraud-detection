import { useEffect } from 'react'
import { Outlet, useLocation } from 'react-router-dom'
import { navigation } from '../navigation.js'
import { useBackendConnection } from '../hooks/useBackendConnection.js'
import { Sidebar } from './Sidebar.jsx'
import { Header } from './Header.jsx'

export function PageLayout() {
  const { pathname } = useLocation()
  const title = navigation.find((item) => item.path === pathname)?.title || 'Page not found'
  const backend = useBackendConnection()
  useEffect(() => { document.title = `RiskGraph · ${title}` }, [title])
  return (
    <>
      <a className="skip-link" href="#main-content">Skip to content</a>
      <Sidebar />
      <div className="workspace">
        <Header title={title} connection={backend.connection} onRetry={backend.checkConnection} />
        <main id="main-content" tabIndex={-1}><Outlet context={backend} /></main>
        <footer className="workspace-footer">
          <span>RiskGraph / Development workspace</span>
          <span>No authentication · Local access only</span>
        </footer>
      </div>
    </>
  )
}
