import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { delay, http, HttpResponse } from 'msw'
import { describe, expect, it, vi } from 'vitest'
import { expectAccessible } from '../../test/accessibility'
import {
  conversationDetailFixture,
  conversationFixture,
  messageFixture,
  sessionFixture,
} from '../../test/fixtures'
import { renderApp } from '../../test/renderApp'
import { server } from '../../test/server'

const conversationId = '11111111-1111-4111-8111-111111111111'

function authenticated() {
  return http.get('/api/v1/auth/session', () =>
    HttpResponse.json(sessionFixture()),
  )
}

function workspaceHandlers() {
  return [
    http.get('/api/v1/operator/conversations', () =>
      HttpResponse.json({ items: [conversationFixture()], next_cursor: null }),
    ),
    http.get('/api/v1/operator/conversations/:conversationId', () =>
      HttpResponse.json(conversationDetailFixture()),
    ),
    http.get('/api/v1/operator/conversations/:conversationId/messages', () =>
      HttpResponse.json({ items: [messageFixture()], next_older_cursor: null }),
    ),
  ]
}

describe('responsive Inbox workflow refinement', () => {
  it('provides the desktop queue, dominant timeline, and limited context regions', async () => {
    server.use(authenticated(), ...workspaceHandlers())
    renderApp(`/inbox/${conversationId}`)

    expect(await screen.findByRole('region', { name: 'Message history' })).toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Conversation queue' })).toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Conversation' })).toBeInTheDocument()
    expect(screen.getByRole('region', { name: 'Messages' })).toBeInTheDocument()
    const context = screen.getByRole('complementary', { name: 'Context' })
    expect(context).toBeInTheDocument()
    expect(context).toHaveAttribute('tabindex', '0')
    context.focus()
    expect(context).toHaveFocus()
    expect(screen.queryByText(/future action|coming soon/i)).not.toBeInTheDocument()
  })

  it('keeps selected row state distinct from keyboard focus', async () => {
    server.use(authenticated(), ...workspaceHandlers())
    renderApp(`/inbox/${conversationId}`)

    const rowLink = await screen.findByRole('link', {
      name: 'Conversation with Marie Client',
    })
    const row = rowLink.closest('li')
    expect(rowLink).toHaveAttribute('aria-current', 'page')
    expect(row).toHaveClass('conversation-row--selected')
    expect(screen.getByRole('heading', { name: 'Conversation' })).toHaveFocus()
    expect(rowLink).not.toHaveFocus()

    rowLink.focus()
    expect(rowLink).toHaveFocus()
    expect(row).toHaveClass('conversation-row--selected')
  })

  it('opens tablet and mobile context as an inert focus-trapped dialog and returns focus on Escape', async () => {
    server.use(authenticated(), ...workspaceHandlers())
    const user = userEvent.setup()
    renderApp(`/inbox/${conversationId}`)
    await screen.findByText('Solar starter kit')

    const trigger = screen.getByRole('button', { name: 'Details' })
    await user.click(trigger)
    const dialog = screen.getByRole('dialog', { name: 'Conversation details' })
    const close = within(dialog).getByRole('button', { name: 'Close' })
    const appFrame = document.querySelector('.app-frame')

    expect(close).toHaveFocus()
    expect(appFrame).toHaveAttribute('inert')
    expect(appFrame).toHaveAttribute('aria-hidden', 'true')
    expect(document.body.style.overflow).toBe('hidden')
    expect(within(dialog).getByText('Solar starter kit')).toBeInTheDocument()

    await user.tab()
    expect(close).toHaveFocus()
    await expectAccessible(document.body)
    await user.keyboard('{Escape}')

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    await waitFor(() => expect(trigger).toHaveFocus())
    expect(appFrame).not.toHaveAttribute('inert')
    expect(appFrame).not.toHaveAttribute('aria-hidden')
    expect(document.body.style.overflow).toBe('')
  })

  it('preserves filters, queue scroll position, and row focus through the mobile Back flow', async () => {
    server.use(authenticated(), ...workspaceHandlers())
    const user = userEvent.setup()
    renderApp('/inbox?status=active&language=french')

    const queue = await screen.findByRole('region', { name: 'Conversation queue' })
    const rowLink = await screen.findByRole('link', {
      name: 'Conversation with Marie Client',
    })
    const rowFocus = vi.spyOn(rowLink, 'focus')
    const scrollY = vi.spyOn(window, 'scrollY', 'get').mockReturnValue(120)
    const windowScroll = vi.spyOn(window, 'scrollTo').mockImplementation(() => undefined)
    queue.scrollTop = 173
    await user.click(rowLink)

    expect(await screen.findByRole('heading', { name: 'Conversation' })).toHaveFocus()
    expect(window.location.search).toBe('?status=active&language=french')
    expect(screen.getByRole('link', { name: 'Inbox' })).toHaveAttribute(
      'href',
      '/inbox?status=active&language=french',
    )
    await user.click(screen.getByRole('link', { name: 'Back to Inbox' }))

    await waitFor(() => expect(window.location.pathname).toBe('/inbox'))
    expect(window.location.search).toBe('?status=active&language=french')
    expect(queue.scrollTop).toBe(173)
    await waitFor(() => expect(rowLink).toHaveFocus())
    expect(rowFocus).toHaveBeenCalledWith({ preventScroll: true })
    expect(windowScroll).toHaveBeenCalledWith({ top: 120, left: 0, behavior: 'auto' })
    expect(rowLink).not.toHaveAttribute('aria-current')
    expect(rowLink.closest('li')).toHaveClass('conversation-row--recent')
    scrollY.mockRestore()
    windowScroll.mockRestore()
  })

  it('uses restrained structural skeletons for startup and region loading', async () => {
    server.use(
      http.get('/api/v1/auth/session', async () => {
        await delay(80)
        return HttpResponse.json(sessionFixture())
      }),
    )
    const startup = renderApp('/inbox')
    expect(await screen.findByRole('heading', { name: 'Checking your session' })).toBeInTheDocument()
    expect(startup.container.querySelector('.startup-skeleton')).toBeInTheDocument()
    await screen.findByRole('heading', { name: 'Inbox' })
    startup.unmount()

    server.use(
      authenticated(),
      http.get('/api/v1/operator/conversations', async () => {
        await delay(100)
        return HttpResponse.json({ items: [conversationFixture()], next_cursor: null })
      }),
      http.get('/api/v1/operator/conversations/:conversationId', async () => {
        await delay(100)
        return HttpResponse.json(conversationDetailFixture())
      }),
      http.get('/api/v1/operator/conversations/:conversationId/messages', async () => {
        await delay(100)
        return HttpResponse.json({ items: [messageFixture()], next_older_cursor: null })
      }),
    )
    const regions = renderApp(`/inbox/${conversationId}`)
    expect(await screen.findByText('Loading conversations…')).toBeInTheDocument()
    expect(screen.getByText('Loading conversation details…')).toBeInTheDocument()
    expect(screen.getByText('Loading messages…')).toBeInTheDocument()
    expect(screen.getByText('Loading context…')).toBeInTheDocument()
    expect(regions.container.querySelectorAll('.skeleton-row')).toHaveLength(3)
    expect(regions.container.querySelectorAll('.skeleton-message')).toHaveLength(3)
    expect((await screen.findAllByText('Bonjour, je souhaite des informations.')).length).toBeGreaterThan(0)
  })

  it('positions the first recent history at the end without forcing later older-page movement', async () => {
    const scrollHeight = vi
      .spyOn(HTMLElement.prototype, 'scrollHeight', 'get')
      .mockReturnValue(640)
    server.use(authenticated(), ...workspaceHandlers())
    renderApp(`/inbox/${conversationId}`)

    const history = await screen.findByRole('region', { name: 'Message history' })
    await waitFor(() => expect(history.scrollTop).toBe(640))
    scrollHeight.mockRestore()
  })

  it('focuses scoped history failures while preserving successful detail and queue regions', async () => {
    server.use(
      authenticated(),
      http.get('/api/v1/operator/conversations', () =>
        HttpResponse.json({ items: [conversationFixture()], next_cursor: null }),
      ),
      http.get('/api/v1/operator/conversations/:conversationId', () =>
        HttpResponse.json(conversationDetailFixture()),
      ),
      http.get('/api/v1/operator/conversations/:conversationId/messages', async () => {
        await delay(60)
        return HttpResponse.json(
          { error: { code: 'SERVICE_UNAVAILABLE', request_id: 'history-focus-ref' } },
          { status: 503 },
        )
      }),
    )
    renderApp(`/inbox/${conversationId}`)

    expect(await screen.findByText('Solar starter kit')).toBeInTheDocument()
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveFocus()
    expect(alert).toHaveTextContent('history-focus-ref')
    expect(screen.getByRole('region', { name: 'Conversation queue' })).toBeInTheDocument()
  })

  it('renders long Unicode identity and message content without changing its meaning', async () => {
    const longName = 'Mado Nzambe — Cliente très patiente — mteja wa muda mrefu 🌍'.repeat(4)
    const longMessage = 'Bonjour nyonso — habari yako? '.repeat(40) + '𐐷'.repeat(40)
    server.use(
      authenticated(),
      http.get('/api/v1/operator/conversations/:conversationId', () =>
        HttpResponse.json({
          ...conversationDetailFixture(),
          customer: { display_name: longName, phone_masked: '***5678' },
        }),
      ),
      http.get('/api/v1/operator/conversations/:conversationId/messages', () =>
        HttpResponse.json({
          items: [messageFixture(undefined, { text: longMessage })],
          next_older_cursor: null,
        }),
      ),
    )
    renderApp(`/inbox/${conversationId}`)

    expect(await screen.findByRole('heading', { name: longName })).toBeInTheDocument()
    const message = screen.getByText(longMessage)
    expect(message).toHaveClass('message-text')
    expect(message.closest('a')).toBeNull()
  })

  it('removes an open context dialog and all protected content on session expiration', async () => {
    server.use(
      authenticated(),
      http.get('/api/v1/operator/conversations/:conversationId', () =>
        HttpResponse.json(conversationDetailFixture()),
      ),
      http.get('/api/v1/operator/conversations/:conversationId/messages', async () => {
        await delay(250)
        return HttpResponse.json(
          { error: { code: 'AUTH_SESSION_EXPIRED', request_id: 'drawer-expired' } },
          { status: 401 },
        )
      }),
    )
    const user = userEvent.setup()
    renderApp(`/inbox/${conversationId}`)
    await screen.findByText('Solar starter kit')
    await user.click(screen.getByRole('button', { name: 'Details' }))
    expect(screen.getByRole('dialog', { name: 'Conversation details' })).toBeInTheDocument()

    expect(await screen.findByRole('heading', { name: 'Sign in' })).toHaveFocus()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.queryByText('Solar starter kit')).not.toBeInTheDocument()
    expect(document.body.style.overflow).toBe('')
  })

  it('keeps the refined workflow read-only and free of unsupported controls', async () => {
    server.use(authenticated(), ...workspaceHandlers())
    const { container } = renderApp(`/inbox/${conversationId}`)
    await screen.findByRole('region', { name: 'Message history' })

    expect(screen.queryByRole('button', {
      name: /reply|send|assign|take over|resolve|escalate|compose|\bAI\b/i,
    })).not.toBeInTheDocument()
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
    expect(screen.queryByText(/owner|channel|delivery status|unread|priority/i)).not.toBeInTheDocument()
    await expectAccessible(container)
  })
})
