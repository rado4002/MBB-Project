import { useEffect } from 'react'
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { useAuth } from '../auth/AuthProvider'
import { AccountMenu } from './AccountMenu'

const pageNames: Record<string, string> = {
  '/inbox': 'Inbox',
  '/account': 'My Account',
  '/session': 'Session',
}

export function ApplicationShell() {
  const auth = useAuth()
  const location = useLocation()
  const navigate = useNavigate()
  const pageName = location.pathname.startsWith('/inbox/')
    ? 'Inbox'
    : (pageNames[location.pathname] ?? 'MBB')

  useEffect(() => {
    document.title = `${pageName} · MBB`
  }, [pageName])

  if (!auth.session) return null

  const logout = async () => {
    try {
      await auth.logout()
      navigate('/login', { replace: true })
    } catch {
      navigate('/login', { replace: true })
    }
  }

  return (
    <div className="app-frame">
      <header className="app-header">
        <a className="brand" href="#main-content" aria-label="MBB, skip to content">
          MBB
        </a>
        <nav aria-label="Primary navigation">
          <NavLink to="/inbox" className={({ isActive }) => (isActive ? 'active' : undefined)}>
            Inbox
          </NavLink>
        </nav>
        <AccountMenu human={auth.session.human} onLogout={logout} />
      </header>
      <main id="main-content" className="app-main" tabIndex={-1}>
        <Outlet />
      </main>
    </div>
  )
}
