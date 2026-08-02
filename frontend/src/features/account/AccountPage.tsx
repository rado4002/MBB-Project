import { useEffect, useRef } from 'react'
import { useAuth } from '../../auth/AuthProvider'

export function AccountPage() {
  const { session } = useAuth()
  const headingRef = useRef<HTMLHeadingElement>(null)
  useEffect(() => headingRef.current?.focus(), [])
  if (!session) return null

  return (
    <>
      <header className="page-header">
        <h1 tabIndex={-1} ref={headingRef}>My Account</h1>
        <p>Read-only account information.</p>
      </header>
      <dl className="detail-card">
        <div><dt>Display name</dt><dd>{session.human.display_name}</dd></div>
        <div><dt>Username</dt><dd>{session.human.username}</dd></div>
        <div><dt>Role</dt><dd>{session.human.role.charAt(0).toUpperCase() + session.human.role.slice(1)}</dd></div>
      </dl>
    </>
  )
}
