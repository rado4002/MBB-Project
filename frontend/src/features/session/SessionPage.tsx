import { useEffect, useMemo, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../../auth/AuthProvider'

function formatExpiry(epoch: number, formatter: Intl.DateTimeFormat) {
  return formatter.format(new Date(epoch * 1000))
}

export function SessionPage() {
  const auth = useAuth()
  const navigate = useNavigate()
  const headingRef = useRef<HTMLHeadingElement>(null)
  const formatter = useMemo(
    () => new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }),
    [],
  )
  useEffect(() => headingRef.current?.focus(), [])
  if (!auth.session) return null

  const logout = async () => {
    try {
      await auth.logout()
    } finally {
      navigate('/login', { replace: true })
    }
  }

  return (
    <>
      <header className="page-header">
        <h1 tabIndex={-1} ref={headingRef}>Session</h1>
        <p>Read-only timing information for this browser session.</p>
      </header>
      <dl className="detail-card">
        <div><dt>Idle expiry</dt><dd>{formatExpiry(auth.session.idle_expires_at_epoch, formatter)}</dd></div>
        <div><dt>Absolute expiry</dt><dd>{formatExpiry(auth.session.absolute_expires_at_epoch, formatter)}</dd></div>
        <div>
          <dt>Recent reauthentication expiry</dt>
          <dd>
            {auth.session.recent_reauthentication_expires_at_epoch
              ? formatExpiry(auth.session.recent_reauthentication_expires_at_epoch, formatter)
              : 'Not currently active'}
          </dd>
        </div>
      </dl>
      <button className="button button--secondary" type="button" onClick={() => void logout()}>
        Logout
      </button>
    </>
  )
}
