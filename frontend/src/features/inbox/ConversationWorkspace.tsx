import {
  Fragment,
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type FormEvent,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
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
import { ConversationAuthority } from './ConversationAuthority'
import { replyUnavailableReason } from './authorityPolicy'
import { OwnershipDialog } from './OwnershipDialog'
import { EscalationForm } from './EscalationForm'
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

function formatDay(value: string) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return 'Date unavailable'
  if (date.toDateString() === new Date().toDateString()) return "Aujourd'hui"
  return new Intl.DateTimeFormat(undefined, { dateStyle: 'long' }).format(date)
}

function initials(name: string) {
  return name.split(/\s+/).slice(0, 2).map((part) => part[0]).join('').toUpperCase()
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

function actorLabel(senderType: MessageSenderType) {
  switch (senderType) {
    case 'customer':
      return 'Customer'
    case 'operator':
      return 'Operator'
    case 'ai':
      return 'MBB AI Assistant'
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
  if (message.sender_type === 'ai') {
    return message.sender_display_name || 'MBB AI Assistant'
  }
  if (message.sender_type === 'unknown' && message.direction === 'outbound') {
    return 'Outbound message'
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
  authority,
}: {
  detail: OperatorConversationDetail | null
  loading: boolean
  error: ApiError | null
  onRetry: () => Promise<void>
  headingRef: RefObject<HTMLHeadingElement | null>
  authority: ReactNode
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
    <div className="conversation-header__content conversation-header__content--loaded">
      <div className="conversation-identity">
        <span className="conversation-avatar conversation-avatar--header" aria-hidden="true">{initials(customerName)}</span>
        <div>
          <h2 id="workspace-heading" tabIndex={-1} ref={headingRef}>{customerName}</h2>
          <p className="masked-phone">{detail.customer.phone_masked}</p>
        </div>
      </div>
      <p className="visually-hidden" role="status">Conversation details loaded.</p>
      {authority}
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
  const commercial = detail.commercial_context
  const lead = detail.lead
  const hasMoreContext = Boolean(
    detail.open_escalation.reason ||
    (commercial && (
      commercial.purchase_intent !== 'none' || commercial.next_objective ||
      commercial.expressed_needs.length || commercial.decision_constraints.length ||
      commercial.current_concern || commercial.selected_products.length
    )) ||
    (lead && (lead.score || lead.stage || lead.intent || lead.product_interests.length)),
  )
  return (
    <>
      <div className="context-client">
        <span className="conversation-avatar" aria-hidden="true">{initials(detail.customer.display_name?.trim() || 'Customer')}</span>
        <div><strong>{detail.customer.display_name?.trim() || 'Customer'}</strong><p>{detail.customer.phone_masked}</p></div>
      </div>
      <dl className="context-details context-details--primary">
        <div><dt>Language</dt><dd>{label(detail.language)}</dd></div>
        <div><dt>Last activity</dt><dd><time dateTime={detail.updated_at}>{formatTimestamp(detail.updated_at)}</time></dd></div>
        <div><dt>Control</dt><dd>{detail.ownership.owner_type === 'ai' ? 'MBB AI Assistant' : detail.ownership.human_owner?.display_name || 'Human Operator'}</dd></div>
        {detail.open_escalation.exists ? <div><dt>Review</dt><dd>Open escalation ticket</dd></div> : null}
        {commercial?.current_goal ? <div><dt>Current goal</dt><dd>{commercial.current_goal}</dd></div> : null}
      </dl>
      {hasMoreContext ? (
        <details className="context-more">
          <summary>Voir plus de contexte</summary>
          <div className="context-more__body">
            <dl className="context-details">
              {detail.open_escalation.reason ? (
                <div><dt>Handoff reason</dt><dd>{label(detail.open_escalation.reason)}</dd></div>
              ) : null}
              {commercial?.purchase_intent && commercial.purchase_intent !== 'none' ? (
                <div><dt>Purchase intent</dt><dd>{label(commercial.purchase_intent)}</dd></div>
              ) : null}
              {commercial?.next_objective ? <div><dt>Next objective</dt><dd>{label(commercial.next_objective)}</dd></div> : null}
              {commercial?.expressed_needs.length ? <div><dt>Needs</dt><dd>{commercial.expressed_needs.join(', ')}</dd></div> : null}
              {commercial?.decision_constraints.length ? (
                <div><dt>Constraints</dt><dd>{commercial.decision_constraints
                  .map((constraint) => `${label(constraint.kind)}: ${constraint.value}`)
                  .join(', ')}</dd></div>
              ) : null}
              {commercial?.current_concern ? (
                <div><dt>Concern</dt><dd>{label(commercial.current_concern.kind)}
                  {commercial.current_concern.detail ? `: ${commercial.current_concern.detail}` : ''}</dd></div>
              ) : null}
              {lead?.score ? <div><dt>Lead score</dt><dd>{label(lead.score)}</dd></div> : null}
              {lead?.stage ? <div><dt>Lead stage</dt><dd>{label(lead.stage)}</dd></div> : null}
              {lead?.intent ? <div><dt>Lead intent</dt><dd>{label(lead.intent)}</dd></div> : null}
            </dl>
            {commercial?.selected_products.length ? (
              <>
                <ProductHeading>Selected products</ProductHeading>
                <ul className="interest-list">
                  {commercial.selected_products.map((product) => (
                    <li key={product.sellable_item_id}>
                      {product.display_name || 'Unavailable selected item'}
                      {product.offer_status ? ` — ${label(product.offer_status)}` : ''}
                      {product.current_usd_price ? ` — $${product.current_usd_price}` : ''}
                    </li>
                  ))}
                </ul>
              </>
            ) : null}
            {lead?.product_interests.length ? (
              <>
                <ProductHeading>Product interests</ProductHeading>
                <ul className="interest-list">
                  {lead.product_interests.slice(0, 5).map((interest, index) => (
                    <li key={`${index}-${interest}`}>{safeInterest(interest)}</li>
                  ))}
                </ul>
              </>
            ) : null}
          </div>
        </details>
      ) : null}
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
  replyReason,
  canReply,
  onAuthorityConflict,
  onReplyAccepted,
}: {
  client: ConversationApiClient
  conversationId: string
  detail: OperatorConversationDetail | null
  replyReason: string | null
  canReply: boolean
  onAuthorityConflict: () => Promise<void>
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
            {history.items.map((item, index) => {
              const previous = history.items[index - 1]
              const day = formatDay(item.occurred_at)
              const dateSeparator = !previous || formatDay(previous.occurred_at) !== day
                ? <li className="message-day"><time dateTime={item.occurred_at}>{day}</time></li>
                : null
              if (item.kind === 'internal_note') {
                return (
                  <Fragment key={timelineItemKey(item)}>
                    {dateSeparator}
                    <li className="internal-note">
                      <article aria-label={`Internal note by ${item.author.display_name}`}>
                        <header>
                          <strong>
                            Internal Note
                            <span className="internal-note__author"> · {item.author.display_name} — Operator</span>
                          </strong>
                          <time dateTime={item.occurred_at}>{formatTimestamp(item.occurred_at)}</time>
                        </header>
                        <p className="message-text">{item.text}</p>
                      </article>
                    </li>
                  </Fragment>
                )
              }
              const message = item
              const actor = messageActorLabel(message)
              const delivery = deliveryLabel(message.delivery_state)
              return (
                <Fragment key={timelineItemKey(message)}>
                  {dateSeparator}
                  <li className={`message message--${message.direction}${message.sender_type === 'system' ? ' message--system' : ''}`}>
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
                </Fragment>
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
          onAuthorityConflict={onAuthorityConflict}
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
  onAuthorityConflict,
  onReplyAccepted,
  onNoteAccepted,
}: {
  client: ConversationApiClient
  conversationId: string
  expectedOwnershipVersion: number | null
  canReply: boolean
  canCreateNote: boolean
  replyUnavailableReason: string | null
  onAuthorityConflict: () => Promise<void>
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
      if (
        activeMode === 'reply' &&
        (apiError?.code === 'OWNERSHIP_CONFLICT' ||
          apiError?.code === 'OWNERSHIP_VERSION_CONFLICT')
      ) {
        await onAuthorityConflict()
      }
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
          {requestError.operatorMessage ?? errorMessage(requestError)} Your {activeMode === 'reply' ? 'reply' : 'internal note'} has been preserved.
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
  const authorityRef = useRef<HTMLDivElement>(null)
  const detailsButtonRef = useRef<HTMLButtonElement>(null)
  const ownershipButtonRef = useRef<HTMLButtonElement>(null)
  const [contextOpen, setContextOpen] = useState(false)
  const [ownershipOpen, setOwnershipOpen] = useState(false)
  const [ownershipAtOpen, setOwnershipAtOpen] =
    useState<ConversationOwnership | null>(null)
  const [authorityFocusRequest, setAuthorityFocusRequest] = useState(0)
  const [authorityNotice, setAuthorityNotice] = useState('')
  const closeContext = useCallback(() => setContextOpen(false), [])
  const closeOwnership = useCallback(() => setOwnershipOpen(false), [])
  const ownership = detail.detail?.ownership
  const hasReplyCapability = Boolean(
    auth.session?.capabilities.includes('message.reply'),
  )
  const hasOwnershipCapability = Boolean(
    auth.session?.capabilities.includes('conversation.ownership.change'),
  )
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
      hasOwnershipCapability &&
      (ownership.owner_type === 'ai' || mayReturnOwnedConversation),
  )
  const currentReplyReason = replyUnavailableReason(
    detail.detail,
    auth.session?.human.account_id,
    hasReplyCapability,
  )

  const openOwnership = () => {
    if (!ownership) return
    setOwnershipAtOpen(ownership)
    setOwnershipOpen(true)
  }
  const handleOwnershipChanged = async (
    result: { ownership: ConversationOwnership },
  ) => {
    detail.applyOwnership(result.ownership)
    setAuthorityFocusRequest((current) => current + 1)
    setAuthorityNotice(
      result.ownership.owner_type === 'human'
        ? `${result.ownership.human_owner?.display_name ?? 'A Human Operator'} now controls this conversation. AI is paused.`
        : 'MBB AI Assistant now controls this conversation. AI is active.',
    )
    void onOwnershipChanged().catch(() => undefined)
  }
  const refreshOwnership = async () => {
    await Promise.all([detail.refresh(), onOwnershipChanged()])
  }
  const reconcileAuthority = async () => {
    await refreshOwnership()
    setAuthorityNotice('Conversation authority changed on the server. Current authority is shown.')
  }
  const reconcileOwnershipDialogConflict = async () => {
    await reconcileAuthority()
    setAuthorityFocusRequest((current) => current + 1)
  }

  useEffect(() => {
    if (
      detail.loading ||
      focusedConversationRef.current === conversationId ||
      !workspaceHeadingRef.current
    ) return
    const activeElement = document.activeElement
    if (
      activeElement !== document.body &&
      !activeElement?.closest('.conversation-row')
    ) return
    workspaceHeadingRef.current.focus()
    focusedConversationRef.current = conversationId
  }, [conversationId, detail.loading])
  useEffect(() => {
    if (authorityFocusRequest > 0 && !ownershipOpen) {
      authorityRef.current?.focus()
    }
  }, [authorityFocusRequest, ownershipOpen])

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
          authority={detail.detail ? (
            <ConversationAuthority
              detail={detail.detail}
              accountId={auth.session?.human.account_id}
              hasOwnershipCapability={hasOwnershipCapability}
              mayReturnOwnedConversation={mayReturnOwnedConversation}
              canChangeOwnership={canChangeOwnership}
              ownershipOpen={ownershipOpen}
              notice={authorityNotice}
              authorityRef={authorityRef}
              actionRef={ownershipButtonRef}
              onAction={openOwnership}
            />
          ) : null}
        />
        <div className="workspace-toolbar__actions">
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
        <EscalationForm
          key={conversationId}
          client={client}
          conversationId={conversationId}
          available={Boolean(detail.detail && !detail.loading && !detail.error)}
          hasOpenEscalation={Boolean(detail.detail?.open_escalation.exists)}
          onRefresh={refreshOwnership}
        />
      </header>
      <div className="workspace-columns">
        <MessageTimeline
          client={client}
          conversationId={conversationId}
          detail={detail.detail}
          replyReason={currentReplyReason}
          canReply={currentReplyReason === null}
          onAuthorityConflict={reconcileAuthority}
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
          onConflict={reconcileOwnershipDialogConflict}
        />
      ) : null}
    </section>
  )
}
