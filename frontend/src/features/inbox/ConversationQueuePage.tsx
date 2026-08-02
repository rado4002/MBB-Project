import { useEffect, useMemo, useRef, useState, type Ref } from 'react'
import { Link, useLocation, useParams, useSearchParams } from 'react-router-dom'
import { createConversationApiClient } from '../../api/conversations'
import {
  conversationLanguages,
  conversationStatuses,
  escalationStates,
  type ConversationFilters,
  type OperatorConversationQueueItem,
} from '../../api/contracts/conversations'
import { errorMessage, type ApiError } from '../../api/errors'
import { useAuth } from '../../auth/AuthProvider'
import { InlineAlert } from '../../components/InlineAlert'
import { ConversationWorkspace } from './ConversationWorkspace'
import { readConversationFilters, writeConversationFilters } from './filterState'
import { useConversationQueue } from './useConversationQueue'

const statusLabels = {
  active: 'Active',
  qualifying: 'Qualifying',
  nurturing: 'Nurturing',
  escalated: 'Escalated',
  converted: 'Converted',
  dormant: 'Dormant',
} as const

const languageLabels = {
  lingala: 'Lingala',
  french: 'French',
  swahili: 'Swahili',
} as const

const escalationLabels = { open: 'Open escalation', none: 'No open escalation' } as const

function formatTimestamp(value: string) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return 'Time unavailable'
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(date)
}

function previewFor(item: OperatorConversationQueueItem) {
  if (!item.latest_message) return 'No messages yet'
  if (item.latest_message.content_type === 'voice_note') return 'Voice note'
  if (item.latest_message.content_type === 'image') return 'Image'
  return item.latest_message.preview
}

function QueueRow({
  item,
  selected,
  recentlyViewed,
  search,
  linkRef,
  onSelect,
}: {
  item: OperatorConversationQueueItem
  selected: boolean
  recentlyViewed: boolean
  search: string
  linkRef: Ref<HTMLAnchorElement>
  onSelect: (conversationId: string) => void
}) {
  const customerName = item.customer.display_name?.trim() || 'Customer'
  const latestTime = item.latest_message?.occurred_at
  return (
    <li
      className={`conversation-row${selected ? ' conversation-row--selected' : ''}${recentlyViewed ? ' conversation-row--recent' : ''}`}
    >
      <Link
        className="conversation-row__link"
        to={{ pathname: `/inbox/${encodeURIComponent(item.conversation_id)}`, search }}
        aria-current={selected ? 'page' : undefined}
        ref={linkRef}
        onClick={() => onSelect(item.conversation_id)}
      >
        <article aria-label={`Conversation with ${customerName}`}>
          <div className="conversation-row__heading">
            <div>
              <h2>{customerName}</h2>
              <p className="masked-phone">{item.customer.phone_masked}</p>
            </div>
            {latestTime ? (
              <time dateTime={latestTime} aria-label={`Latest message ${formatTimestamp(latestTime)}`}>
                {formatTimestamp(latestTime)}
              </time>
            ) : null}
          </div>
          <p className="conversation-preview">{previewFor(item)}</p>
          {item.latest_message ? (
            <p className="conversation-direction">
              {item.latest_message.direction === 'inbound' ? 'Received' : 'Sent'}
            </p>
          ) : null}
          <div className="conversation-labels" aria-label="Conversation labels">
            <span>{statusLabels[item.status]}</span>
            <span>{languageLabels[item.language]}</span>
            {item.open_escalation.exists ? <span>Open escalation</span> : null}
          </div>
          {item.awaiting_response_since ? (
            <p className="awaiting-response">
              Awaiting response since{' '}
              <time dateTime={item.awaiting_response_since}>
                {formatTimestamp(item.awaiting_response_since)}
              </time>
            </p>
          ) : null}
        </article>
      </Link>
    </li>
  )
}

function queueErrorText(error: ApiError) {
  if (error.category === 'forbidden') {
    return 'You do not have permission to view the conversation queue.'
  }
  return errorMessage(error)
}

export function ConversationQueuePage() {
  const headingRef = useRef<HTMLHeadingElement>(null)
  const queueErrorRef = useRef<HTMLDivElement>(null)
  const rowLinksRef = useRef(new Map<string, HTMLAnchorElement>())
  const auth = useAuth()
  const location = useLocation()
  const { conversationId } = useParams<{ conversationId?: string }>()
  const [lastConversationId, setLastConversationId] = useState<string | undefined>(conversationId)
  const [searchParams, setSearchParams] = useSearchParams()
  const filters = useMemo(
    () => readConversationFilters(searchParams),
    [searchParams],
  )
  const normalizedSearch = useMemo(
    () => writeConversationFilters(filters).toString(),
    [filters],
  )
  const client = useMemo(
    () => createConversationApiClient(auth.handleSessionExpired),
    [auth.handleSessionExpired],
  )
  const queue = useConversationQueue(client, filters)
  const activeFilters = Object.entries(filters) as [keyof ConversationFilters, string][]

  useEffect(() => {
    if (conversationId) return
    const lastConversationLink = lastConversationId
      ? rowLinksRef.current.get(lastConversationId)
      : undefined
    if (lastConversationLink) lastConversationLink.focus()
    else headingRef.current?.focus()
  }, [conversationId, lastConversationId])
  useEffect(() => {
    if (queue.error) queueErrorRef.current?.focus()
  }, [queue.error])
  useEffect(() => {
    if (searchParams.toString() !== normalizedSearch) {
      setSearchParams(normalizedSearch, { replace: true })
    }
  }, [normalizedSearch, searchParams, setSearchParams])

  const updateFilter = (name: keyof ConversationFilters, value: string) => {
    const next = { ...filters }
    if (value) Object.assign(next, { [name]: value })
    else delete next[name]
    setSearchParams(writeConversationFilters(next))
  }

  const clearFilters = () => setSearchParams(new URLSearchParams())

  return (
    <div className={`inbox-page${conversationId ? ' inbox-page--selected' : ''}`}>
      <header className="page-header inbox-header">
        <div>
          <h1 tabIndex={-1} ref={headingRef}>Inbox</h1>
          <p>Read-only conversation queue.</p>
        </div>
        <button
          className="button button--secondary"
          type="button"
          disabled={queue.loading || queue.refreshing}
          onClick={() => void queue.refresh()}
        >
          {queue.refreshing ? 'Refreshing…' : 'Refresh'}
        </button>
      </header>

      <section className="conversation-filters" aria-labelledby="filters-heading">
        <h2 id="filters-heading">Filters</h2>
        <div className="conversation-filters__controls">
          <label>
            Status
            <select value={filters.status ?? ''} onChange={(event) => updateFilter('status', event.target.value)}>
              <option value="">All statuses</option>
              {conversationStatuses.map((status) => <option key={status} value={status}>{statusLabels[status]}</option>)}
            </select>
          </label>
          <label>
            Escalation
            <select value={filters.escalation_state ?? ''} onChange={(event) => updateFilter('escalation_state', event.target.value)}>
              <option value="">Any escalation state</option>
              {escalationStates.map((state) => <option key={state} value={state}>{escalationLabels[state]}</option>)}
            </select>
          </label>
          <label>
            Language
            <select value={filters.language ?? ''} onChange={(event) => updateFilter('language', event.target.value)}>
              <option value="">All languages</option>
              {conversationLanguages.map((language) => <option key={language} value={language}>{languageLabels[language]}</option>)}
            </select>
          </label>
        </div>
        {activeFilters.length ? (
          <div className="active-filters" aria-label="Active filters">
            <span>Active:</span>
            {filters.status ? <span className="filter-chip">Status: {statusLabels[filters.status]}</span> : null}
            {filters.escalation_state ? <span className="filter-chip">Escalation: {escalationLabels[filters.escalation_state]}</span> : null}
            {filters.language ? <span className="filter-chip">Language: {languageLabels[filters.language]}</span> : null}
            <button className="button button--text" type="button" onClick={clearFilters}>Clear Filters</button>
          </div>
        ) : null}
      </section>

      <div className={`inbox-layout${conversationId ? ' inbox-layout--selected' : ''}`}>
        <section className="queue-panel" aria-labelledby="queue-heading" aria-busy={queue.loading || queue.refreshing}>
          <h2 id="queue-heading" className="visually-hidden">Conversation queue</h2>
          {queue.refreshing ? <p className="queue-status" role="status">Refreshing conversations…</p> : null}
          {queue.error ? (
            <InlineAlert ref={queueErrorRef} requestId={queue.error.requestId}>
              {queueErrorText(queue.error)}
              <button className="button button--secondary alert__action" type="button" onClick={() => void queue.retry()}>
                Try again
              </button>
            </InlineAlert>
          ) : null}
          {queue.loading ? (
            <div className="queue-loading" role="status">
              <span className="visually-hidden">Loading conversations…</span>
              <div className="skeleton-list" aria-hidden="true">
                <span className="skeleton-row" />
                <span className="skeleton-row" />
                <span className="skeleton-row" />
              </div>
            </div>
          ) : queue.items.length === 0 && !queue.error ? (
            <div className="queue-state">
              <h3>{activeFilters.length ? 'No conversations match these filters' : 'No conversations are available'}</h3>
              <p>{activeFilters.length ? 'Clear or change a filter to see other conversations.' : 'There are no conversations to show.'}</p>
              {activeFilters.length ? <button className="button button--secondary" type="button" onClick={clearFilters}>Clear Filters</button> : null}
            </div>
          ) : queue.items.length > 0 ? (
            <>
              <ul className="conversation-list">
                {queue.items.map((item) => (
                  <QueueRow
                    key={item.conversation_id}
                    item={item}
                    selected={item.conversation_id === conversationId}
                    recentlyViewed={!conversationId && item.conversation_id === lastConversationId}
                    search={location.search}
                    linkRef={(node) => {
                      if (node) rowLinksRef.current.set(item.conversation_id, node)
                      else rowLinksRef.current.delete(item.conversation_id)
                    }}
                    onSelect={setLastConversationId}
                  />
                ))}
              </ul>
              {queue.nextCursor ? (
                <div className="load-more">
                  <button className="button button--secondary" type="button" disabled={queue.loadingMore} onClick={() => void queue.loadMore()}>
                    {queue.loadingMore ? 'Loading more…' : 'Load More'}
                  </button>
                </div>
              ) : null}
            </>
          ) : null}
        </section>
        {conversationId ? (
          <ConversationWorkspace
            key={conversationId}
            client={client}
            conversationId={conversationId}
            backTo={`/inbox${location.search}`}
          />
        ) : (
          <aside className="queue-workspace" aria-label="Conversation workspace">
            <div>
              <h2>No conversation selected</h2>
              <p>Choose a conversation from the queue to read its history and limited context.</p>
            </div>
          </aside>
        )}
      </div>
    </div>
  )
}
