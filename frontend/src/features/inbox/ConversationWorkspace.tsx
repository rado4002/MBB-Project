import { useEffect, useLayoutEffect, useRef } from 'react'
import { Link } from 'react-router-dom'
import type { ConversationApiClient } from '../../api/conversations'
import type {
  MessageSenderType,
  OperatorConversationDetail,
  OperatorMessageItem,
} from '../../api/contracts/conversations'
import { errorMessage, type ApiError } from '../../api/errors'
import { InlineAlert } from '../../components/InlineAlert'
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
  if (loading) return <p className="workspace-state" role="status">Loading conversation details…</p>
  if (error) {
    return (
      <InlineAlert requestId={error.requestId}>
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
      <div>
        <h2>{customerName}</h2>
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
    <aside className="context-panel" aria-labelledby="context-heading">
      <h3 id="context-heading">Context</h3>
      {loading ? <p role="status">Loading context…</p> : error ? (
        <p>Context is unavailable.</p>
      ) : detail?.lead ? (
        <>
          <dl className="context-details">
            <div><dt>Lead score</dt><dd>{label(detail.lead.score)}</dd></div>
            <div><dt>Lead stage</dt><dd>{label(detail.lead.stage)}</dd></div>
            <div><dt>Lead intent</dt><dd>{label(detail.lead.intent)}</dd></div>
          </dl>
          <h4>Product interests</h4>
          {detail.lead.product_interests.length ? (
            <ul className="interest-list">
              {detail.lead.product_interests.slice(0, 5).map((interest, index) => (
                <li key={`${index}-${interest}`}>{safeInterest(interest)}</li>
              ))}
            </ul>
          ) : <p>No product interests available.</p>}
        </>
      ) : (
        <p>No lead context is available.</p>
      )}
    </aside>
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
  const scrollAnchor = useRef<{ height: number; top: number } | null>(null)

  useLayoutEffect(() => {
    const timeline = timelineRef.current
    const anchor = scrollAnchor.current
    if (!timeline || !anchor || history.loadingOlder) return
    timeline.scrollTop = anchor.top + (timeline.scrollHeight - anchor.height)
    scrollAnchor.current = null
  }, [history.items.length, history.loadingOlder])

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
    <section className="timeline-panel" aria-labelledby="messages-heading">
      <h3 id="messages-heading">Messages</h3>
      {history.loading ? (
        <p className="workspace-state" role="status">Loading messages…</p>
      ) : history.error ? (
        <InlineAlert requestId={history.error.requestId}>
          {workspaceError(history.error)}
          <button className="button button--secondary alert__action" type="button" onClick={() => void history.retry()}>
            Retry messages
          </button>
        </InlineAlert>
      ) : history.items.length === 0 ? (
        <p className="workspace-state">No messages are available.</p>
      ) : (
        <div className="message-history" ref={timelineRef} role="region" aria-label="Message history">
          {history.nextOlderCursor ? (
            <button className="button button--secondary load-earlier" type="button" disabled={history.loadingOlder} onClick={loadEarlier}>
              {history.loadingOlder ? 'Loading earlier…' : 'Load Earlier'}
            </button>
          ) : null}
          {history.olderError ? (
            <InlineAlert requestId={history.olderError.requestId}>
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
  const detail = useConversationDetail(client, conversationId)
  const workspaceHeadingRef = useRef<HTMLHeadingElement>(null)

  useEffect(() => workspaceHeadingRef.current?.focus(), [conversationId])

  return (
    <section className="conversation-workspace" aria-labelledby="workspace-heading">
      <div className="workspace-toolbar">
        <Link className="button button--secondary" to={backTo}>Back to Inbox</Link>
        <h2 id="workspace-heading" className="visually-hidden" tabIndex={-1} ref={workspaceHeadingRef}>
          Selected conversation
        </h2>
      </div>
      <div className="workspace-detail-region" aria-label="Conversation details" aria-busy={detail.loading}>
        <ConversationHeader
          detail={detail.detail}
          loading={detail.loading}
          error={detail.error}
          onRetry={detail.retry}
        />
      </div>
      <div className="workspace-columns">
        <MessageTimeline client={client} conversationId={conversationId} />
        <ContextPanel detail={detail.detail} loading={detail.loading} error={detail.error} />
      </div>
    </section>
  )
}
