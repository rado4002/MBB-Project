import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { delay, http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { expectAccessible } from '../../test/accessibility'
import { conversationFixture, sessionFixture } from '../../test/fixtures'
import { renderApp } from '../../test/renderApp'
import { server } from '../../test/server'

function authenticated(role: 'operator' | 'administrator' | 'analyst' = 'operator') {
  return http.get('/api/v1/auth/session', () => HttpResponse.json(sessionFixture(role)))
}

describe('read-only conversation queue', () => {
  it('requests the authenticated E1 list and announces truthful loading', async () => {
    server.use(
      authenticated(),
      http.get('/api/v1/operator/conversations', async ({ request }) => {
        expect(new URL(request.url).search).toBe('')
        await delay(80)
        return HttpResponse.json({ items: [conversationFixture()], next_cursor: null })
      }),
    )
    renderApp('/inbox')

    expect(await screen.findByRole('status')).toHaveTextContent('Loading conversations')
    expect(await screen.findByText('Marie Client')).toBeInTheDocument()
  })

  it('renders only minimized contract fields, masked phone, plain hostile text, and safe media labels', async () => {
    const hostile = {
      ...conversationFixture(),
      customer: { display_name: '<script>alert(document.cookie)</script>', phone_masked: '***5678' },
      latest_message: {
        ...conversationFixture().latest_message!,
        preview: '<img src=x onerror=alert(1)>',
      },
      open_escalation: { exists: true },
    }
    const media = {
      ...conversationFixture('22222222-2222-4222-8222-222222222222'),
      customer: { display_name: null, phone_masked: '***0009' },
      language: 'swahili' as const,
      status: 'escalated' as const,
      latest_message: {
        preview: 'https://provider.example/private-media',
        content_type: 'voice_note' as const,
        direction: 'outbound' as const,
        occurred_at: '2026-08-02T12:00:00Z',
      },
      awaiting_response_since: null,
    }
    server.use(
      authenticated(),
      http.get('/api/v1/operator/conversations', () =>
        HttpResponse.json({ items: [hostile, media], next_cursor: null }),
      ),
    )
    const { container } = renderApp('/inbox')

    expect(await screen.findByText('<script>alert(document.cookie)</script>')).toBeInTheDocument()
    expect(screen.getByText('<img src=x onerror=alert(1)>')).toBeInTheDocument()
    expect(container.querySelector('script')).toBeNull()
    expect(container.querySelector('img')).toBeNull()
    expect(screen.getByText('***5678')).toBeInTheDocument()
    expect(screen.queryByText('+243990005678')).not.toBeInTheDocument()
    expect(screen.getByText('Voice note')).toBeInTheDocument()
    expect(screen.queryByText('https://provider.example/private-media')).not.toBeInTheDocument()
    expect(screen.getByText('Customer')).toBeInTheDocument()
    expect(within(screen.getAllByRole('article')[0]).getByText('Open escalation')).toBeInTheDocument()
    expect(screen.getByText(/Awaiting response since/)).toBeInTheDocument()
  })

  it('distinguishes unfiltered and filtered empty states', async () => {
    server.use(authenticated())
    const user = userEvent.setup()
    renderApp('/inbox')
    expect(await screen.findByRole('heading', { name: 'No conversations are available' })).toBeInTheDocument()

    await user.selectOptions(screen.getByLabelText('Status'), 'active')
    expect(await screen.findByRole('heading', { name: 'No conversations match these filters' })).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: 'Clear Filters' }).length).toBeGreaterThan(0)
  })

  it('shows permission denial without replacing it with an empty result', async () => {
    server.use(
      authenticated('analyst'),
      http.get('/api/v1/operator/conversations', () =>
        HttpResponse.json(
          { error: { code: 'FORBIDDEN', message: 'denied', request_id: 'permission-ref' } },
          { status: 403 },
        ),
      ),
    )
    renderApp('/inbox')
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('do not have permission')
    expect(alert).toHaveTextContent('permission-ref')
    expect(screen.queryByText('There are no conversations to show.')).not.toBeInTheDocument()
  })

  it('shows throttling and Retry-After truthfully', async () => {
    server.use(
      authenticated(),
      http.get('/api/v1/operator/conversations', () =>
        HttpResponse.json(
          { error: { code: 'SERVICE_UNAVAILABLE', request_id: 'service-ref' } },
          { status: 429, headers: { 'Retry-After': '18' } },
        ),
      ),
    )
    renderApp('/inbox')
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Try again in 18 seconds')
    expect(alert).toHaveTextContent('service-ref')
  })

  it('shows a service failure without replacing it with an empty result', async () => {
    server.use(
      authenticated(),
      http.get('/api/v1/operator/conversations', () =>
        HttpResponse.json(
          { error: { code: 'SERVICE_UNAVAILABLE', request_id: 'unavailable-ref' } },
          { status: 503 },
        ),
      ),
    )
    renderApp('/inbox')
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('temporarily unavailable')
    expect(alert).toHaveTextContent('unavailable-ref')
    expect(screen.queryByText('There are no conversations to show.')).not.toBeInTheDocument()
  })

  it('removes protected queue data on session expiration', async () => {
    server.use(
      authenticated(),
      http.get('/api/v1/operator/conversations', () =>
        HttpResponse.json(
          { error: { code: 'AUTH_SESSION_EXPIRED', request_id: 'expired-queue' } },
          { status: 401 },
        ),
      ),
    )
    renderApp('/inbox')
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toHaveFocus()
    expect(screen.queryByRole('navigation')).not.toBeInTheDocument()
  })

  it('serializes supported filters, displays them, and clears them without PII', async () => {
    const requests: string[] = []
    server.use(
      authenticated(),
      http.get('/api/v1/operator/conversations', ({ request }) => {
        requests.push(new URL(request.url).search)
        return HttpResponse.json({ items: [], next_cursor: null })
      }),
    )
    const user = userEvent.setup()
    renderApp('/inbox')
    await screen.findByRole('heading', { name: 'No conversations are available' })
    await user.selectOptions(screen.getByLabelText('Status'), 'qualifying')
    await user.selectOptions(screen.getByLabelText('Escalation'), 'open')
    await user.selectOptions(screen.getByLabelText('Language'), 'lingala')

    await waitFor(() => expect(window.location.search).toBe('?status=qualifying&escalation_state=open&language=lingala'))
    const active = screen.getByLabelText('Active filters')
    expect(active).toHaveTextContent('Status: Qualifying')
    expect(active).toHaveTextContent('Escalation: Open escalation')
    expect(active).toHaveTextContent('Language: Lingala')
    expect(requests.at(-1)).toBe('?status=qualifying&escalation_state=open&language=lingala')
    expect(window.location.search).not.toMatch(/cursor|phone|message|customer/)

    await user.click(within(active).getByRole('button', { name: 'Clear Filters' }))
    await waitFor(() => expect(window.location.search).toBe(''))
  })

  it('safely removes invalid and unsupported URL filters', async () => {
    const requests: string[] = []
    server.use(
      authenticated(),
      http.get('/api/v1/operator/conversations', ({ request }) => {
        requests.push(new URL(request.url).search)
        return HttpResponse.json({ items: [], next_cursor: null })
      }),
    )
    renderApp('/inbox?status=closed&language=english&search=%2B243990005678&cursor=secret')

    await waitFor(() => expect(window.location.search).toBe(''))
    expect(screen.getByLabelText('Status')).toHaveValue('')
    expect(screen.getByLabelText('Language')).toHaveValue('')
    expect(requests.at(-1)).toBe('')
  })

  it('loads more with an opaque cursor and deduplicates repeated conversation IDs', async () => {
    const first = conversationFixture()
    const second = conversationFixture('22222222-2222-4222-8222-222222222222')
    let requests = 0
    server.use(
      authenticated(),
      http.get('/api/v1/operator/conversations', ({ request }) => {
        requests += 1
        const cursor = new URL(request.url).searchParams.get('cursor')
        return cursor
          ? HttpResponse.json({ items: [first, second], next_cursor: null })
          : HttpResponse.json({ items: [first], next_cursor: 'opaque-next-page' })
      }),
    )
    const user = userEvent.setup()
    renderApp('/inbox')
    await screen.findByText('Marie Client')
    await user.click(screen.getByRole('button', { name: 'Load More' }))

    await waitFor(() => expect(screen.getAllByRole('article')).toHaveLength(2))
    expect(requests).toBe(2)
    expect(window.location.search).not.toContain('cursor')
  })

  it('resets pagination and visible rows when a filter changes', async () => {
    const first = conversationFixture()
    const second = { ...conversationFixture('22222222-2222-4222-8222-222222222222'), customer: { display_name: 'Second Customer', phone_masked: '***2222' } }
    const filtered = { ...conversationFixture('33333333-3333-4333-8333-333333333333'), customer: { display_name: 'Filtered Customer', phone_masked: '***3333' } }
    const seen: string[] = []
    server.use(
      authenticated(),
      http.get('/api/v1/operator/conversations', ({ request }) => {
        const url = new URL(request.url)
        seen.push(url.search)
        if (url.searchParams.get('status') === 'active') {
          return HttpResponse.json({ items: [filtered], next_cursor: null })
        }
        if (url.searchParams.has('cursor')) {
          return HttpResponse.json({ items: [second], next_cursor: null })
        }
        return HttpResponse.json({ items: [first], next_cursor: 'page-two' })
      }),
    )
    const user = userEvent.setup()
    renderApp('/inbox')
    await screen.findByText('Marie Client')
    await user.click(screen.getByRole('button', { name: 'Load More' }))
    await screen.findByText('Second Customer')
    await user.selectOptions(screen.getByLabelText('Status'), 'active')

    expect(await screen.findByText('Filtered Customer')).toBeInTheDocument()
    expect(screen.queryByText('Second Customer')).not.toBeInTheDocument()
    expect(seen.at(-1)).toBe('?status=active')
  })

  it('preserves current results during a safe background refresh', async () => {
    const refreshed = { ...conversationFixture(), customer: { display_name: 'Refreshed Customer', phone_masked: '***5678' } }
    let calls = 0
    server.use(
      authenticated(),
      http.get('/api/v1/operator/conversations', async () => {
        calls += 1
        if (calls === 1) return HttpResponse.json({ items: [conversationFixture()], next_cursor: null })
        await delay(80)
        return HttpResponse.json({ items: [refreshed], next_cursor: null })
      }),
    )
    const user = userEvent.setup()
    renderApp('/inbox')
    await screen.findByText('Marie Client')
    await user.click(screen.getByRole('button', { name: 'Refresh' }))

    expect(screen.getByText('Marie Client')).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('Refreshing conversations')
    expect(await screen.findByText('Refreshed Customer')).toBeInTheDocument()
  })

  it('suppresses stale responses when filters change quickly', async () => {
    const active = { ...conversationFixture(), customer: { display_name: 'Stale Active', phone_masked: '***1111' } }
    const dormant = { ...conversationFixture('22222222-2222-4222-8222-222222222222'), status: 'dormant' as const, customer: { display_name: 'Current Dormant', phone_masked: '***2222' } }
    server.use(
      authenticated(),
      http.get('/api/v1/operator/conversations', async ({ request }) => {
        const status = new URL(request.url).searchParams.get('status')
        if (status === 'active') {
          await delay(120)
          return HttpResponse.json({ items: [active], next_cursor: null })
        }
        return HttpResponse.json({ items: [dormant], next_cursor: null })
      }),
    )
    const user = userEvent.setup()
    renderApp('/inbox?status=active')
    await screen.findByRole('status')
    await user.selectOptions(screen.getByLabelText('Status'), 'dormant')

    expect(await screen.findByText('Current Dormant')).toBeInTheDocument()
    await delay(150)
    expect(screen.queryByText('Stale Active')).not.toBeInTheDocument()
  })

  it('restores supported filters with browser Back and Forward', async () => {
    server.use(authenticated())
    const user = userEvent.setup()
    renderApp('/inbox')
    await screen.findByRole('heading', { name: 'No conversations are available' })
    await user.selectOptions(screen.getByLabelText('Status'), 'active')
    await user.selectOptions(screen.getByLabelText('Language'), 'french')
    await waitFor(() => expect(window.location.search).toContain('language=french'))

    window.history.back()
    fireEvent(window, new PopStateEvent('popstate'))
    await waitFor(() => expect(screen.getByLabelText('Language')).toHaveValue(''))
    expect(screen.getByLabelText('Status')).toHaveValue('active')

    window.history.forward()
    fireEvent(window, new PopStateEvent('popstate'))
    await waitFor(() => expect(screen.getByLabelText('Language')).toHaveValue('french'))
  })

  it('is keyboard accessible, non-interactive per row, and contains no unsupported controls or fake data', async () => {
    server.use(
      authenticated(),
      http.get('/api/v1/operator/conversations', () =>
        HttpResponse.json({ items: [conversationFixture()], next_cursor: null }),
      ),
    )
    const user = userEvent.setup()
    const { container } = renderApp('/inbox')
    await screen.findByText('Marie Client')
    await user.tab()
    expect(document.activeElement).toBeTruthy()
    expect(screen.getByRole('article')).not.toHaveAttribute('tabindex')
    expect(screen.getByRole('article').closest('a, button')).toBeNull()
    expect(screen.queryByRole('searchbox')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /reply|assign|take over|resolve|escalate|compose|AI/i })).not.toBeInTheDocument()
    expect(screen.queryByText(/Jane Doe|Lorem ipsum|Priority|Unread/i)).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'No conversation selected' })).toBeInTheDocument()
    await expectAccessible(container)
  })
})
