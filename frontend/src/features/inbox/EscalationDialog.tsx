import {
  useEffect,
  useId,
  useRef,
  useState,
  type FormEvent,
  type RefObject,
} from 'react'
import { createPortal } from 'react-dom'
import type { ConversationApiClient } from '../../api/conversations'
import type {
  OperatorEscalationCreate,
  OperatorEscalationPriority,
  OperatorEscalationResponse,
  OperatorEscalationType,
} from '../../api/contracts/escalations'
import { asApiError, type ApiError } from '../../api/errors'
import { useAuth } from '../../auth/AuthProvider'
import { InlineAlert } from '../../components/InlineAlert'

interface EscalationDialogProps {
  open: boolean
  conversationId: string
  client: ConversationApiClient
  returnFocusRef: RefObject<HTMLButtonElement | null>
  onClose: () => void
  onCreated: (result: OperatorEscalationResponse) => Promise<void>
  onAlreadyOpen: () => Promise<void>
}

interface FieldErrors {
  type?: string
  reason?: string
}

interface IdempotencyReservation {
  fingerprint: string
  key: string
}

function errorCopy(error: ApiError): string {
  switch (error.code) {
    case 'ESCALATION_ALREADY_OPEN':
      return 'This conversation already has an active escalation. Conversation details were refreshed.'
    case 'IDEMPOTENCY_IN_PROGRESS':
      return 'This escalation request is still being processed. Retry the unchanged submission in a moment.'
    case 'IDEMPOTENCY_CONFLICT':
      return 'This retry key was used for different escalation details. Change the submission before trying again.'
    default:
      break
  }
  switch (error.category) {
    case 'session_expired':
      return 'Your session has ended. Sign in again to continue.'
    case 'forbidden':
      return 'You do not have permission to create escalations.'
    case 'throttled':
      return error.retryAfterSeconds !== undefined
        ? `Too many escalation attempts. Try again in ${error.retryAfterSeconds} seconds.`
        : 'Too many escalation attempts. Please wait before trying again.'
    case 'csrf':
      return 'The security check expired. Retry this unchanged submission.'
    case 'validation':
      return 'The escalation details were not accepted. Check the fields and try again.'
    case 'unavailable':
      return 'The escalation service is temporarily unavailable. Retry this unchanged submission.'
    default:
      return 'The escalation could not be created. Please try again.'
  }
}

function validate(type: string, reason: string): FieldErrors {
  const errors: FieldErrors = {}
  if (!type) errors.type = 'Choose an escalation type.'
  const normalizedReason = reason.trim()
  if (normalizedReason.length < 10) {
    errors.reason = 'Enter at least 10 characters after trimming spaces.'
  } else if (normalizedReason.length > 500) {
    errors.reason = 'Enter no more than 500 characters after trimming spaces.'
  }
  return errors
}

export function EscalationDialog({
  open,
  conversationId,
  client,
  returnFocusRef,
  onClose,
  onCreated,
  onAlreadyOpen,
}: EscalationDialogProps) {
  const auth = useAuth()
  const titleId = useId()
  const descriptionId = useId()
  const typeErrorId = useId()
  const reasonHelpId = useId()
  const reasonErrorId = useId()
  const panelRef = useRef<HTMLElement>(null)
  const typeRef = useRef<HTMLSelectElement>(null)
  const reasonRef = useRef<HTMLTextAreaElement>(null)
  const alertRef = useRef<HTMLDivElement>(null)
  const submittingRef = useRef(false)
  const reservationRef = useRef<IdempotencyReservation | null>(null)
  const controllerRef = useRef<AbortController | null>(null)
  const [type, setType] = useState<OperatorEscalationType | ''>('')
  const [priority, setPriority] = useState<OperatorEscalationPriority>('medium')
  const [reason, setReason] = useState('')
  const [errors, setErrors] = useState<FieldErrors>({})
  const [requestError, setRequestError] = useState<ApiError | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [alreadyOpen, setAlreadyOpen] = useState(false)

  useEffect(() => {
    if (requestError) alertRef.current?.focus()
  }, [requestError])

  useEffect(() => {
    if (!open) return
    const appFrame = document.querySelector<HTMLElement>('.app-frame')
    const previousAriaHidden = appFrame?.getAttribute('aria-hidden')
    const previousOverflow = document.body.style.overflow
    const returnTarget = returnFocusRef.current
    appFrame?.setAttribute('inert', '')
    appFrame?.setAttribute('aria-hidden', 'true')
    document.body.style.overflow = 'hidden'
    typeRef.current?.focus()

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !submittingRef.current) {
        event.preventDefault()
        onClose()
        return
      }
      if (event.key !== 'Tab') return
      const panel = panelRef.current
      if (!panel) return
      const focusable = Array.from(
        panel.querySelectorAll<HTMLElement>(
          'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ),
      )
      if (!focusable.length) {
        event.preventDefault()
        panel.focus()
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
      if (!panelRef.current?.contains(event.target as Node)) {
        typeRef.current?.focus()
      }
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
      controllerRef.current?.abort()
      queueMicrotask(() => {
        if (returnTarget?.isConnected) returnTarget.focus()
      })
    }
  }, [onClose, open, returnFocusRef])

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (submittingRef.current || alreadyOpen) return
    const nextErrors = validate(type, reason)
    setErrors(nextErrors)
    if (nextErrors.type) {
      typeRef.current?.focus()
      return
    }
    if (nextErrors.reason) {
      reasonRef.current?.focus()
      return
    }

    const payload: OperatorEscalationCreate = {
      type: type as OperatorEscalationType,
      priority,
      reason: reason.trim(),
    }
    const fingerprint = JSON.stringify(payload)
    let reservation = reservationRef.current
    if (!reservation || reservation.fingerprint !== fingerprint) {
      reservation = { fingerprint, key: crypto.randomUUID() }
      reservationRef.current = reservation
    }

    submittingRef.current = true
    setSubmitting(true)
    setRequestError(null)
    const controller = new AbortController()
    controllerRef.current = controller
    try {
      const csrfToken = await auth.getCsrfForMutation()
      const result = await client.createEscalation(
        conversationId,
        payload,
        reservation.key,
        csrfToken,
        controller.signal,
      )
      await onCreated(result)
      reservationRef.current = null
      setType('')
      setPriority('medium')
      setReason('')
      setErrors({})
      setRequestError(null)
      onClose()
    } catch (unknownError) {
      if (controller.signal.aborted) return
      const error = asApiError(unknownError)
      setRequestError(error)
      if (error.code === 'ESCALATION_ALREADY_OPEN') {
        setAlreadyOpen(true)
        await onAlreadyOpen()
      }
    } finally {
      if (controllerRef.current === controller) controllerRef.current = null
      submittingRef.current = false
      setSubmitting(false)
    }
  }

  if (!open) return null
  const reasonLength = reason.trim().length
  return createPortal(
    <div className="escalation-dialog" role="presentation">
      <section
        className="escalation-dialog__panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descriptionId}
        ref={panelRef}
        tabIndex={-1}
      >
        <header className="escalation-dialog__header">
          <div>
            <p className="eyebrow">Conversation action</p>
            <h2 id={titleId}>Create escalation</h2>
          </div>
          <button
            className="button button--secondary"
            type="button"
            disabled={submitting}
            onClick={onClose}
          >
            Cancel
          </button>
        </header>
        <p id={descriptionId} className="escalation-dialog__description">
          Record an issue for follow-up. Conversation state and routing remain unchanged.
        </p>
        <form className="escalation-form" noValidate onSubmit={(event) => void submit(event)}>
          {requestError ? (
            <InlineAlert ref={alertRef} requestId={requestError.requestId}>
              {errorCopy(requestError)}
            </InlineAlert>
          ) : null}
          <label className="field" htmlFor={`${titleId}-type`}>
            <span>Type</span>
            <select
              id={`${titleId}-type`}
              ref={typeRef}
              value={type}
              disabled={submitting || alreadyOpen}
              aria-invalid={Boolean(errors.type)}
              aria-describedby={errors.type ? typeErrorId : undefined}
              onChange={(event) => {
                setType(event.target.value as OperatorEscalationType | '')
                setErrors((current) => ({ ...current, type: undefined }))
              }}
            >
              <option value="">Choose a type</option>
              <option value="voice_note">Voice note</option>
              <option value="complex_issue">Complex issue</option>
              <option value="high_value_lead">High-value lead</option>
              <option value="payment_issue">Payment issue</option>
            </select>
          </label>
          {errors.type ? <p className="field-error" id={typeErrorId}>{errors.type}</p> : null}

          <label className="field" htmlFor={`${titleId}-priority`}>
            <span>Priority</span>
            <select
              id={`${titleId}-priority`}
              value={priority}
              disabled={submitting || alreadyOpen}
              onChange={(event) => setPriority(event.target.value as OperatorEscalationPriority)}
            >
              <option value="low">Low</option>
              <option value="medium">Medium</option>
              <option value="high">High</option>
            </select>
          </label>

          <label className="field" htmlFor={`${titleId}-reason`}>
            <span>Reason</span>
            <textarea
              id={`${titleId}-reason`}
              ref={reasonRef}
              rows={6}
              value={reason}
              maxLength={520}
              disabled={submitting || alreadyOpen}
              aria-invalid={Boolean(errors.reason)}
              aria-describedby={`${reasonHelpId}${errors.reason ? ` ${reasonErrorId}` : ''}`}
              onChange={(event) => {
                setReason(event.target.value)
                setErrors((current) => ({ ...current, reason: undefined }))
              }}
            />
          </label>
          <p className="field-help" id={reasonHelpId}>
            10–500 characters after trimming spaces. {reasonLength}/500
          </p>
          {errors.reason ? <p className="field-error" id={reasonErrorId}>{errors.reason}</p> : null}

          <div className="escalation-form__actions">
            <button
              className="button button--primary"
              type="submit"
              disabled={submitting || alreadyOpen}
            >
              {submitting ? 'Creating…' : 'Create escalation'}
            </button>
          </div>
        </form>
      </section>
    </div>,
    document.body,
  )
}
