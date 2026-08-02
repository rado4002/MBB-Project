import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it, vi } from 'vitest'
import { expectAccessible } from '../../test/accessibility'
import { sessionFixture } from '../../test/fixtures'
import { renderApp } from '../../test/renderApp'
import { server } from '../../test/server'

function csrfHandler(events?: string[]) {
  return http.get('/api/v1/auth/csrf', () => {
    events?.push('csrf')
    return HttpResponse.json({ csrf_token: 'preauth-memory', expires_at_epoch: 1_900_000_000 })
  })
}

async function enterCredentials(password = 'temporary password') {
  const user = userEvent.setup()
  await user.type(screen.getByLabelText('Username'), 'operator.user')
  await user.type(screen.getByLabelText('Password'), password)
  return user
}

describe('login', () => {
  it('initializes CSRF before login and reaches Inbox with a rotated token', async () => {
    const events: string[] = []
    server.use(
      csrfHandler(events),
      http.post('/api/v1/auth/login', async ({ request }) => {
        events.push('login')
        expect(request.headers.get('X-CSRF-Token')).toBe('preauth-memory')
        expect(await request.json()).toEqual({ username: 'operator.user', password: 'temporary password' })
        return HttpResponse.json({ ...sessionFixture(), csrf_token: 'session-memory' })
      }),
    )
    renderApp('/login')
    await screen.findByRole('heading', { name: 'Sign in' })
    const user = await enterCredentials()
    await user.click(screen.getByRole('button', { name: 'Sign in' }))

    expect(await screen.findByRole('heading', { name: 'Inbox' })).toBeInTheDocument()
    expect(events).toEqual(['csrf', 'login'])
  })

  it('routes a temporary credential to mandatory password change', async () => {
    server.use(
      csrfHandler(),
      http.post('/api/v1/auth/login', () =>
        HttpResponse.json({ ...sessionFixture('operator', true), csrf_token: 'session-memory' }),
      ),
    )
    renderApp('/login')
    await screen.findByRole('heading', { name: 'Sign in' })
    const user = await enterCredentials()
    await user.click(screen.getByRole('button', { name: 'Sign in' }))
    expect(await screen.findByRole('heading', { name: 'Change your password' })).toBeInTheDocument()
  })

  it.each([
    ['invalid credentials', 401, 'invalid_credentials', 'The username or password is incorrect.'],
    ['invalid origin', 403, 'origin_invalid', 'The sign-in request could not be accepted. Please try again.'],
    ['JSON required', 415, 'json_required', 'The sign-in request could not be accepted. Please try again.'],
    ['validation', 422, 'request_invalid', 'Check the information entered and try again.'],
    ['service unavailable', 503, 'authentication_unavailable', 'Authentication is temporarily unavailable. Please try again.'],
  ])('handles %s safely while preserving username and clearing password', async (_name, status, code, message) => {
    server.use(
      csrfHandler(),
      http.post('/api/v1/auth/login', () =>
        HttpResponse.json({ error: { code, message: 'backend detail', request_id: `request-${status}` } }, { status }),
      ),
    )
    renderApp('/login')
    await screen.findByRole('heading', { name: 'Sign in' })
    const user = await enterCredentials()
    await user.click(screen.getByRole('button', { name: 'Sign in' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(message)
    expect(screen.getByRole('alert')).toHaveTextContent(`Request reference: request-${status}`)
    expect(screen.getByLabelText('Username')).toHaveValue('operator.user')
    expect(screen.getByLabelText('Password')).toHaveValue('')
  })

  it('handles CSRF initialization failure without submitting credentials', async () => {
    let loginCalls = 0
    server.use(
      http.get('/api/v1/auth/csrf', () =>
        HttpResponse.json({ error: { code: 'csrf_invalid', request_id: 'csrf-request' } }, { status: 403 }),
      ),
      http.post('/api/v1/auth/login', () => {
        loginCalls += 1
        return HttpResponse.json({})
      }),
    )
    renderApp('/login')
    await screen.findByRole('heading', { name: 'Sign in' })
    const user = await enterCredentials()
    await user.click(screen.getByRole('button', { name: 'Sign in' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('security check expired')
    expect(loginCalls).toBe(0)
  })

  it('respects throttling metadata and presents a safe request reference', async () => {
    server.use(
      csrfHandler(),
      http.post('/api/v1/auth/login', () =>
        HttpResponse.json(
          { error: { code: 'authentication_throttled', request_id: 'body-reference' } },
          { status: 429, headers: { 'Retry-After': '30', 'X-Request-ID': 'header-reference' } },
        ),
      ),
    )
    renderApp('/login')
    await screen.findByRole('heading', { name: 'Sign in' })
    const user = await enterCredentials()
    await user.click(screen.getByRole('button', { name: 'Sign in' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Try again in 30 seconds')
    expect(screen.getByRole('alert')).toHaveTextContent('header-reference')
    expect(screen.getByRole('alert')).not.toHaveTextContent('body-reference')
  })

  it('never writes credentials or auth state to browser storage', async () => {
    const localSpy = vi.spyOn(Storage.prototype, 'setItem')
    server.use(
      csrfHandler(),
      http.post('/api/v1/auth/login', () =>
        HttpResponse.json({ error: { code: 'invalid_credentials', request_id: 'storage-test' } }, { status: 401 }),
      ),
    )
    renderApp('/login')
    await screen.findByRole('heading', { name: 'Sign in' })
    const user = await enterCredentials('do-not-store')
    await user.click(screen.getByRole('button', { name: 'Sign in' }))
    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument())
    expect(localSpy).not.toHaveBeenCalled()
  })

  it('has no automated accessibility violations', async () => {
    const { container } = renderApp('/login')
    await screen.findByRole('heading', { name: 'Sign in' })
    await expectAccessible(container)
  })
})
