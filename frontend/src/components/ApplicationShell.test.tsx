import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { delay, http, HttpResponse } from 'msw'
import { describe, expect, it, vi } from 'vitest'
import { expectAccessible } from '../test/accessibility'
import { sessionFixture } from '../test/fixtures'
import { renderApp } from '../test/renderApp'
import { server } from '../test/server'

function authenticatedHandlers() {
  return [
    http.get('/api/v1/auth/session', () => HttpResponse.json(sessionFixture())),
    http.get('/api/v1/auth/csrf', () =>
      HttpResponse.json({ csrf_token: 'logout-csrf', expires_at_epoch: 1_900_000_000 }),
    ),
  ]
}

describe('account menu', () => {
  it('is keyboard operable and returns focus to its trigger after Escape', async () => {
    server.use(...authenticatedHandlers())
    const user = userEvent.setup()
    renderApp('/inbox')
    const trigger = await screen.findByRole('button', { name: /Omar Operator/ })
    trigger.focus()
    await user.keyboard('{Enter}')
    expect(screen.getByRole('menu')).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: 'My Account' })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: 'Session' })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: 'Logout' })).toBeInTheDocument()
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
    await waitFor(() => expect(trigger).toHaveFocus())
  })

  it('has accessible Account and Session pages', async () => {
    server.use(...authenticatedHandlers())
    const user = userEvent.setup()
    const { container } = renderApp('/account')
    await screen.findByRole('heading', { name: 'My Account' })
    await expectAccessible(container)
    await user.click(screen.getByRole('button', { name: /Omar Operator/ }))
    await user.click(screen.getByRole('menuitem', { name: 'Session' }))
    await screen.findByRole('heading', { name: 'Session' })
    await expectAccessible(container)
  })
})

describe('logout safety', () => {
  it('removes protected state immediately and completes successful logout', async () => {
    server.use(
      ...authenticatedHandlers(),
      http.post('/api/v1/auth/logout', async ({ request }) => {
        expect(request.headers.get('X-CSRF-Token')).toBe('logout-csrf')
        expect(await request.json()).toEqual({})
        await delay(80)
        return HttpResponse.json({ logged_out: true })
      }),
    )
    const user = userEvent.setup()
    renderApp('/inbox')
    await screen.findByRole('heading', { name: 'Inbox' })
    await user.click(screen.getByRole('button', { name: /Omar Operator/ }))
    await user.click(screen.getByRole('menuitem', { name: 'Logout' }))

    await waitFor(() => expect(screen.queryByRole('heading', { name: 'Inbox' })).not.toBeInTheDocument())
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
  })

  it('treats an already-absent session as idempotent logout success', async () => {
    server.use(
      ...authenticatedHandlers(),
      http.post('/api/v1/auth/logout', () =>
        HttpResponse.json({ error: { code: 'session_required', request_id: 'already-gone' } }, { status: 401 }),
      ),
    )
    const user = userEvent.setup()
    renderApp('/session')
    await screen.findByRole('heading', { name: 'Session' })
    await user.click(screen.getByRole('button', { name: 'Logout' }))
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
    expect(screen.queryByText(/could not be confirmed/i)).not.toBeInTheDocument()
  })

  it('does not claim success after server failure and offers a working retry', async () => {
    let attempts = 0
    server.use(
      ...authenticatedHandlers(),
      http.post('/api/v1/auth/logout', () => {
        attempts += 1
        if (attempts === 1) {
          return HttpResponse.json(
            { error: { code: 'authentication_unavailable', request_id: 'logout-failed' } },
            { status: 503 },
          )
        }
        return HttpResponse.json({ logged_out: true })
      }),
    )
    const user = userEvent.setup()
    renderApp('/inbox')
    await screen.findByRole('heading', { name: 'Inbox' })
    await user.click(screen.getByRole('button', { name: /Omar Operator/ }))
    await user.click(screen.getByRole('menuitem', { name: 'Logout' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Server logout could not be confirmed')
    expect(alert).toHaveTextContent('logout-failed')
    expect(screen.queryByRole('heading', { name: 'Inbox' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Retry logout' }))
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument())
    expect(attempts).toBe(2)
  })

  it('never attempts to manipulate an HttpOnly cookie through JavaScript', async () => {
    server.use(
      ...authenticatedHandlers(),
      http.post('/api/v1/auth/logout', () => HttpResponse.json({ logged_out: true })),
    )
    const cookieDescriptor = Object.getOwnPropertyDescriptor(Document.prototype, 'cookie')
    const cookieSetter = vi.fn(cookieDescriptor?.set)
    Object.defineProperty(document, 'cookie', {
      configurable: true,
      get: cookieDescriptor?.get?.bind(document),
      set: cookieSetter,
    })
    const user = userEvent.setup()
    renderApp('/session')
    await screen.findByRole('heading', { name: 'Session' })
    await user.click(screen.getByRole('button', { name: 'Logout' }))
    await screen.findByRole('heading', { name: 'Sign in' })
    expect(cookieSetter).not.toHaveBeenCalled()
    delete (document as unknown as { cookie?: string }).cookie
  })
})
