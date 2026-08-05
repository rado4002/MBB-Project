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
  OperatorMessageItem,
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

function ownershipLabel(ownership: ConversationOwnership) {
  return ownership.owner_type === 'ai'
    ? 'MBB AI Assistant'
    : ownership.human_owner?.display_name ?? 'Human Operator'
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
  ownershipRef,
}: {
  detail: OperatorConversationDetail | null
  loading: boolean
  error: ApiError | null
  onRetry: () => Promise<void>
  ownershipRef: RefObject<HTMLParagraphElement | null>
}) {
  const errorRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (error) errorRef.current?.focus()
  }, [error])

  if (loading) return <DetailLoadingState />
  if (error) {
    return (
      <InlineAlert ref={errorRef} requestId={error.requestId}>
        {workspaceError(error)}
        <button className="button button--secondary alert__action" type="button" onClick={() => void onRetry()}>
          Retry details
        </button>
      </InlineAlert>
    )
  }
  if (!detail) return null
  const customerName = detail.customer.display_name?.trim() || 'Customer'
  return (
    <div className="workspace-summary">
      <p className="visually-hidden" role="status">Conversation details loaded.</p>
      <div>
        <h3>{customerName}</h3>
        <p className="masked-phone">{detail.customer.phone_masked}</p>
      </div>
      <div className="conversation-labels" aria-label="Conversation attributes">
        <span>{label(detail.status)}</span>
        <span>{label(detail.language)}</span>
        <span>{detail.message_count} messages</span>
        {detail.open_escalation.exists ? <span>Open escalation</span> : null}
      </div>
      <p
        className="ownership-summary"
        ref={ownershipRef}
        tabIndex={-1}
        aria-live="polite"
      >
        <strong>Controlled by {ownershipLabel(detail.ownership)}</strong>
        {detail.ownership.owner_type === 'human' ? <span>AI paused</span> : null}
      </p>
      <p className="workspace-updated">
        Last updated <time dateTime={detail.updated_at}>{formatTimestamp(detail.updated_at)}</time>
      </p>
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
        <div>
          <dt>Conversation control</dt>
          <dd>
            Controlled by {ownershipLabel(detail.ownership)}
            {detail.ownership.owner_type === 'human' ? ' — AI paused' : ''}
          </dd>
        </div>
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

  const canReply = Boolean(
    detail &&
      ['active', 'qualifying', 'nurturing', 'escalated'].includes(detail.status) &&
      detail.ownership.owner_type === 'human' &&
      detail.ownership.ai_execution_state === 'paused' &&
      detail.ownership.human_owner?.account_id === auth.session?.human.account_id &&
      auth.session?.capabilities.includes('message.reply'),
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
    const lastMessageId = history.items.at(-1)?.message_id ?? null
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
      <h3 id="messages-heading">Messages</h3>
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
        <p className="workspace-state">No messages are available.</p>
      ) : (
        <div className="message-history" ref={timelineRef} role="region" aria-label="Message history" tabIndex={0}>
          <p className="visually-hidden" role="status">{history.items.length} messages loaded.</p>
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
            {history.items.map((message) => {
              const actor = messageActorLabel(message)
              const delivery = deliveryLabel(message.delivery_state)
              return (
                <li key={message.message_id} className={`message message--${message.direction}`}>
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
      {canReply && detail ? (
        <ReplyComposer
          client={client}
          conversationId={conversationId}
          expectedOwnershipVersion={detail.ownership.version}
          onAccepted={(message) => {
            history.appendAccepted(message)
            return onReplyAccepted()
          }}
        />
      ) : null}
    </section>
  )
}

function ReplyComposer({
  client,
  conversationId,
  expectedOwnershipVersion,
  onAccepted,
}: {
  client: ConversationApiClient
  conversationId: string
  expectedOwnershipVersion: number
  onAccepted: (message: OperatorMessageItem) => Promise<void>
}) {
  const auth = useAuth()
  const [text, setText] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [validationError, setValidationError] = useState<string | null>(null)
  const [requestError, setRequestError] = useState<ApiError | null>(null)
  const [acceptedAnnouncement, setAcceptedAnnouncement] = useState('')
  const submittingRef = useRef(false)
  const focusAfterAcceptanceRef = useRef(false)
  const attemptRef = useRef<{ text: string; key: string } | null>(null)
  const errorRef = useRef<HTMLDivElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const helpId = useId()
  const errorId = useId()

  useEffect(() => {
    if (requestError) errorRef.current?.focus()
  }, [requestError])

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
      setValidationError('Enter a reply before submitting.')
      textareaRef.current?.focus()
      return
    }
    if (Array.from(text).length > 4096) {
      setValidationError('Reply text must be 4,096 characters or fewer.')
      textareaRef.current?.focus()
      return
    }

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
      const message = await client.createReply(
        conversationId,
        { text, expected_ownership_version: expectedOwnershipVersion },
        attempt.key,
        csrfToken,
      )
      historyAssertAccepted(message)
      setText('')
      attemptRef.current = null
      setAcceptedAnnouncement('Reply accepted and added to the timeline.')
      focusAfterAcceptanceRef.current = true
      void onAccepted(message).catch(() => undefined)
    } catch (unknownError) {
      setRequestError(unknownError instanceof ApiError ? unknownError : null)
      if (!(unknownError instanceof ApiError)) {
        setRequestError(new ApiError({
          status: 0,
          code: 'reply_unavailable',
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
    <form className="reply-composer" onSubmit={(event) => void submit(event)}>
      <label htmlFor={`${helpId}-reply`}>Reply to Customer</label>
      {requestError ? (
        <InlineAlert ref={errorRef} requestId={requestError.requestId}>
          {errorMessage(requestError)} Your reply has been preserved.
        </InlineAlert>
      ) : null}
      <textarea
        id={`${helpId}-reply`}
        ref={textareaRef}
        value={text}
        maxLength={4096}
        rows={3}
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
        <span id={helpId}>{Array.from(text).length}/4,096 · Ctrl+Enter to submit</span>
        <button className="button button--primary" type="submit" disabled={submitting}>
          {submitting ? 'Submitting…' : 'Submit Reply'}
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
  const ownershipStatusRef = useRef<HTMLParagraphElement>(null)
  const detailsButtonRef = useRef<HTMLButtonElement>(null)
  const ownershipButtonRef = useRef<HTMLButtonElement>(null)
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

  useEffect(() => workspaceHeadingRef.current?.focus(), [conversationId])
  useEffect(() => {
    if (successfulVersion !== null && !ownershipOpen) {
      ownershipStatusRef.current?.focus()
    }
  }, [ownershipOpen, successfulVersion])

  return (
    <section className="conversation-workspace" aria-labelledby="workspace-heading">
      <div className="workspace-header">
        <div className="workspace-toolbar">
          <Link className="button button--secondary" to={backTo} aria-label="Back to Inbox">
            <span className="back-label back-label--long" aria-hidden="true">Back to Inbox</span>
            <span className="back-label back-label--short" aria-hidden="true">Back</span>
          </Link>
          <h2 id="workspace-heading" tabIndex={-1} ref={workspaceHeadingRef}>Conversation</h2>
          <div className="workspace-toolbar__actions">
            {canChangeOwnership ? (
              <button
                className="button button--primary"
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
        </div>
        <div className="workspace-detail-region" aria-label="Conversation details" aria-busy={detail.loading}>
          <ConversationHeader
            detail={detail.detail}
            loading={detail.loading}
            error={detail.error}
            onRetry={detail.retry}
            ownershipRef={ownershipStatusRef}
          />
        </div>
      </div>
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
