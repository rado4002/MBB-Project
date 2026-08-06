import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent as ReactKeyboardEvent,
  type RefObject,
} from 'react'
import { createPortal } from 'react-dom'
import { Link } from 'react-router-dom'
import type { ConversationApiClient } from '../../api/conversations'
import type {
  ConversationOwnership,
  MessageSenderType,
  OperatorConversationDetail,
  OperatorInternalNoteItem,
  OperatorMessageItem,
  OperatorTimelineItem,
} from '../../api/contracts/conversations'
import { ApiError, errorMessage } from '../../api/errors'
import { useAuth } from '../../auth/AuthProvider'
import { InlineAlert } from '../../components/InlineAlert'
import { OwnershipDialog } from './OwnershipDialog'
import {
  useConversationDetail,
  useMessageHistory,
} from './useConversationWorkspace'

function formatTimestamp(value: string) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return 'Time unavailable'
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(date)
}

function label(value: string | null) {
  if (!value) return 'Not available'
  return value
    .split('_')
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ')
}

function safeInterest(value: string) {
  return Array.from(value).slice(0, 80).join('')
}

function useMediaQuery(query: string) {
  const [matches, setMatches] = useState(() =>
    typeof window !== 'undefined' && typeof window.matchMedia === 'function'
      ? window.matchMedia(query).matches
      : false,
  )

  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return
    const mediaQuery = window.matchMedia(query)
    const update = () => setMatches(mediaQuery.matches)
    update()
    mediaQuery.addEventListener('change', update)
    return () => mediaQuery.removeEventListener('change', update)
  }, [query])

  return matches
}

function actorLabel(senderType: MessageSenderType) {
  switch (senderType) {
    case 'customer':
      return 'Customer'
    case 'operator':
      return 'Operator'
    case 'system':
      return 'System'
    default:
      return 'Unknown sender'
  }
}

function messageActorLabel(message: OperatorMessageItem) {
  if (message.sender_type === 'operator' && message.operator_author) {
    return `${message.operator_author.display_name} — Operator`
  }
  return actorLabel(message.sender_type)
}

function deliveryLabel(state: OperatorMessageItem['delivery_state']) {
  if (!state) return null
  return state.charAt(0).toUpperCase() + state.slice(1)
}

function timelineItemKey(item: OperatorTimelineItem) {
  return item.kind === 'message'
    ? `message:${item.message_id}`
    : `internal_note:${item.note_id}`
}

function ownershipLabel(ownership: ConversationOwnership) {
  return ownership.owner_type === 'ai'
    ? 'MBB AI Assistant'
    : ownership.human_owner?.display_name ?? 'Human Operator'
}

const REPLY_ELIGIBLE_STATUSES = new Set([
  'active',
  'qualifying',
  'nurturing',
  'escalated',
])

function replyUnavailableReason(
  detail: OperatorConversationDetail | null,
  accountId: string | undefined,
  hasReplyCapability: boolean,
) {
  if (!hasReplyCapability) {
    return 'Reply unavailable — your account does not have permission to reply.'
  }
  if (!detail) {
    return 'Reply unavailable — conversation ownership is unavailable.'
  }
  if (detail.ownership.owner_type === 'ai') {
    return 'Reply unavailable — this conversation is controlled by MBB AI Assistant.'
  }
  if (
    detail.ownership.ai_execution_state !== 'paused' ||
    !detail.ownership.human_owner
  ) {
    return 'Reply unavailable — conversation ownership is unavailable.'
  }
  if (detail.ownership.human_owner.account_id !== accountId) {
    return `Reply unavailable — only ${detail.ownership.human_owner.display_name} may reply.`
  }
  if (!REPLY_ELIGIBLE_STATUSES.has(detail.status)) {
    return 'Reply unavailable — this conversation is not currently eligible for replies.'
  }
  return null
}

function workspaceError(error: ApiError) {
  if (error.status === 404 || error.code === 'CONVERSATION_NOT_FOUND') {
    return 'This conversation is unavailable.'
  }
  if (error.category === 'forbidden') {
    return 'You do not have permission to view this conversation.'
  }
  if (error.category === 'unavailable') {
    return 'Conversation data is temporarily unavailable. Please try again.'
  }
  return errorMessage(error)
}

function DetailLoadingState() {
  return (
    <div className="workspace-loading" role="status">
      <span className="visually-hidden">Loading conversation details…</span>
      <div className="skeleton-stack" aria-hidden="true">
        <span className="skeleton-block skeleton-block--title" />
        <span className="skeleton-block skeleton-block--short" />
        <span className="skeleton-block" />
      </div>
    </div>
  )
}

function HistoryLoadingState() {
  return (
    <div className="history-loading" role="status">
      <span className="visually-hidden">Loading messages…</span>
      <div className="skeleton-stack" aria-hidden="true">
        <span className="skeleton-message" />
        <span className="skeleton-message skeleton-message--outbound" />
        <span className="skeleton-message" />
      </div>
    </div>
  )
}

function messageContent(message: OperatorMessageItem) {
  if (message.content_type === 'text') {
    return <p className="message-text">{message.text ?? ''}</p>
  }
  const kind = message.media?.kind ?? message.content_type
  return (
    <p className="message-media">
      {kind === 'voice_note' ? 'Voice note unavailable' : 'Image unavailable'}
    </p>
  )
}

function ConversationHeader({
  detail,
  loading,
  error,
  onRetry,
  headingRef,
  ownershipRef,
}: {
  detail: OperatorConversationDetail | null
  loading: boolean
  error: ApiError | null
  onRetry: () => Promise<void>
  headingRef: RefObject<HTMLHeadingElement | null>
  ownershipRef: RefObject<HTMLSpanElement | null>
}) {
  const errorRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (error) errorRef.current?.focus()
  }, [error])

  if (loading) {
    return (
      <div className="conversation-header__content">
        <h2 id="workspace-heading" tabIndex={-1} ref={headingRef}>Conversation</h2>
        <DetailLoadingState />
      </div>
    )
  }
  if (error) {
    return (
      <div className="conversation-header__content">
        <h2 id="workspace-heading" tabIndex={-1} ref={headingRef}>Conversation</h2>
        <InlineAlert ref={errorRef} requestId={error.requestId}>
          {workspaceError(error)}
          <button className="button button--secondary alert__action" type="button" onClick={() => void onRetry()}>
            Retry details
          </button>
        </InlineAlert>
      </div>
    )
  }
  if (!detail) return null
  const customerName = detail.customer.display_name?.trim() || 'Customer'
  return (
    <div className="conversation-header__content">
      <h2 id="workspace-heading" tabIndex={-1} ref={headingRef}>{customerName}</h2>
      <p className="masked-phone">{detail.customer.phone_masked}</p>
      <p className="visually-hidden" role="status">Conversation details loaded.</p>
      <div className="conversation-metadata" aria-label="Conversation attributes">
        <span>Status: {label(detail.status)}</span>
        <span>Language: {label(detail.language)}</span>
        <span
          className="ownership-summary"
          ref={ownershipRef}
          tabIndex={-1}
          aria-live="polite"
        >
          Controlled by {ownershipLabel(detail.ownership)}
        </span>
        <span>
          AI {detail.ownership.ai_execution_state === 'paused'
            ? 'paused'
            : label(detail.ownership.ai_execution_state)}
        </span>
        <span>
          Last activity <time dateTime={detail.updated_at}>{formatTimestamp(detail.updated_at)}</time>
        </span>
        {detail.open_escalation.exists ? <span>Open escalation</span> : null}
      </div>
    </div>
  )
}

function ContextBody({
  detail,
  loading,
  error,
  productHeadingLevel = 'h4',
}: {
  detail: OperatorConversationDetail | null
  loading: boolean
  error: ApiError | null
  productHeadingLevel?: 'h3' | 'h4'
}) {
  if (loading) {
    return (
      <div className="context-loading" role="status">
        <span className="visually-hidden">Loading context…</span>
        <div className="skeleton-stack" aria-hidden="true">
          <span className="skeleton-block" />
          <span className="skeleton-block skeleton-block--short" />
          <span className="skeleton-block" />
        </div>
      </div>
    )
  }
  if (error) return <p>Context is unavailable.</p>
  if (!detail) return <p>Context is unavailable.</p>
  const ProductHeading = productHeadingLevel
  return (
    <>
      <dl className="context-details">
        {detail.lead ? (
          <>
            <div><dt>Lead score</dt><dd>{label(detail.lead.score)}</dd></div>
            <div><dt>Lead stage</dt><dd>{label(detail.lead.stage)}</dd></div>
            <div><dt>Lead intent</dt><dd>{label(detail.lead.intent)}</dd></div>
          </>
        ) : null}
      </dl>
      {detail.lead ? (
        <>
          <ProductHeading>Product interests</ProductHeading>
          {detail.lead.product_interests.length ? (
            <ul className="interest-list">
              {detail.lead.product_interests.slice(0, 5).map((interest, index) => (
                <li key={`${index}-${interest}`}>{safeInterest(interest)}</li>
              ))}
            </ul>
          ) : <p>No product interests available.</p>}
        </>
      ) : <p>No lead context is available.</p>}
    </>
  )
}

function ContextPanel({
  detail,
  loading,
  error,
}: {
  detail: OperatorConversationDetail | null
  loading: boolean
  error: ApiError | null
}) {
  return (
    <aside
      className="context-panel context-panel--desktop"
      aria-labelledby="context-heading"
      tabIndex={0}
    >
      <h3 id="context-heading">Context</h3>
      <ContextBody detail={detail} loading={loading} error={error} />
    </aside>
  )
}

function ContextDrawer({
  open,
  onClose,
  returnFocusRef,
  detail,
  loading,
  error,
}: {
  open: boolean
  onClose: () => void
  returnFocusRef: RefObject<HTMLButtonElement | null>
  detail: OperatorConversationDetail | null
  loading: boolean
  error: ApiError | null
}) {
  const titleId = useId()
  const panelRef = useRef<HTMLElement>(null)
  const closeRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    if (!open) return
    const appFrame = document.querySelector<HTMLElement>('.app-frame')
    const previousAriaHidden = appFrame?.getAttribute('aria-hidden')
    const previousOverflow = document.body.style.overflow
    appFrame?.setAttribute('inert', '')
    appFrame?.setAttribute('aria-hidden', 'true')
    document.body.style.overflow = 'hidden'
    closeRef.current?.focus()
    const returnTarget = returnFocusRef.current

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        onClose()
        return
      }
      if (event.key !== 'Tab') return
      const panel = panelRef.current
      if (!panel) return
      const focusable = Array.from(
        panel.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
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
      if (!panelRef.current?.contains(event.target as Node)) closeRef.current?.focus()
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
  }, [onClose, open, returnFocusRef])

  if (!open) return null
  return createPortal(
    <div className="context-drawer" role="presentation">
      <section
        className="context-drawer__panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        ref={panelRef}
        tabIndex={-1}
      >
        <header className="context-drawer__header">
          <h2 id={titleId}>Conversation details</h2>
          <button className="button button--secondary" type="button" onClick={onClose} ref={closeRef}>
            Close
          </button>
        </header>
        <div className="context-drawer__content">
          <ContextBody
            detail={detail}
            loading={loading}
            error={error}
            productHeadingLevel="h3"
          />
        </div>
      </section>
    </div>,
    document.body,
  )
}

function MessageTimeline({
  client,
  conversationId,
  detail,
  onReplyAccepted,
}: {
  client: ConversationApiClient
  conversationId: string
  detail: OperatorConversationDetail | null
  onReplyAccepted: () => Promise<void>
}) {
  const auth = useAuth()
  const history = useMessageHistory(client, conversationId)
  const timelineRef = useRef<HTMLDivElement>(null)
  const initialPositioned = useRef(false)
  const scrollAnchor = useRef<{ height: number; top: number } | null>(null)
  const errorRef = useRef<HTMLDivElement>(null)
  const olderErrorRef = useRef<HTMLDivElement>(null)
  const lastMessageIdRef = useRef<string | null>(null)

  const replyReason = replyUnavailableReason(
    detail,
    auth.session?.human.account_id,
    Boolean(auth.session?.capabilities.includes('message.reply')),
  )
  const canReply = replyReason === null
  const canCreateNote = Boolean(
    auth.session?.capabilities.includes('internal_note.create'),
  )

  useLayoutEffect(() => {
    const timeline = timelineRef.current
    if (!timeline || initialPositioned.current || history.loading || !history.items.length) return
    timeline.scrollTop = timeline.scrollHeight
    initialPositioned.current = true
  }, [history.items.length, history.loading])

  useLayoutEffect(() => {
    const timeline = timelineRef.current
    const anchor = scrollAnchor.current
    if (!timeline || !anchor || history.loadingOlder) return
    timeline.scrollTop = anchor.top + (timeline.scrollHeight - anchor.height)
    scrollAnchor.current = null
  }, [history.items.length, history.loadingOlder])

  useLayoutEffect(() => {
    const timeline = timelineRef.current
    const lastMessageId = history.items.at(-1)
      ? timelineItemKey(history.items.at(-1) as OperatorTimelineItem)
      : null
    if (
      timeline &&
      initialPositioned.current &&
      lastMessageIdRef.current &&
      lastMessageId !== lastMessageIdRef.current &&
      !history.loadingOlder
    ) {
      timeline.scrollTop = timeline.scrollHeight
    }
    lastMessageIdRef.current = lastMessageId
  }, [history.items, history.loadingOlder])

  useEffect(() => {
    if (history.error) errorRef.current?.focus()
  }, [history.error])

  useEffect(() => {
    if (history.olderError) olderErrorRef.current?.focus()
  }, [history.olderError])

  const loadEarlier = () => {
    const timeline = timelineRef.current
    if (timeline) {
      scrollAnchor.current = {
        height: timeline.scrollHeight,
        top: timeline.scrollTop,
      }
    }
    void history.loadEarlier()
  }

  return (
    <section
      className="timeline-panel"
      aria-labelledby="messages-heading"
      aria-busy={history.loading || history.loadingOlder}
    >
      <h3 className="timeline-panel__label visually-hidden" id="messages-heading">
        Timeline
      </h3>
      {history.loading ? (
        <HistoryLoadingState />
      ) : history.error ? (
        <InlineAlert ref={errorRef} requestId={history.error.requestId}>
          {workspaceError(history.error)}
          <button className="button button--secondary alert__action" type="button" onClick={() => void history.retry()}>
            Retry messages
          </button>
        </InlineAlert>
      ) : history.items.length === 0 ? (
        <p className="workspace-state">No timeline items are available.</p>
      ) : (
        <div className="message-history" ref={timelineRef} role="region" aria-label="Conversation timeline" tabIndex={0}>
          <p className="visually-hidden" role="status">{history.items.length} timeline items loaded.</p>
          {history.nextOlderCursor ? (
            <div className="load-earlier" aria-live="polite">
              <button className="button button--secondary" type="button" disabled={history.loadingOlder} onClick={loadEarlier}>
                {history.loadingOlder ? 'Loading earlier…' : 'Load Earlier'}
              </button>
            </div>
          ) : null}
          {history.olderError ? (
            <InlineAlert ref={olderErrorRef} requestId={history.olderError.requestId}>
              Earlier messages could not be loaded. {errorMessage(history.olderError)}
              <button className="button button--secondary alert__action" type="button" onClick={loadEarlier}>
                Retry earlier messages
              </button>
            </InlineAlert>
          ) : null}
          <ol className="message-list">
            {history.items.map((item) => {
              if (item.kind === 'internal_note') {
                return (
                  <li key={timelineItemKey(item)} className="internal-note">
                    <article aria-label={`Internal note by ${item.author.display_name}`}>
                      <header>
                        <strong>Internal Note</strong>
                        <time dateTime={item.occurred_at}>{formatTimestamp(item.occurred_at)}</time>
                      </header>
                      <p className="internal-note__author">{item.author.display_name} — Operator</p>
                      <p className="message-text">{item.text}</p>
                    </article>
                  </li>
                )
              }
              const message = item
              const actor = messageActorLabel(message)
              const delivery = deliveryLabel(message.delivery_state)
              return (
                <li key={timelineItemKey(message)} className={`message message--${message.direction}`}>
                  <article aria-label={`${actor} message`}>
                    <header>
                      <strong>{actor}</strong>
                      <time dateTime={message.occurred_at}>{formatTimestamp(message.occurred_at)}</time>
                    </header>
                    {messageContent(message)}
                    {delivery ? (
                      <footer className={`message-delivery message-delivery--${message.delivery_state}`}>
                        {delivery}
                      </footer>
                    ) : null}
                  </article>
                </li>
              )
            })}
          </ol>
        </div>
      )}
      {(canReply || canCreateNote) ? (
        <ConversationComposer
          client={client}
          conversationId={conversationId}
          expectedOwnershipVersion={detail?.ownership.version ?? null}
          canReply={canReply}
          canCreateNote={canCreateNote}
          replyUnavailableReason={replyReason}
          onReplyAccepted={(message) => {
            history.appendAccepted(message)
            return onReplyAccepted()
          }}
          onNoteAccepted={(note) => history.appendInternalNote(note)}
        />
      ) : null}
    </section>
  )
}

function ConversationComposer({
  client,
  conversationId,
  expectedOwnershipVersion,
  canReply,
  canCreateNote,
  replyUnavailableReason,
  onReplyAccepted,
  onNoteAccepted,
}: {
  client: ConversationApiClient
  conversationId: string
  expectedOwnershipVersion: number | null
  canReply: boolean
  canCreateNote: boolean
  replyUnavailableReason: string | null
  onReplyAccepted: (message: OperatorMessageItem) => Promise<void>
  onNoteAccepted: (note: OperatorInternalNoteItem) => void
}) {
  const auth = useAuth()
  const [mode, setMode] = useState<'reply' | 'internal_note'>(
    canReply ? 'reply' : 'internal_note',
  )
  const [replyText, setReplyText] = useState('')
  const [noteText, setNoteText] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [validationError, setValidationError] = useState<string | null>(null)
  const [requestError, setRequestError] = useState<ApiError | null>(null)
  const [acceptedAnnouncement, setAcceptedAnnouncement] = useState('')
  const submittingRef = useRef(false)
  const focusAfterAcceptanceRef = useRef(false)
  const replyAttemptRef = useRef<{ text: string; key: string } | null>(null)
  const noteAttemptRef = useRef<{ text: string; key: string } | null>(null)
  const previousCanReplyRef = useRef(canReply)
  const errorRef = useRef<HTMLDivElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const helpId = useId()
  const errorId = useId()
  const replyReasonId = useId()
  const activeMode = mode === 'reply' && !canReply && canCreateNote
    ? 'internal_note'
    : mode
  const text = activeMode === 'reply' ? replyText : noteText
  const setText = activeMode === 'reply' ? setReplyText : setNoteText

  useEffect(() => {
    if (requestError) errorRef.current?.focus()
  }, [requestError])

  useEffect(() => {
    const replyBecameAvailable = !previousCanReplyRef.current && canReply
    previousCanReplyRef.current = canReply
    if (replyBecameAvailable && mode === 'internal_note' && !replyText && !noteText) {
      setMode('reply')
    }
  }, [canReply, mode, noteText, replyText])

  useEffect(() => {
    if (!submitting && focusAfterAcceptanceRef.current) {
      focusAfterAcceptanceRef.current = false
      textareaRef.current?.focus()
    }
  }, [submitting])

  const submit = async (event?: FormEvent<HTMLFormElement>) => {
    event?.preventDefault()
    if (submittingRef.current) return
    if (!text.trim()) {
      setValidationError(
        activeMode === 'reply'
          ? 'Enter a reply before submitting.'
          : 'Enter an internal note before submitting.',
      )
      textareaRef.current?.focus()
      return
    }
    if (Array.from(text).length > 4096) {
      setValidationError(
        `${activeMode === 'reply' ? 'Reply' : 'Internal note'} text must be 4,096 characters or fewer.`,
      )
      textareaRef.current?.focus()
      return
    }

    const attemptRef = activeMode === 'reply' ? replyAttemptRef : noteAttemptRef
    const attempt =
      attemptRef.current?.text === text
        ? attemptRef.current
        : { text, key: crypto.randomUUID() }
    attemptRef.current = attempt
    submittingRef.current = true
    setSubmitting(true)
    setValidationError(null)
    setRequestError(null)
    setAcceptedAnnouncement('')
    try {
      const csrfToken = await auth.getCsrfForMutation()
      if (activeMode === 'reply') {
        if (expectedOwnershipVersion === null) return
        const message = await client.createReply(
          conversationId,
          { text, expected_ownership_version: expectedOwnershipVersion },
          attempt.key,
          csrfToken,
        )
        historyAssertAccepted(message)
        setReplyText('')
        replyAttemptRef.current = null
        setAcceptedAnnouncement('Reply accepted and added to the timeline.')
        void onReplyAccepted(message).catch(() => undefined)
      } else {
        const note = await client.createInternalNote(
          conversationId,
          { text },
          attempt.key,
          csrfToken,
        )
        setNoteText('')
        noteAttemptRef.current = null
        setAcceptedAnnouncement('Internal note added to the timeline.')
        onNoteAccepted(note)
      }
      focusAfterAcceptanceRef.current = true
    } catch (unknownError) {
      const apiError = unknownError instanceof ApiError ? unknownError : null
      setRequestError(apiError)
      if (activeMode === 'internal_note' && apiError?.code === 'IDEMPOTENCY_CONFLICT') {
        noteAttemptRef.current = null
      }
      if (!(unknownError instanceof ApiError)) {
        setRequestError(new ApiError({
          status: 0,
          code: activeMode === 'reply' ? 'reply_unavailable' : 'internal_note_unavailable',
          category: 'unavailable',
        }))
      }
    } finally {
      submittingRef.current = false
      setSubmitting(false)
    }
  }

  const onKeyDown = (event: ReactKeyboardEvent<HTMLTextAreaElement>) => {
    if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
      event.preventDefault()
      event.currentTarget.form?.requestSubmit()
    }
  }

  return (
    <form
      className={`reply-composer${activeMode === 'internal_note' ? ' reply-composer--internal-note' : ''}`}
      onSubmit={(event) => void submit(event)}
    >
      <div className="composer-controls">
        <fieldset
          className="composer-modes"
          aria-describedby={replyUnavailableReason ? replyReasonId : undefined}
        >
          <legend className="visually-hidden">Choose message type</legend>
          <div className="composer-modes__options">
            <label>
              <input
                className="composer-mode-input visually-hidden"
                type="radio"
                name={`${helpId}-mode`}
                value="reply"
                checked={activeMode === 'reply'}
                disabled={!canReply || submitting}
                aria-describedby={replyUnavailableReason ? replyReasonId : undefined}
                onChange={() => {
                  setMode('reply')
                  setValidationError(null)
                  setRequestError(null)
                }}
              />
              Reply
            </label>
            <label>
              <input
                className="composer-mode-input visually-hidden"
                type="radio"
                name={`${helpId}-mode`}
                value="internal_note"
                checked={activeMode === 'internal_note'}
                disabled={!canCreateNote || submitting}
                onChange={() => {
                  setMode('internal_note')
                  setValidationError(null)
                  setRequestError(null)
                }}
              />
              Internal Note
            </label>
          </div>
        </fieldset>
        {replyUnavailableReason ? (
          <p
            className="reply-unavailable-reason"
            id={replyReasonId}
            role="note"
            tabIndex={0}
          >
            {replyUnavailableReason}
          </p>
        ) : null}
      </div>
      <label className="visually-hidden" htmlFor={`${helpId}-composer`}>
        {activeMode === 'reply' ? 'Reply to Customer' : 'Internal Note'}
      </label>
      {requestError ? (
        <InlineAlert ref={errorRef} requestId={requestError.requestId}>
          {errorMessage(requestError)} Your {activeMode === 'reply' ? 'reply' : 'internal note'} has been preserved.
        </InlineAlert>
      ) : null}
      <textarea
        id={`${helpId}-composer`}
        ref={textareaRef}
        value={text}
        maxLength={4096}
        rows={2}
        disabled={submitting}
        aria-invalid={validationError ? 'true' : undefined}
        aria-describedby={`${helpId} ${validationError ? errorId : ''}`.trim()}
        onChange={(event) => {
          setText(event.target.value)
          setValidationError(null)
          setRequestError(null)
        }}
        onKeyDown={onKeyDown}
      />
      <div className="reply-composer__footer">
        <div className="reply-composer__meta" id={helpId}>
          {activeMode === 'internal_note' ? (
            <span className="internal-note-warning" role="note">
              Internal only — not sent to the customer or available to AI.
            </span>
          ) : (
            <span className="composer-guidance">
              Sent to the customer through the conversation channel.
            </span>
          )}
          <span>{Array.from(text).length}/4,096 · Ctrl+Enter to submit</span>
        </div>
        <button className="button button--primary" type="submit" disabled={submitting}>
          {activeMode === 'reply'
            ? submitting ? 'Submitting…' : 'Submit Reply'
            : submitting ? 'Adding…' : 'Add Internal Note'}
        </button>
      </div>
      {validationError ? <p className="field-error" id={errorId}>{validationError}</p> : null}
      <p className="visually-hidden" role="status" aria-live="polite">
        {acceptedAnnouncement}
      </p>
    </form>
  )
}

function historyAssertAccepted(message: OperatorMessageItem) {
  if (message.delivery_state !== 'accepted') {
    throw new ApiError({
      status: 0,
      code: 'invalid_reply_state',
      category: 'unavailable',
    })
  }
}

export function ConversationWorkspace({
  client,
  conversationId,
  backTo,
  onOwnershipChanged,
}: {
  client: ConversationApiClient
  conversationId: string
  backTo: string
  onOwnershipChanged: () => Promise<void>
}) {
  const auth = useAuth()
  const detail = useConversationDetail(client, conversationId)
  const workspaceHeadingRef = useRef<HTMLHeadingElement>(null)
  const focusedConversationRef = useRef<string | null>(null)
  const ownershipStatusRef = useRef<HTMLSpanElement>(null)
  const detailsButtonRef = useRef<HTMLButtonElement>(null)
  const ownershipButtonRef = useRef<HTMLButtonElement>(null)
  const useNarrowActionMenu = useMediaQuery('(max-width: 30rem)')
  const [contextOpen, setContextOpen] = useState(false)
  const [ownershipOpen, setOwnershipOpen] = useState(false)
  const [ownershipAtOpen, setOwnershipAtOpen] =
    useState<ConversationOwnership | null>(null)
  const [successfulVersion, setSuccessfulVersion] = useState<number | null>(null)
  const closeContext = useCallback(() => setContextOpen(false), [])
  const closeOwnership = useCallback(() => setOwnershipOpen(false), [])
  const ownership = detail.detail?.ownership
  const mayReturnOwnedConversation = Boolean(
    ownership?.owner_type === 'human' &&
      (
        ownership.human_owner?.account_id === auth.session?.human.account_id ||
        auth.session?.human.role === 'administrator'
      ),
  )
  const canChangeOwnership = Boolean(
    ownership &&
      !detail.loading &&
      !detail.error &&
      auth.session?.capabilities.includes('conversation.ownership.change') &&
      (ownership.owner_type === 'ai' || mayReturnOwnedConversation),
  )

  const openOwnership = () => {
    if (!ownership) return
    setOwnershipAtOpen(ownership)
    setOwnershipOpen(true)
  }
  const handleOwnershipChanged = async (
    result: { ownership: ConversationOwnership },
  ) => {
    setSuccessfulVersion(result.ownership.version)
    await Promise.all([detail.refresh(), onOwnershipChanged()])
  }
  const refreshOwnership = async () => {
    await Promise.all([detail.refresh(), onOwnershipChanged()])
  }

  useEffect(() => {
    if (focusedConversationRef.current === conversationId || !workspaceHeadingRef.current) return
    const activeElement = document.activeElement
    if (
      activeElement !== document.body &&
      !activeElement?.closest('.conversation-row')
    ) return
    workspaceHeadingRef.current.focus()
    focusedConversationRef.current = conversationId
  }, [conversationId, detail.loading])
  useEffect(() => {
    if (successfulVersion !== null && !ownershipOpen) {
      ownershipStatusRef.current?.focus()
    }
  }, [ownershipOpen, successfulVersion])

  return (
    <section className="conversation-workspace" aria-labelledby="workspace-heading">
      <header className="workspace-header">
        <Link className="button button--secondary" to={backTo} aria-label="Back to Inbox">
          <span className="back-label back-label--long" aria-hidden="true">Back to Inbox</span>
          <span className="back-label back-label--short" aria-hidden="true">Back</span>
        </Link>
        <ConversationHeader
          detail={detail.detail}
          loading={detail.loading}
          error={detail.error}
          onRetry={detail.retry}
          headingRef={workspaceHeadingRef}
          ownershipRef={ownershipStatusRef}
        />
        {useNarrowActionMenu ? (
          <details className="workspace-action-menu">
            <summary className="button button--secondary">Actions</summary>
            <div className="workspace-action-menu__items">
              {canChangeOwnership ? (
                <button
                  className="button button--secondary"
                  type="button"
                  aria-haspopup="dialog"
                  aria-expanded={ownershipOpen}
                  onClick={openOwnership}
                  ref={ownershipButtonRef}
                >
                  {ownership?.owner_type === 'ai' ? 'Escalate to Human' : 'Return to AI'}
                </button>
              ) : null}
              <button
                className="button button--secondary"
                type="button"
                aria-haspopup="dialog"
                aria-expanded={contextOpen}
                onClick={() => setContextOpen(true)}
                ref={detailsButtonRef}
              >
                Details
              </button>
            </div>
          </details>
        ) : (
          <div className="workspace-toolbar__actions">
            {canChangeOwnership ? (
              <button
                className={`button ${ownership?.owner_type === 'human' ? 'button--secondary' : 'button--primary'}`}
                type="button"
                aria-haspopup="dialog"
                aria-expanded={ownershipOpen}
                aria-label={
                  ownership?.owner_type === 'ai'
                    ? 'Escalate to Human'
                    : 'Return to AI'
                }
                onClick={openOwnership}
                ref={ownershipButtonRef}
              >
                {ownership?.owner_type === 'ai'
                  ? 'Escalate to Human'
                  : 'Return to AI'}
              </button>
            ) : null}
            <button
              className="button button--secondary context-trigger"
              type="button"
              aria-haspopup="dialog"
              aria-expanded={contextOpen}
              onClick={() => setContextOpen(true)}
              ref={detailsButtonRef}
            >
              Details
            </button>
          </div>
        )}
      </header>
      <div className="workspace-columns">
        <MessageTimeline
          client={client}
          conversationId={conversationId}
          detail={detail.detail}
          onReplyAccepted={async () => {
            await Promise.all([detail.refresh(), onOwnershipChanged()])
          }}
        />
        <ContextPanel detail={detail.detail} loading={detail.loading} error={detail.error} />
      </div>
      <ContextDrawer
        open={contextOpen}
        onClose={closeContext}
        returnFocusRef={detailsButtonRef}
        detail={detail.detail}
        loading={detail.loading}
        error={detail.error}
      />
      {ownershipAtOpen ? (
        <OwnershipDialog
          open={ownershipOpen}
          conversationId={conversationId}
          ownership={ownershipAtOpen}
          client={client}
          returnFocusRef={ownershipButtonRef}
          onClose={closeOwnership}
          onChanged={handleOwnershipChanged}
          onConflict={refreshOwnership}
        />
      ) : null}
    </section>
  )
}
