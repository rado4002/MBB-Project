import { fireEvent, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { delay, http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { expectAccessible } from '../test/accessibility'
import { sessionFixture } from '../test/fixtures'
import { renderApp } from '../test/renderApp'
import { server } from '../test/server'

function sessionHandler(role: 'administrator' | 'operator' | 'analyst' = 'operator', required = false) {
  return http.get('/api/v1/auth/session', () => HttpResponse.json(sessionFixture(role, required)))
}

describe('startup authentication resolution', () => {
  it('shows a neutral loading state and never flashes protected navigation', async () => {
    server.use(
      http.get('/api/v1/auth/session', async () => {
        await delay(80)
        return HttpResponse.json(sessionFixture())
      }),
    )
    renderApp('/inbox')

    expect(screen.getByRole('heading', { name: 'Checking your session' })).toBeInTheDocument()
    expect(screen.queryByRole('navigation')).not.toBeInTheDocument()
    expect(await screen.findByRole('heading', { name: 'Inbox' })).toBeInTheDocument()
  })

  it.each([
    ['administrator', 'Ada Admin', 'Administrator'],
    ['operator', 'Omar Operator', 'Operator'],
  ] as const)('resolves an authenticated %s into the shared shell', async (role, name, roleLabel) => {
    server.use(sessionHandler(role))
    renderApp('/inbox')

    expect(await screen.findByRole('heading', { name: 'Inbox' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: new RegExp(name) })).toHaveTextContent(roleLabel)
    expect(screen.getAllByRole('link', { name: 'Inbox' })).toHaveLength(1)
  })

  it('forces mandatory password change before protected routes', async () => {
    server.use(sessionHandler('operator', true))
    renderApp('/account')
    expect(await screen.findByRole('heading', { name: 'Change your password' })).toBeInTheDocument()
    expect(screen.queryByRole('navigation')).not.toBeInTheDocument()
  })

  it('resolves a missing session as anonymous', async () => {
    renderApp('/inbox')
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
    expect(window.location.pathname).toBe('/login')
  })

  it('distinguishes authentication unavailability from anonymity', async () => {
    server.use(
      http.get('/api/v1/auth/session', () =>
        HttpResponse.json({ error: { code: 'authentication_unavailable', request_id: 'start-503' } }, { status: 503 }),
      ),
    )
    renderApp('/inbox')
    expect(await screen.findByRole('heading', { name: 'Authentication unavailable' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Sign in' })).not.toBeInTheDocument()
  })
})

describe('routing and authenticated shell', () => {
  it('redirects an authenticated user away from login', async () => {
    server.use(sessionHandler())
    renderApp('/login')
    expect(await screen.findByRole('heading', { name: 'Inbox' })).toBeInTheDocument()
  })

  it('shows read-only Account and Session views without sensitive identifiers', async () => {
    server.use(sessionHandler())
    const user = userEvent.setup()
    renderApp('/account')
    expect(await screen.findByRole('heading', { name: 'My Account' })).toBeInTheDocument()
    expect(screen.getAllByText('Omar Operator')).toHaveLength(2)
    expect(screen.getByText('operator.user')).toBeInTheDocument()
    expect(screen.queryByText('account-test-only')).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /Omar Operator/ }))
    await user.click(screen.getByRole('menuitem', { name: 'Session' }))
    expect(await screen.findByRole('heading', { name: 'Session' })).toBeInTheDocument()
    expect(screen.getByText('Idle expiry')).toBeInTheDocument()
    expect(screen.queryByText(/csrf|cookie|token/i)).not.toBeInTheDocument()
  })

  it('supports browser Back and Forward navigation after in-app navigation', async () => {
    server.use(sessionHandler())
    const user = userEvent.setup()
    renderApp('/inbox')
    await screen.findByRole('heading', { name: 'Inbox' })
    await user.click(screen.getByRole('button', { name: /Omar Operator/ }))
    await user.click(screen.getByRole('menuitem', { name: 'My Account' }))
    await screen.findByRole('heading', { name: 'My Account' })

    window.history.back()
    fireEvent(window, new PopStateEvent('popstate'))
    await waitFor(() => expect(window.location.pathname).toBe('/inbox'))

    window.history.forward()
    fireEvent(window, new PopStateEvent('popstate'))
    await waitFor(() => expect(window.location.pathname).toBe('/account'))
    expect(screen.getByRole('heading', { name: 'My Account' })).toBeInTheDocument()
  })

  it('returns a safe application-level not-found page', async () => {
    renderApp('/does-not-exist')
    expect(screen.getByRole('heading', { name: 'Page not found' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Return to MBB' })).toHaveAttribute('href', '/')
  })

  it('exposes only Inbox primary navigation and no unsupported modules', async () => {
    server.use(sessionHandler('administrator'))
    renderApp('/inbox')
    await screen.findByRole('heading', { name: 'Inbox' })
    const navigation = screen.getByRole('navigation', { name: 'Primary navigation' })
    expect(navigation).toHaveTextContent('Inbox')
    expect(navigation).not.toHaveTextContent(/Home|Customers|Sales|Commerce|Operations|Insights|Administration|Channels|AI Configuration/)
    expect(screen.queryByRole('searchbox')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /reply|filter|compose/i })).not.toBeInTheDocument()
  })

  it('has no automated accessibility violations in the Inbox, Account, or Session routes', async () => {
    server.use(sessionHandler())
    const { container } = renderApp('/inbox')
    await screen.findByRole('heading', { name: 'Inbox' })
    await expectAccessible(container)
  })
})
