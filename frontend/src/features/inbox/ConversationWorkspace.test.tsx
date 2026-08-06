import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { delay, http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { expectAccessible } from '../../test/accessibility'
import {
  conversationDetailFixture,
  conversationFixture,
  internalNoteFixture,
  messageFixture,
  sessionFixture,
} from '../../test/fixtures'
import { renderApp } from '../../test/renderApp'
import { server } from '../../test/server'

const firstId = '11111111-1111-4111-8111-111111111111'
const secondId = '22222222-2222-4222-8222-222222222222'

function authenticated() {
  return http.get('/api/v1/auth/session', () =>
    HttpResponse.json(sessionFixture()),
  )
}

function successfulWorkspace() {
  return [
    http.get('/api/v1/operator/conversations/:conversationId', ({ params }) =>
      HttpResponse.json(conversationDetailFixture(String(params.conversationId))),
    ),
    http.get('/api/v1/operator/conversations/:conversationId/timeline', () =>
      HttpResponse.json({ items: [messageFixture()], next_older_cursor: null }),
    ),
  ]
}

describe('read-only conversation workspace', () => {
  it('navigates from an accessible selected row and preserves supported filters', async () => {
    server.use(
      authenticated(),
      http.get('/api/v1/operator/conversations', () =>
        HttpResponse.json({ items: [conversationFixture()], next_cursor: null }),
      ),
      ...successfulWorkspace(),
    )
    const user = userEvent.setup()
    renderApp('/inbox?status=active&language=french')

    const link = await screen.findByRole('link', {
      name: 'Conversation with Marie Client',
    })
    link.focus()
    expect(link).toHaveFocus()
    await user.keyboard('{Enter}')

    await waitFor(() =>
      expect(window.location.pathname).toBe(`/inbox/${firstId}`),
    )
    expect(window.location.search).toBe('?status=active&language=french')
    expect(link).toHaveAttribute('aria-current', 'page')
    expect(await screen.findByRole('heading', { name: 'Timeline' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Conversation' })).toHaveFocus()
  })

  it('supports direct deep links, Back, Forward, and the progressive Back to Inbox action', async () => {
    server.use(
      authenticated(),
      http.get('/api/v1/operator/conversations', () =>
        HttpResponse.json({ items: [conversationFixture()], next_cursor: null }),
      ),
      ...successfulWorkspace(),
    )
    const user = userEvent.setup()
    renderApp(`/inbox/${firstId}?escalation_state=open`)

    expect(await screen.findByText('Solar starter kit')).toBeInTheDocument()
    expect(window.location.search).toBe('?escalation_state=open')
    expect(document.title).toBe('Inbox · MBB')
    await user.click(screen.getByRole('link', { name: 'Back to Inbox' }))
    await waitFor(() => expect(window.location.pathname).toBe('/inbox'))
    expect(window.location.search).toBe('?escalation_state=open')

    window.history.back()
    fireEvent(window, new PopStateEvent('popstate'))
    await waitFor(() => expect(window.location.pathname).toBe(`/inbox/${firstId}`))
    expect(await screen.findByRole('heading', { name: 'Timeline' })).toBeInTheDocument()

    window.history.forward()
    fireEvent(window, new PopStateEvent('popstate'))
    await waitFor(() => expect(window.location.pathname).toBe('/inbox'))
  })

  it('loads detail independently and displays only masked, limited contract context', async () => {
    const detail = {
      ...conversationDetailFixture(),
      customer: { display_name: 'Marie Client', phone_masked: '***5678' },
      lead: {
        score: 'high',
        stage: 'sales_ready',
        intent: 'request_quote',
        product_interests: [
          'Solar kit',
          'Battery storage',
          'A'.repeat(100),
          'Installation',
          'Maintenance',
        ],
      },
      open_escalation: { exists: true },
    }
    server.use(
      authenticated(),
      http.get('/api/v1/operator/conversations/:conversationId', async () => {
        await delay(80)
        return HttpResponse.json(detail)
      }),
      http.get('/api/v1/operator/conversations/:conversationId/timeline', () =>
        HttpResponse.json({ items: [], next_older_cursor: null }),
      ),
    )
    renderApp(`/inbox/${firstId}`)

    expect(await screen.findByText('Loading conversation details…')).toBeInTheDocument()
    expect(await screen.findByText('***5678')).toBeInTheDocument()
    expect(screen.getByText('Lead score').nextElementSibling).toHaveTextContent('High')
    expect(screen.getByText('Sales Ready')).toBeInTheDocument()
    expect(screen.getByText('Request Quote')).toBeInTheDocument()
    expect(
      screen.getAllByText('Open escalation').some((node) => node.tagName === 'SPAN'),
    ).toBe(true)
    expect(screen.getByText('A'.repeat(80))).toBeInTheDocument()
    expect(screen.queryByText('A'.repeat(100))).not.toBeInTheDocument()
    expect(screen.queryByText('+243990005678')).not.toBeInTheDocument()
    expect(screen.queryByText(firstId)).not.toBeInTheDocument()
    expect(screen.queryByText(/city|consent|opt-out|raw context/i)).not.toBeInTheDocument()
  })

  it('loads history independently without hiding available conversation details', async () => {
    server.use(
      authenticated(),
      http.get('/api/v1/operator/conversations/:conversationId', () =>
        HttpResponse.json(conversationDetailFixture()),
      ),
      http.get('/api/v1/operator/conversations/:conversationId/timeline', async () => {
        await delay(80)
        return HttpResponse.json({ items: [messageFixture()], next_older_cursor: null })
      }),
    )
    renderApp(`/inbox/${firstId}`)

    expect(await screen.findByText('Solar starter kit')).toBeInTheDocument()
    expect(screen.getByText('Loading messages…')).toBeInTheDocument()
    expect(await screen.findByText('Bonjour, je souhaite des informations.')).toBeInTheDocument()
  })

  it('renders chronological plain-text messages, authoritative actors, and local media placeholders', async () => {
    const messages = [
      messageFixture('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', {
        occurred_at: '2026-08-02T10:00:00Z',
        text: '<script>alert(document.cookie)</script>',
      }),
      messageFixture('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', {
        occurred_at: '2026-08-02T11:00:00Z',
        direction: 'outbound',
        sender_type: 'unknown',
        text: 'Legacy outbound',
      }),
      messageFixture('cccccccc-cccc-4ccc-8ccc-cccccccccccc', {
        occurred_at: '2026-08-02T12:00:00Z',
        direction: 'outbound',
        sender_type: 'operator',
        text: 'Operator response',
      }),
      messageFixture('dddddddd-dddd-4ddd-8ddd-dddddddddddd', {
        occurred_at: '2026-08-02T12:15:00Z',
        sender_type: 'system',
        content_type: 'voice_note',
        text: null,
        media: { kind: 'voice_note', available: false },
      }),
      messageFixture('eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee', {
        occurred_at: '2026-08-02T12:20:00Z',
        content_type: 'image',
        text: null,
        media: { kind: 'image', available: false },
      }),
    ]
    server.use(
      authenticated(),
      http.get('/api/v1/operator/conversations/:conversationId', () =>
        HttpResponse.json(conversationDetailFixture()),
      ),
      http.get('/api/v1/operator/conversations/:conversationId/timeline', () =>
        HttpResponse.json({ items: messages, next_older_cursor: null }),
      ),
    )
    const { container } = renderApp(`/inbox/${firstId}`)

    const history = await screen.findByRole('region', { name: 'Conversation timeline' })
    const rendered = within(history).getAllByRole('article')
    expect(rendered.map((item) => item.textContent)).toEqual([
      expect.stringContaining('<script>alert(document.cookie)</script>'),
      expect.stringContaining('Unknown sender'),
      expect.stringContaining('Operator'),
      expect.stringContaining('System'),
      expect.stringContaining('Image unavailable'),
    ])
    expect(within(history).getByText('Voice note unavailable')).toBeInTheDocument()
    expect(container.querySelector('script')).toBeNull()
    expect(container.querySelector('a[href^="http"]')).toBeNull()
    expect(screen.queryByText(/delivery status|authored by human/i)).not.toBeInTheDocument()
    expect(screen.getAllByText('Controlled by MBB AI Assistant')).toHaveLength(2)
  })

  it('shows a truthful empty history without hiding loaded detail', async () => {
    server.use(
      authenticated(),
      http.get('/api/v1/operator/conversations/:conversationId', () =>
        HttpResponse.json(conversationDetailFixture()),
      ),
      http.get('/api/v1/operator/conversations/:conversationId/timeline', () =>
        HttpResponse.json({ items: [], next_older_cursor: null }),
      ),
    )
    renderApp(`/inbox/${firstId}`)

    expect(await screen.findByText('No timeline items are available.')).toBeInTheDocument()
    expect(screen.getByText('Solar starter kit')).toBeInTheDocument()
  })

  it('loads earlier timeline items, deduplicates by kind and ID, and preserves scroll', async () => {
    const older = messageFixture('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', {
      occurred_at: '2026-08-02T09:00:00Z',
      text: 'Earlier message',
    })
    const duplicate = messageFixture('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', {
      occurred_at: '2026-08-02T10:00:00Z',
      text: 'Recent one',
    })
    const noteWithSameId = internalNoteFixture(
      'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
      { occurred_at: '2026-08-02T09:30:00Z', text: 'Distinct note kind' },
    )
    const recent = messageFixture('cccccccc-cccc-4ccc-8ccc-cccccccccccc', {
      occurred_at: '2026-08-02T11:00:00Z',
      text: 'Recent two',
    })
    server.use(
      authenticated(),
      http.get('/api/v1/operator/conversations/:conversationId', () =>
        HttpResponse.json(conversationDetailFixture()),
      ),
      http.get('/api/v1/operator/conversations/:conversationId/timeline', ({ request }) => {
        const before = new URL(request.url).searchParams.get('before')
        return before === 'opaque-older'
          ? HttpResponse.json({ items: [older, noteWithSameId, duplicate], next_older_cursor: null })
          : HttpResponse.json({ items: [duplicate, recent], next_older_cursor: 'opaque-older' })
      }),
    )
    const user = userEvent.setup()
    renderApp(`/inbox/${firstId}?status=active`)
    const history = await screen.findByRole('region', { name: 'Conversation timeline' })
    Object.defineProperty(history, 'scrollHeight', {
      configurable: true,
      get: () => within(history).getAllByRole('article').length * 100,
    })
    history.scrollTop = 40

    await user.click(within(history).getByRole('button', { name: 'Load Earlier' }))
    await waitFor(() => expect(within(history).getAllByRole('article')).toHaveLength(4))
    expect(within(history).getAllByRole('article').map((item) => item.textContent)).toEqual([
      expect.stringContaining('Earlier message'),
      expect.stringContaining('Distinct note kind'),
      expect.stringContaining('Recent one'),
      expect.stringContaining('Recent two'),
    ])
    expect(history.scrollTop).toBe(240)
    expect(window.location.search).toBe('?status=active')
  })

  it('keeps message success visible when detail fails and detail success visible when history fails', async () => {
    server.use(
      authenticated(),
      http.get('/api/v1/operator/conversations/:conversationId', () =>
        HttpResponse.json(
          { error: { code: 'SERVICE_UNAVAILABLE', request_id: 'detail-ref' } },
          { status: 503 },
        ),
      ),
      http.get('/api/v1/operator/conversations/:conversationId/timeline', () =>
        HttpResponse.json({ items: [messageFixture()], next_older_cursor: null }),
      ),
    )
    const firstRender = renderApp(`/inbox/${firstId}`)
    expect(await screen.findByText('Bonjour, je souhaite des informations.')).toBeInTheDocument()
    expect(screen.getByText('detail-ref')).toBeInTheDocument()
    firstRender.unmount()

    server.use(
      authenticated(),
      http.get('/api/v1/operator/conversations/:conversationId', () =>
        HttpResponse.json(conversationDetailFixture()),
      ),
      http.get('/api/v1/operator/conversations/:conversationId/timeline', () =>
        HttpResponse.json(
          { error: { code: 'SERVICE_UNAVAILABLE', request_id: 'history-ref' } },
          { status: 503 },
        ),
      ),
    )
    renderApp(`/inbox/${firstId}`)
    expect(await screen.findByText('Solar starter kit')).toBeInTheDocument()
    expect(screen.getByText('history-ref')).toBeInTheDocument()
  })

  it('preserves recent messages and offers retry when an older page fails', async () => {
    server.use(
      authenticated(),
      http.get('/api/v1/operator/conversations/:conversationId', () =>
        HttpResponse.json(conversationDetailFixture()),
      ),
      http.get('/api/v1/operator/conversations/:conversationId/timeline', ({ request }) =>
        new URL(request.url).searchParams.has('before')
          ? HttpResponse.json(
              { error: { code: 'SERVICE_UNAVAILABLE', request_id: 'older-ref' } },
              { status: 503 },
            )
          : HttpResponse.json({ items: [messageFixture()], next_older_cursor: 'older' }),
      ),
    )
    const user = userEvent.setup()
    renderApp(`/inbox/${firstId}`)
    const history = await screen.findByRole('region', { name: 'Conversation timeline' })
    await user.click(within(history).getByRole('button', { name: 'Load Earlier' }))

    expect(await within(history).findByText('Earlier messages could not be loaded.', { exact: false })).toBeInTheDocument()
    expect(within(history).getByText('Bonjour, je souhaite des informations.')).toBeInTheDocument()
    expect(within(history).getByRole('button', { name: 'Retry earlier messages' })).toBeInTheDocument()
  })

  it('suppresses stale detail and history when selection changes rapidly', async () => {
    const secondQueue = {
      ...conversationFixture(secondId),
      customer: { display_name: 'Second Customer', phone_masked: '***2222' },
    }
    server.use(
      authenticated(),
      http.get('/api/v1/operator/conversations', () =>
        HttpResponse.json({ items: [conversationFixture(), secondQueue], next_cursor: null }),
      ),
      http.get('/api/v1/operator/conversations/:conversationId', async ({ params }) => {
        const id = String(params.conversationId)
        if (id === firstId) await delay(120)
        return HttpResponse.json({
          ...conversationDetailFixture(id),
          customer: {
            display_name: id === firstId ? 'Stale Customer' : 'Current Customer',
            phone_masked: id === firstId ? '***1111' : '***2222',
          },
        })
      }),
      http.get('/api/v1/operator/conversations/:conversationId/timeline', async ({ params }) => {
        const id = String(params.conversationId)
        if (id === firstId) await delay(120)
        return HttpResponse.json({
          items: [messageFixture(undefined, { text: id === firstId ? 'Stale message' : 'Current message' })],
          next_older_cursor: null,
        })
      }),
    )
    const user = userEvent.setup()
    renderApp('/inbox')
    await user.click(await screen.findByRole('link', { name: 'Conversation with Marie Client' }))
    await user.click(screen.getByRole('link', { name: 'Conversation with Second Customer' }))

    expect(await screen.findByText('Current Customer')).toBeInTheDocument()
    expect(await screen.findByText('Current message')).toBeInTheDocument()
    await delay(150)
    expect(screen.queryByText('Stale Customer')).not.toBeInTheDocument()
    expect(screen.queryByText('Stale message')).not.toBeInTheDocument()
  })

  it('uses the same safe unavailable state for a missing or inaccessible conversation', async () => {
    server.use(
      authenticated(),
      http.get('/api/v1/operator/conversations/:conversationId', () =>
        HttpResponse.json(
          { error: { code: 'CONVERSATION_NOT_FOUND', request_id: 'not-found-ref' } },
          { status: 404 },
        ),
      ),
      http.get('/api/v1/operator/conversations/:conversationId/timeline', () =>
        HttpResponse.json(
          { error: { code: 'CONVERSATION_NOT_FOUND', request_id: 'history-not-found-ref' } },
          { status: 404 },
        ),
      ),
    )
    renderApp(`/inbox/${firstId}`)

    expect((await screen.findAllByText('This conversation is unavailable.')).length).toBe(2)
    expect(screen.queryByText(/does not exist|belongs to another/i)).not.toBeInTheDocument()
  })

  it('shows capability permission denial without exposing conversation data', async () => {
    server.use(
      authenticated(),
      http.get('/api/v1/operator/conversations/:conversationId', () =>
        HttpResponse.json(
          { error: { code: 'CAPABILITY_REQUIRED', request_id: 'permission-ref' } },
          { status: 403 },
        ),
      ),
      http.get('/api/v1/operator/conversations/:conversationId/timeline', () =>
        HttpResponse.json(
          { error: { code: 'CAPABILITY_REQUIRED', request_id: 'history-permission-ref' } },
          { status: 403 },
        ),
      ),
    )
    renderApp(`/inbox/${firstId}`)

    expect((await screen.findAllByText('You do not have permission to view this conversation.')).length).toBe(2)
    expect(screen.queryByText('Marie Client')).not.toBeInTheDocument()
    expect(screen.getByText('permission-ref')).toBeInTheDocument()
  })

  it('removes all protected workspace state when either request reports session expiration', async () => {
    server.use(
      authenticated(),
      http.get('/api/v1/operator/conversations/:conversationId', () =>
        HttpResponse.json(
          { error: { code: 'AUTH_SESSION_EXPIRED', request_id: 'detail-expired' } },
          { status: 401 },
        ),
      ),
      http.get('/api/v1/operator/conversations/:conversationId/timeline', async () => {
        await delay(80)
        return HttpResponse.json({ items: [messageFixture()], next_older_cursor: null })
      }),
    )
    renderApp(`/inbox/${firstId}`)

    expect(await screen.findByRole('heading', { name: 'Sign in' })).toHaveFocus()
    expect(screen.queryByText('Marie Client')).not.toBeInTheDocument()
    expect(screen.queryByRole('navigation')).not.toBeInTheDocument()
  })

  it('is axe-clean and exposes only the supported ownership write action', async () => {
    server.use(
      authenticated(),
      http.get('/api/v1/operator/conversations', () =>
        HttpResponse.json({ items: [conversationFixture()], next_cursor: null }),
      ),
      ...successfulWorkspace(),
    )
    const { container } = renderApp(`/inbox/${firstId}`)
    await screen.findByRole('region', { name: 'Conversation timeline' })

    expect(screen.getByRole('button', { name: 'Escalate to Human' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /reply|send|assign|resolve|compose|return to ai/i })).not.toBeInTheDocument()
    expect(screen.queryByText(/channel|delivery status|unread|priority/i)).not.toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Internal Note' })).toBeInTheDocument()
    await expectAccessible(container)
  })
})
