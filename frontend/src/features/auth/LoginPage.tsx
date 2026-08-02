import { useEffect, useRef, useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { asApiError, errorMessage, type ApiError } from '../../api/errors'
import { useAuth } from '../../auth/AuthProvider'
import { InlineAlert } from '../../components/InlineAlert'
import { PasswordField } from '../../components/PasswordField'

export function LoginPage() {
  const auth = useAuth()
  const navigate = useNavigate()
  const headingRef = useRef<HTMLHeadingElement>(null)
  const alertRef = useRef<HTMLDivElement>(null)
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<ApiError | null>(null)
  const [submitting, setSubmitting] = useState(false)

  useEffect(() => headingRef.current?.focus(), [])

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    setError(null)
    setSubmitting(true)
    try {
      const session = await auth.login(username, password)
      setPassword('')
      navigate(session.must_change_password ? '/password-change' : '/inbox', {
        replace: true,
      })
    } catch (unknownError) {
      setPassword('')
      setError(asApiError(unknownError))
      queueMicrotask(() => alertRef.current?.focus())
    } finally {
      setSubmitting(false)
    }
  }

  const retryLogout = async () => {
    try {
      await auth.retryLogout()
    } catch {
      queueMicrotask(() => alertRef.current?.focus())
    }
  }

  const shownError = error ?? auth.error

  return (
    <main className="centered-page">
      <section className="auth-panel" aria-labelledby="login-heading">
        <p className="eyebrow">MBB</p>
        <h1 id="login-heading" tabIndex={-1} ref={headingRef}>
          Sign in
        </h1>
        <p className="muted">Use your MBB account to continue.</p>
        {auth.status === 'logout_unconfirmed' ? (
          <InlineAlert id="login-error" ref={alertRef} requestId={shownError?.requestId}>
            Server logout could not be confirmed. Protected information has been hidden.
            <button className="button button--secondary alert__action" type="button" onClick={() => void retryLogout()}>
              Retry logout
            </button>
          </InlineAlert>
        ) : shownError ? (
          <InlineAlert id="login-error" ref={alertRef} requestId={shownError.requestId}>
            {errorMessage(shownError)}
          </InlineAlert>
        ) : null}
        <form onSubmit={submit} noValidate>
          <div className="field">
            <label htmlFor="username">Username</label>
            <input
              id="username"
              name="username"
              autoComplete="username"
              required
              maxLength={128}
              aria-describedby={shownError ? 'login-error' : undefined}
              value={username}
              onChange={(event) => setUsername(event.target.value)}
            />
          </div>
          <PasswordField
            id="password"
            name="password"
            label="Password"
            autoComplete="current-password"
            required
            aria-describedby={shownError ? 'login-error' : undefined}
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
          <button className="button button--primary button--full" type="submit" disabled={submitting}>
            {submitting ? 'Signing in…' : 'Sign in'}
          </button>
        </form>
      </section>
    </main>
  )
}
