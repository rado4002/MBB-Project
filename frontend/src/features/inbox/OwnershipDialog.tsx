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
  ConversationOwnership,
  OwnershipTransitionResponse,
} from '../../api/contracts/conversations'
import { asApiError, type ApiError } from '../../api/errors'
import { useAuth } from '../../auth/AuthProvider'
import { InlineAlert } from '../../components/InlineAlert'

interface OwnershipDialogProps {
  open: boolean
  conversationId: string
  ownership: ConversationOwnership
  client: ConversationApiClient
  returnFocusRef: RefObject<HTMLButtonElement | null>
  onClose: () => void
  onChanged: (result: OwnershipTransitionResponse) => Promise<void>
  onConflict: () => Promise<void>
}

function errorCopy(error: ApiError): string {
  if (error.operatorMessage) return error.operatorMessage
  switch (error.code) {
    case 'OWNERSHIP_CONFLICT':
      return 'Conversation ownership changed. Current details were refreshed.'
    case 'IDEMPOTENCY_IN_PROGRESS':
      return 'This ownership request is still being processed. Retry the unchanged action in a moment.'
    case 'IDEMPOTENCY_CONFLICT':
      return 'This retry key was already used for another ownership request.'
    case 'AI_DISABLED':
      return 'The MBB AI Assistant is currently disabled. This conversation remains under human control.'
    case 'AI_UNAVAILABLE':
      return 'The MBB AI Assistant is currently unavailable. This conversation remains under human control.'
    case 'AI_RETURN_BLOCKED':
      return 'An unresolved condition prevents returning this conversation to the MBB AI Assistant.'
    default:
      break
  }
  switch (error.category) {
    case 'session_expired':
      return 'Your session has ended. Sign in again to continue.'
    case 'forbidden':
      return 'You do not have permission to change conversation ownership.'
    case 'throttled':
      return error.retryAfterSeconds !== undefined
        ? `Too many ownership attempts. Try again in ${error.retryAfterSeconds} seconds.`
        : 'Too many ownership attempts. Please wait before trying again.'
    case 'csrf':
      return 'The security check expired. Retry this unchanged action.'
    case 'unavailable':
      return 'The ownership service is temporarily unavailable. Retry this unchanged action.'
    default:
      return 'Conversation ownership could not be changed. Please try again.'
  }
}

export function OwnershipDialog({
  open,
  conversationId,
  ownership,
  client,
  returnFocusRef,
  onClose,
  onChanged,
  onConflict,
}: OwnershipDialogProps) {
  const auth = useAuth()
  const titleId = useId()
  const descriptionId = useId()
  const panelRef = useRef<HTMLElement>(null)
  const confirmRef = useRef<HTMLButtonElement>(null)
  const alertRef = useRef<HTMLDivElement>(null)
  const submittingRef = useRef(false)
  const completedRef = useRef(false)
  const controllerRef = useRef<AbortController | null>(null)
  const idempotencyKeyRef = useRef<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [requestError, setRequestError] = useState<ApiError | null>(null)
  const target = ownership.owner_type === 'ai' ? 'human' : 'ai'
  const takingControl = target === 'human'

  useEffect(() => {
    if (requestError) alertRef.current?.focus()
  }, [requestError])

  useEffect(() => {
    if (!open) return
    completedRef.current = false
    const appFrame = document.querySelector<HTMLElement>('.app-frame')
    const previousAriaHidden = appFrame?.getAttribute('aria-hidden')
    const previousOverflow = document.body.style.overflow
    const returnTarget = returnFocusRef.current
    appFrame?.setAttribute('inert', '')
    appFrame?.setAttribute('aria-hidden', 'true')
    document.body.style.overflow = 'hidden'
    confirmRef.current?.focus()

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !submittingRef.current) {
        event.preventDefault()
        onClose()
        return
      }
      if (event.key !== 'Tab') return
      const focusable = Array.from(
        panelRef.current?.querySelectorAll<HTMLElement>(
          'button:not([disabled]), [tabindex]:not([tabindex="-1"])',
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
      if (!panelRef.current?.contains(event.target as Node)) {
        confirmRef.current?.focus()
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
        if (!completedRef.current && returnTarget?.isConnected) returnTarget.focus()
      })
    }
  }, [onClose, open, returnFocusRef])

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (submittingRef.current) return
    submittingRef.current = true
    setSubmitting(true)
    setRequestError(null)
    idempotencyKeyRef.current ??= crypto.randomUUID()
    const controller = new AbortController()
    controllerRef.current = controller
    try {
      const csrfToken = await auth.getCsrfForMutation()
      const result = await client.changeOwnership(
        conversationId,
        {
          target_owner_type: target,
          expected_version: ownership.version,
        },
        idempotencyKeyRef.current,
        csrfToken,
        controller.signal,
      )
      idempotencyKeyRef.current = null
      completedRef.current = true
      await onChanged(result)
      onClose()
    } catch (unknownError) {
      if (controller.signal.aborted) return
      const error = asApiError(unknownError)
      setRequestError(error)
      if (error.code === 'OWNERSHIP_CONFLICT') await onConflict()
    } finally {
      if (controllerRef.current === controller) controllerRef.current = null
      submittingRef.current = false
      setSubmitting(false)
    }
  }

  if (!open) return null
  return createPortal(
    <div className="ownership-dialog" role="presentation">
      <section
        className="ownership-dialog__panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descriptionId}
        ref={panelRef}
        tabIndex={-1}
      >
        <header className="ownership-dialog__header">
          <div>
            <p className="eyebrow">Conversation control</p>
            <h2 id={titleId}>
              {takingControl ? 'Escalate to Human' : 'Return to AI'}
            </h2>
          </div>
        </header>
        <p id={descriptionId} className="ownership-dialog__description">
          {takingControl
            ? 'Escalate this conversation to yourself? You will take control, and the MBB AI Assistant will be paused.'
            : 'Return this conversation to the MBB AI Assistant? Your active ownership will end, and the AI assistant will become eligible to handle future messages.'}
        </p>
        <form className="ownership-form" onSubmit={(event) => void submit(event)}>
          {requestError ? (
            <InlineAlert ref={alertRef} requestId={requestError.requestId}>
              {errorCopy(requestError)}
            </InlineAlert>
          ) : null}
          <div className="ownership-form__actions">
            <button
              className="button button--secondary"
              type="button"
              disabled={submitting}
              onClick={onClose}
            >
              Cancel
            </button>
            <button
              className="button button--primary"
              type="submit"
              disabled={submitting}
              ref={confirmRef}
            >
              {submitting
                ? takingControl
                  ? 'Taking control…'
                  : 'Returning to AI…'
                : takingControl
                  ? 'Take Control'
                  : 'Return to AI'}
            </button>
          </div>
        </form>
      </section>
    </div>,
    document.body,
  )
}
