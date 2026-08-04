import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type RefObject,
} from 'react'
import { createPortal } from 'react-dom'
import { Link } from 'react-router-dom'
import type { ConversationApiClient } from '../../api/conversations'
import type {
  MessageSenderType,
  OperatorConversationDetail,
  OperatorMessageItem,
} from '../../api/contracts/conversations'
import type { OperatorEscalationResponse } from '../../api/contracts/escalations'
import { errorMessage, type ApiError } from '../../api/errors'
import { useAuth } from '../../auth/AuthProvider'
import { InlineAlert } from '../../components/InlineAlert'
import { EscalationDialog } from './EscalationDialog'
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
}: {
  detail: OperatorConversationDetail | null
  loading: boolean
  error: ApiError | null
  onRetry: () => Promise<void>
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
      <p className="workspace-updated">
        Last updated <time dateTime={detail.updated_at}>{formatTimestamp(detail.updated_at)}</time>
      </p>
    </div>
  )
}

function EscalationResultCard({
  result,
  headingRef,
}: {
  result: OperatorEscalationResponse
  headingRef: RefObject<HTMLHeadingElement | null>
}) {
  return (
    <section className="escalation-result" aria-labelledby="escalation-result-heading">
      <div className="escalation-result__heading">
        <div>
          <p className="eyebrow">Authoritative escalation</p>
          <h3 id="escalation-result-heading" ref={headingRef} tabIndex={-1}>Escalation created</h3>
        </div>
        <span className="status-pill">{label(result.status)}</span>
      </div>
      <dl className="escalation-result__details">
        <div><dt>Type</dt><dd>{label(result.type)}</dd></div>
        <div><dt>Priority</dt><dd>{label(result.priority)}</dd></div>
        <div><dt>Created by</dt><dd>{result.created_by.display_name}</dd></div>
        <div>
          <dt>Created</dt>
          <dd><time dateTime={result.created_at}>{formatTimestamp(result.created_at)}</time></dd>
        </div>
        <div className="escalation-result__reason"><dt>Reason</dt><dd>{result.reason}</dd></div>
        <div className="escalation-result__reference"><dt>Reference</dt><dd><code>{result.escalation_id}</code></dd></div>
      </dl>
      <p className="escalation-result__note">
        Conversation state and routing were not changed.
      </p>
    </section>
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
  if (!detail?.lead) return <p>No lead context is available.</p>
  const ProductHeading = productHeadingLevel
  return (
    <>
      <dl className="context-details">
        <div><dt>Lead score</dt><dd>{label(detail.lead.score)}</dd></div>
        <div><dt>Lead stage</dt><dd>{label(detail.lead.stage)}</dd></div>
        <div><dt>Lead intent</dt><dd>{label(detail.lead.intent)}</dd></div>
      </dl>
      <ProductHeading>Product interests</ProductHeading>
      {detail.lead.product_interests.length ? (
        <ul className="interest-list">
          {detail.lead.product_interests.slice(0, 5).map((interest, index) => (
            <li key={`${index}-${interest}`}>{safeInterest(interest)}</li>
          ))}
        </ul>
      ) : <p>No product interests available.</p>}
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
}: {
  client: ConversationApiClient
  conversationId: string
}) {
  const history = useMessageHistory(client, conversationId)
  const timelineRef = useRef<HTMLDivElement>(null)
  const initialPositioned = useRef(false)
  const scrollAnchor = useRef<{ height: number; top: number } | null>(null)
  const errorRef = useRef<HTMLDivElement>(null)
  const olderErrorRef = useRef<HTMLDivElement>(null)

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
              const actor = actorLabel(message.sender_type)
              return (
                <li key={message.message_id} className={`message message--${message.direction}`}>
                  <article aria-label={`${actor} message`}>
                    <header>
                      <strong>{actor}</strong>
                      <time dateTime={message.occurred_at}>{formatTimestamp(message.occurred_at)}</time>
                    </header>
                    {messageContent(message)}
                  </article>
                </li>
              )
            })}
          </ol>
        </div>
      )}
    </section>
  )
}

export function ConversationWorkspace({
  client,
  conversationId,
  backTo,
}: {
  client: ConversationApiClient
  conversationId: string
  backTo: string
}) {
  const auth = useAuth()
  const detail = useConversationDetail(client, conversationId)
  const workspaceHeadingRef = useRef<HTMLHeadingElement>(null)
  const escalationResultHeadingRef = useRef<HTMLHeadingElement>(null)
  const detailsButtonRef = useRef<HTMLButtonElement>(null)
  const escalationButtonRef = useRef<HTMLButtonElement>(null)
  const [contextOpen, setContextOpen] = useState(false)
  const [escalationOpen, setEscalationOpen] = useState(false)
  const [createdEscalation, setCreatedEscalation] =
    useState<OperatorEscalationResponse | null>(null)
  const [alreadyOpenConversationId, setAlreadyOpenConversationId] =
    useState<string | null>(null)
  const closeContext = useCallback(() => setContextOpen(false), [])
  const closeEscalation = useCallback(() => setEscalationOpen(false), [])

  const currentCreatedEscalation =
    createdEscalation?.conversation_id === conversationId
      ? createdEscalation
      : null
  const alreadyOpen = alreadyOpenConversationId === conversationId
  const canEscalate = Boolean(
    detail.detail &&
      !detail.loading &&
      !detail.error &&
      !detail.detail.open_escalation.exists &&
      !currentCreatedEscalation &&
      !alreadyOpen &&
      auth.session?.capabilities.includes('escalation.create'),
  )

  const handleCreated = async (result: OperatorEscalationResponse) => {
    setCreatedEscalation(result)
    setAlreadyOpenConversationId(null)
    await detail.refresh()
  }

  const handleAlreadyOpen = async () => {
    setAlreadyOpenConversationId(conversationId)
    await detail.refresh()
  }

  useEffect(() => workspaceHeadingRef.current?.focus(), [conversationId])
  useEffect(() => {
    if (currentCreatedEscalation && !escalationOpen) {
      escalationResultHeadingRef.current?.focus()
    }
  }, [currentCreatedEscalation, escalationOpen])

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
            {canEscalate ? (
              <button
                className="button button--primary"
                type="button"
                aria-haspopup="dialog"
                aria-expanded={escalationOpen}
                onClick={() => setEscalationOpen(true)}
                ref={escalationButtonRef}
              >
                Escalate
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
          />
          {alreadyOpen ? (
            <InlineAlert tone="warning">
              This conversation already has an active escalation. Conversation details were refreshed.
            </InlineAlert>
          ) : null}
          {currentCreatedEscalation ? (
            <EscalationResultCard
              result={currentCreatedEscalation}
              headingRef={escalationResultHeadingRef}
            />
          ) : null}
        </div>
      </div>
      <div className="workspace-columns">
        <MessageTimeline client={client} conversationId={conversationId} />
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
      <EscalationDialog
        key={conversationId}
        open={escalationOpen}
        conversationId={conversationId}
        client={client}
        returnFocusRef={escalationButtonRef}
        onClose={closeEscalation}
        onCreated={handleCreated}
        onAlreadyOpen={handleAlreadyOpen}
      />
    </section>
  )
}
