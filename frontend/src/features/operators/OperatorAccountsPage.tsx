import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type FormEvent,
  type ReactNode,
  type RefObject,
} from 'react'
import { createPortal } from 'react-dom'
import { createOperatorAccountsApiClient } from '../../api/operatorAccounts'
import type { OperatorAccountSummary } from '../../api/contracts/operatorAccounts'
import { ApiError, asApiError } from '../../api/errors'
import { useAuth } from '../../auth/AuthProvider'
import { InlineAlert } from '../../components/InlineAlert'
import { PasswordField } from '../../components/PasswordField'

type AccountAction =
  | { kind: 'create' }
  | { kind: 'password'; account: OperatorAccountSummary }
  | { kind: 'disable'; account: OperatorAccountSummary }
  | { kind: 'enable'; account: OperatorAccountSummary }

interface AccountDialogProps {
  title: string
  description: string
  returnFocusRef: RefObject<HTMLButtonElement | null>
  initialFocusRef: RefObject<HTMLElement | null>
  busy: boolean
  onClose: () => void
  children: ReactNode
}

function AccountDialog({
  title,
  description,
  returnFocusRef,
  initialFocusRef,
  busy,
  onClose,
  children,
}: AccountDialogProps) {
  const titleId = useId()
  const descriptionId = useId()
  const panelRef = useRef<HTMLElement>(null)
  const busyRef = useRef(busy)
  const onCloseRef = useRef(onClose)

  useEffect(() => {
    busyRef.current = busy
  }, [busy])
  useEffect(() => {
    onCloseRef.current = onClose
  }, [onClose])

  useEffect(() => {
    const appFrame = document.querySelector<HTMLElement>('.app-frame')
    const previousAriaHidden = appFrame?.getAttribute('aria-hidden')
    const previousOverflow = document.body.style.overflow
    const returnTarget = returnFocusRef.current
    appFrame?.setAttribute('inert', '')
    appFrame?.setAttribute('aria-hidden', 'true')
    document.body.style.overflow = 'hidden'
    initialFocusRef.current?.focus()

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !busyRef.current) {
        event.preventDefault()
        onCloseRef.current()
        return
      }
      if (event.key !== 'Tab') return
      const focusable = Array.from(
        panelRef.current?.querySelectorAll<HTMLElement>(
          'button:not([disabled]), input:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ) ?? [],
      )
      if (!focusable.length) {
        event.preventDefault()
        panelRef.current?.focus()
        return
      }
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }
    const keepFocusInside = (event: FocusEvent) => {
      if (!panelRef.current?.contains(event.target as Node)) initialFocusRef.current?.focus()
    }
    document.addEventListener('keydown', handleKeyDown)
    document.addEventListener('focusin', keepFocusInside)
    return () => {
      document.removeEventListener('keydown', handleKeyDown)
      document.removeEventListener('focusin', keepFocusInside)
      appFrame?.removeAttribute('inert')
      if (previousAriaHidden == null) appFrame?.removeAttribute('aria-hidden')
      else appFrame?.setAttribute('aria-hidden', previousAriaHidden)
      document.body.style.overflow = previousOverflow
      queueMicrotask(() => {
        if (returnTarget?.isConnected) returnTarget.focus()
      })
    }
  }, [initialFocusRef, returnFocusRef])

  return createPortal(
    <div className="account-dialog" role="presentation">
      <section
        className="account-dialog__panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descriptionId}
        ref={panelRef}
        tabIndex={-1}
      >
        <header>
          <p className="eyebrow">Operator access</p>
          <h2 id={titleId}>{title}</h2>
        </header>
        <p id={descriptionId} className="account-dialog__description">{description}</p>
        {children}
      </section>
    </div>,
    document.body,
  )
}

function accountErrorCopy(error: ApiError): string {
  if (error.code === 'recent_reauthentication_required') {
    return 'Confirm your Administrator password, then retry the action.'
  }
  if (error.operatorMessage) return error.operatorMessage
  if (error.category === 'session_expired') return 'Your session has ended. Sign in again to continue.'
  if (error.category === 'forbidden') return 'You do not have permission to manage Operator accounts.'
  if (error.category === 'unavailable') return 'Operator account management is temporarily unavailable.'
  return 'The Operator account action could not be completed.'
}

function displayDate(value: string | null): string {
  if (!value) return 'Never'
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? 'Unavailable' : parsed.toLocaleString()
}

export function OperatorAccountsPage() {
  const auth = useAuth()
  const client = useMemo(
    () => createOperatorAccountsApiClient(auth.handleSessionExpired),
    [auth.handleSessionExpired],
  )
  const [accounts, setAccounts] = useState<OperatorAccountSummary[]>([])
  const [loading, setLoading] = useState(true)
  const [pageError, setPageError] = useState<ApiError | null>(null)
  const [success, setSuccess] = useState<string | null>(null)
  const [action, setAction] = useState<AccountAction | null>(null)
  const [pendingAction, setPendingAction] = useState<AccountAction | null>(null)
  const [reauthOpen, setReauthOpen] = useState(false)
  const returnFocusRef = useRef<HTMLButtonElement>(null)

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoading(true)
    setPageError(null)
    try {
      const result = await client.list(signal)
      setAccounts(result.items)
    } catch (unknownError) {
      if (!signal?.aborted) setPageError(asApiError(unknownError))
    } finally {
      if (!signal?.aborted) setLoading(false)
    }
  }, [client])

  useEffect(() => {
    const controller = new AbortController()
    // The initial request owns the page's loading state and is aborted on unmount.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load(controller.signal)
    return () => controller.abort()
  }, [load])

  const hasRecentReauthentication = () => {
    const expiry = auth.session?.recent_reauthentication_expires_at_epoch
    return expiry != null && expiry >= Math.floor(Date.now() / 1000)
  }

  const beginAction = (nextAction: AccountAction, trigger: HTMLButtonElement) => {
    returnFocusRef.current = trigger
    setPageError(null)
    setSuccess(null)
    if (hasRecentReauthentication()) {
      setAction(nextAction)
      return
    }
    setPendingAction(nextAction)
    setReauthOpen(true)
  }

  const applyAuthoritativeAccount = (updated: OperatorAccountSummary) => {
    setAccounts((current) => {
      const exists = current.some((account) => account.account_id === updated.account_id)
      const next = exists
        ? current.map((account) => account.account_id === updated.account_id ? updated : account)
        : [...current, updated]
      return next.sort((left, right) => left.display_name.localeCompare(right.display_name))
    })
  }

  const requireReauthenticationAgain = (failedAction: AccountAction, error: ApiError) => {
    if (error.code !== 'recent_reauthentication_required') return false
    setAction(null)
    setPendingAction(failedAction)
    setReauthOpen(true)
    return true
  }

  return (
    <section className="operators-page" aria-labelledby="operators-heading">
      <header className="operators-header">
        <div>
          <p className="eyebrow">Administration</p>
          <h1 id="operators-heading">Operators</h1>
          <p>Create Operator access, reset passwords, and disable access while preserving history.</p>
        </div>
        <button
          className="button button--primary"
          type="button"
          onClick={(event) => beginAction({ kind: 'create' }, event.currentTarget)}
        >
          + Create Operator
        </button>
      </header>

      {pageError ? (
        <InlineAlert requestId={pageError.requestId}>{accountErrorCopy(pageError)}</InlineAlert>
      ) : null}
      {success ? <div className="alert account-success" role="status">{success}</div> : null}

      {loading ? <p className="muted" role="status">Loading Operators…</p> : null}
      {!loading && !pageError && accounts.length === 0 ? (
        <div className="operators-empty"><h2>No Operators yet</h2><p>Create the first Operator account when access is needed.</p></div>
      ) : null}
      {!loading && accounts.length > 0 ? (
        <ul className="operator-account-list" aria-label="Operator accounts">
          {accounts.map((account) => (
            <li key={account.account_id}>
              <article className="operator-account-card">
                <div className="operator-account-card__identity">
                  <div>
                    <h2>{account.display_name}</h2>
                    <p className="muted">@{account.username}</p>
                  </div>
                  <span className={`account-status account-status--${account.status}`}>
                    {account.status === 'active' ? 'Active' : 'Disabled'}
                  </span>
                </div>
                <dl>
                  <div><dt>Email</dt><dd>{account.email ?? 'Not provided'}</dd></div>
                  <div><dt>Last login</dt><dd>{displayDate(account.last_login_at)}</dd></div>
                </dl>
                <div className="operator-account-card__actions">
                  {account.status === 'active' ? (
                    <>
                      <button
                        className="button button--secondary"
                        type="button"
                        onClick={(event) => beginAction({ kind: 'password', account }, event.currentTarget)}
                      >
                        Set New Password
                      </button>
                      <button
                        className="button button--secondary"
                        type="button"
                        onClick={(event) => beginAction({ kind: 'disable', account }, event.currentTarget)}
                      >
                        Disable
                      </button>
                    </>
                  ) : (
                    <button
                      className="button button--primary"
                      type="button"
                      onClick={(event) => beginAction({ kind: 'enable', account }, event.currentTarget)}
                    >
                      Re-enable
                    </button>
                  )}
                </div>
              </article>
            </li>
          ))}
        </ul>
      ) : null}

      {reauthOpen ? (
        <ReauthenticationDialog
          returnFocusRef={returnFocusRef}
          onClose={() => {
            setReauthOpen(false)
            setPendingAction(null)
          }}
          onConfirmed={() => {
            setReauthOpen(false)
            setAction(pendingAction)
            setPendingAction(null)
          }}
        />
      ) : null}
      {action?.kind === 'create' ? (
        <CreateOperatorDialog
          client={client}
          returnFocusRef={returnFocusRef}
          onClose={() => setAction(null)}
          onCompleted={(account) => {
            applyAuthoritativeAccount(account)
            setAction(null)
            setSuccess(`${account.display_name} can now sign in as an Operator.`)
          }}
          onRecentReauthRequired={(error) => requireReauthenticationAgain(action, error)}
        />
      ) : null}
      {action?.kind === 'password' || action?.kind === 'enable' ? (
        <OperatorPasswordDialog
          mode={action.kind}
          account={action.account}
          client={client}
          returnFocusRef={returnFocusRef}
          onClose={() => setAction(null)}
          onCompleted={(account) => {
            applyAuthoritativeAccount(account)
            setAction(null)
            setSuccess(
              action.kind === 'enable'
                ? `${account.display_name} is active and can sign in with the new password.`
                : `${account.display_name}'s password was updated and previous sessions were revoked.`,
            )
          }}
          onRecentReauthRequired={(error) => requireReauthenticationAgain(action, error)}
        />
      ) : null}
      {action?.kind === 'disable' ? (
        <DisableOperatorDialog
          account={action.account}
          client={client}
          returnFocusRef={returnFocusRef}
          onClose={() => setAction(null)}
          onCompleted={(account) => {
            applyAuthoritativeAccount(account)
            setAction(null)
            setSuccess(`${account.display_name}'s access was disabled and previous sessions were revoked.`)
          }}
          onRecentReauthRequired={(error) => requireReauthenticationAgain(action, error)}
        />
      ) : null}
    </section>
  )
}

interface DialogCommonProps {
  client: ReturnType<typeof createOperatorAccountsApiClient>
  returnFocusRef: RefObject<HTMLButtonElement | null>
  onClose: () => void
  onRecentReauthRequired: (error: ApiError) => boolean
}

function ReauthenticationDialog({
  returnFocusRef,
  onClose,
  onConfirmed,
}: {
  returnFocusRef: RefObject<HTMLButtonElement | null>
  onClose: () => void
  onConfirmed: () => void
}) {
  const auth = useAuth()
  const inputRef = useRef<HTMLInputElement>(null)
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<ApiError | null>(null)
  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (busy || !password) return
    setBusy(true)
    setError(null)
    const submitted = password
    setPassword('')
    try {
      await auth.reauthenticate(submitted)
      onConfirmed()
    } catch (unknownError) {
      setError(asApiError(unknownError))
    } finally {
      setBusy(false)
    }
  }
  return (
    <AccountDialog
      title="Confirm Administrator password"
      description="Confirm your own password before changing Operator access."
      returnFocusRef={returnFocusRef}
      initialFocusRef={inputRef}
      busy={busy}
      onClose={onClose}
    >
      <form className="account-dialog__form" onSubmit={(event) => void submit(event)}>
        {error ? <InlineAlert requestId={error.requestId}>{accountErrorCopy(error)}</InlineAlert> : null}
        <PasswordField
          id="administrator-password"
          label="Administrator password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          autoComplete="current-password"
          required
          ref={inputRef}
        />
        <DialogActions busy={busy} confirmLabel="Confirm" onClose={onClose} />
      </form>
    </AccountDialog>
  )
}

function CreateOperatorDialog({
  client,
  returnFocusRef,
  onClose,
  onCompleted,
  onRecentReauthRequired,
}: DialogCommonProps & { onCompleted: (account: OperatorAccountSummary) => void }) {
  const auth = useAuth()
  const inputRef = useRef<HTMLInputElement>(null)
  const [username, setUsername] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<ApiError | null>(null)

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (busy) return
    if (password !== confirmation) {
      setPassword('')
      setConfirmation('')
      setError(new ApiError({ status: 422, code: 'password_confirmation_mismatch', category: 'validation', operatorMessage: 'The passwords do not match.' }))
      return
    }
    setBusy(true)
    setError(null)
    const body = { username, display_name: displayName, email: email.trim() || null, password }
    setPassword('')
    setConfirmation('')
    try {
      const csrfToken = await auth.getCsrfForMutation()
      onCompleted(await client.create(body, csrfToken))
    } catch (unknownError) {
      const apiError = asApiError(unknownError)
      if (!onRecentReauthRequired(apiError)) setError(apiError)
    } finally {
      body.password = ''
      setBusy(false)
    }
  }

  return (
    <AccountDialog
      title="Create Operator"
      description="Create an active Operator who can sign in directly with the password you set."
      returnFocusRef={returnFocusRef}
      initialFocusRef={inputRef}
      busy={busy}
      onClose={onClose}
    >
      <form className="account-dialog__form" onSubmit={(event) => void submit(event)}>
        {error ? <InlineAlert requestId={error.requestId}>{accountErrorCopy(error)}</InlineAlert> : null}
        <label className="field"><span>Username</span><input ref={inputRef} value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="off" minLength={3} maxLength={32} required /></label>
        <label className="field"><span>Display name</span><input value={displayName} onChange={(event) => setDisplayName(event.target.value)} autoComplete="off" maxLength={100} required /></label>
        <label className="field"><span>Email (optional)</span><input type="email" value={email} onChange={(event) => setEmail(event.target.value)} autoComplete="off" maxLength={320} /></label>
        <PasswordField id="create-operator-password" label="Password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="new-password" minLength={14} maxLength={128} required />
        <PasswordField id="create-operator-password-confirmation" label="Confirm password" value={confirmation} onChange={(event) => setConfirmation(event.target.value)} autoComplete="new-password" minLength={14} maxLength={128} required />
        <p className="field-help">Use 14–128 characters and avoid the Operator's name or username.</p>
        <DialogActions busy={busy} confirmLabel="Create Operator" onClose={onClose} />
      </form>
    </AccountDialog>
  )
}

function OperatorPasswordDialog({
  mode,
  account,
  client,
  returnFocusRef,
  onClose,
  onCompleted,
  onRecentReauthRequired,
}: DialogCommonProps & {
  mode: 'password' | 'enable'
  account: OperatorAccountSummary
  onCompleted: (account: OperatorAccountSummary) => void
}) {
  const auth = useAuth()
  const inputRef = useRef<HTMLInputElement>(null)
  const [password, setPassword] = useState('')
  const [confirmation, setConfirmation] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<ApiError | null>(null)
  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (busy) return
    if (password !== confirmation) {
      setPassword('')
      setConfirmation('')
      setError(new ApiError({ status: 422, code: 'password_confirmation_mismatch', category: 'validation', operatorMessage: 'The passwords do not match.' }))
      return
    }
    setBusy(true)
    setError(null)
    const body = { new_password: password }
    setPassword('')
    setConfirmation('')
    try {
      const csrfToken = await auth.getCsrfForMutation()
      const result = mode === 'enable'
        ? await client.enable(account.account_id, body, csrfToken)
        : await client.setPassword(account.account_id, body, csrfToken)
      onCompleted(result)
    } catch (unknownError) {
      const apiError = asApiError(unknownError)
      if (!onRecentReauthRequired(apiError)) setError(apiError)
    } finally {
      body.new_password = ''
      setBusy(false)
    }
  }
  return (
    <AccountDialog
      title={mode === 'enable' ? 'Re-enable Operator' : 'Set New Password'}
      description={
        mode === 'enable'
          ? `Set a new password and restore ${account.display_name}'s access. Previous sessions remain revoked.`
          : `Set a new password for ${account.display_name}. All previous sessions will be revoked.`
      }
      returnFocusRef={returnFocusRef}
      initialFocusRef={inputRef}
      busy={busy}
      onClose={onClose}
    >
      <form className="account-dialog__form" onSubmit={(event) => void submit(event)}>
        {error ? <InlineAlert requestId={error.requestId}>{accountErrorCopy(error)}</InlineAlert> : null}
        <PasswordField id="operator-new-password" label="New password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="new-password" minLength={14} maxLength={128} required ref={inputRef} />
        <PasswordField id="operator-new-password-confirmation" label="Confirm new password" value={confirmation} onChange={(event) => setConfirmation(event.target.value)} autoComplete="new-password" minLength={14} maxLength={128} required />
        <p className="field-help">Use 14–128 characters and avoid the Operator's name or username.</p>
        <DialogActions busy={busy} confirmLabel={mode === 'enable' ? 'Re-enable Operator' : 'Set New Password'} onClose={onClose} />
      </form>
    </AccountDialog>
  )
}

function DisableOperatorDialog({
  account,
  client,
  returnFocusRef,
  onClose,
  onCompleted,
  onRecentReauthRequired,
}: DialogCommonProps & {
  account: OperatorAccountSummary
  onCompleted: (account: OperatorAccountSummary) => void
}) {
  const auth = useAuth()
  const confirmRef = useRef<HTMLButtonElement>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<ApiError | null>(null)
  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      const csrfToken = await auth.getCsrfForMutation()
      onCompleted(await client.disable(account.account_id, csrfToken))
    } catch (unknownError) {
      const apiError = asApiError(unknownError)
      if (!onRecentReauthRequired(apiError)) setError(apiError)
    } finally {
      setBusy(false)
    }
  }
  return (
    <AccountDialog
      title="Disable Operator"
      description={`Disable ${account.display_name}? Access and existing sessions will be removed, but historical messages, notes, and audit attribution will be preserved.`}
      returnFocusRef={returnFocusRef}
      initialFocusRef={confirmRef}
      busy={busy}
      onClose={onClose}
    >
      <form className="account-dialog__form" onSubmit={(event) => void submit(event)}>
        {error ? <InlineAlert requestId={error.requestId}>{accountErrorCopy(error)}</InlineAlert> : null}
        <DialogActions busy={busy} confirmLabel="Disable Operator" onClose={onClose} confirmRef={confirmRef} />
      </form>
    </AccountDialog>
  )
}

function DialogActions({
  busy,
  confirmLabel,
  onClose,
  confirmRef,
}: {
  busy: boolean
  confirmLabel: string
  onClose: () => void
  confirmRef?: RefObject<HTMLButtonElement | null>
}) {
  return (
    <div className="account-dialog__actions">
      <button className="button button--secondary" type="button" disabled={busy} onClick={onClose}>Cancel</button>
      <button className="button button--primary" type="submit" disabled={busy} ref={confirmRef}>
        {busy ? 'Saving…' : confirmLabel}
      </button>
    </div>
  )
}
