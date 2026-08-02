import { useEffect, useRef, useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { asApiError, errorMessage, type ApiError } from '../../api/errors'
import { useAuth } from '../../auth/AuthProvider'
import { InlineAlert } from '../../components/InlineAlert'
import { PasswordField } from '../../components/PasswordField'

export function PasswordChangePage() {
  const auth = useAuth()
  const navigate = useNavigate()
  const headingRef = useRef<HTMLHeadingElement>(null)
  const alertRef = useRef<HTMLDivElement>(null)
  const [currentPassword, setCurrentPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [error, setError] = useState<ApiError | null>(null)
  const [mismatch, setMismatch] = useState(false)
  const [submitting, setSubmitting] = useState(false)

  useEffect(() => headingRef.current?.focus(), [])

  const clearPasswords = () => {
    setCurrentPassword('')
    setNewPassword('')
    setConfirmation('')
  }

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    setError(null)
    setMismatch(false)
    if (newPassword !== confirmation) {
      setMismatch(true)
      clearPasswords()
      queueMicrotask(() => alertRef.current?.focus())
      return
    }
    setSubmitting(true)
    try {
      await auth.changePassword(currentPassword, newPassword)
      clearPasswords()
      navigate('/inbox', { replace: true })
    } catch (unknownError) {
      clearPasswords()
      setError(asApiError(unknownError))
      queueMicrotask(() => alertRef.current?.focus())
    } finally {
      setSubmitting(false)
    }
  }

  const logout = async () => {
    try {
      await auth.logout()
    } finally {
      navigate('/login', { replace: true })
    }
  }

  return (
    <main className="centered-page">
      <section className="auth-panel auth-panel--wide" aria-labelledby="password-heading">
        <p className="eyebrow">MBB</p>
        <h1 id="password-heading" tabIndex={-1} ref={headingRef}>
          Change your password
        </h1>
        <p>You must set a new password before opening the Inbox.</p>
        {mismatch ? (
          <InlineAlert id="password-error" ref={alertRef}>The new passwords do not match.</InlineAlert>
        ) : error ? (
          <InlineAlert id="password-error" ref={alertRef} requestId={error.requestId}>
            {errorMessage(error)}
          </InlineAlert>
        ) : null}
        <div className="policy" aria-labelledby="password-policy-heading">
          <h2 id="password-policy-heading">Password policy</h2>
          <ul>
            <li>Use 14–128 Unicode characters.</li>
            <li>Do not use control characters.</li>
            <li>Do not reuse your current password.</li>
            <li>Do not include normalized variants of your username or display name.</li>
            <li>Do not use a blocked common password.</li>
          </ul>
          <p className="muted">The server makes the final policy decision.</p>
        </div>
        <form onSubmit={submit} noValidate>
          <PasswordField
            id="current-password"
            label="Current password"
            autoComplete="current-password"
            required
            aria-invalid={Boolean(error) || undefined}
            aria-describedby={error ? 'password-error' : undefined}
            value={currentPassword}
            onChange={(event) => setCurrentPassword(event.target.value)}
          />
          <PasswordField
            id="new-password"
            label="New password"
            autoComplete="new-password"
            required
            aria-invalid={mismatch || Boolean(error) || undefined}
            aria-describedby={`${mismatch || error ? 'password-error ' : ''}password-policy-heading`}
            value={newPassword}
            onChange={(event) => setNewPassword(event.target.value)}
          />
          <PasswordField
            id="confirm-password"
            label="Confirm new password"
            autoComplete="new-password"
            required
            aria-invalid={mismatch || undefined}
            aria-describedby={mismatch ? 'password-error' : undefined}
            value={confirmation}
            onChange={(event) => setConfirmation(event.target.value)}
          />
          <button className="button button--primary button--full" type="submit" disabled={submitting}>
            {submitting ? 'Changing password…' : 'Change password'}
          </button>
        </form>
        <button className="button button--text" type="button" onClick={() => void logout()}>
          Logout safely
        </button>
      </section>
    </main>
  )
}
