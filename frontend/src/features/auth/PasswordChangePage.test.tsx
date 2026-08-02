import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { expectAccessible } from '../../test/accessibility'
import { sessionFixture } from '../../test/fixtures'
import { renderApp } from '../../test/renderApp'
import { server } from '../../test/server'

function requiredSessionHandler() {
  return http.get('/api/v1/auth/session', () =>
    HttpResponse.json(sessionFixture('operator', true)),
  )
}

function csrfHandler() {
  return http.get('/api/v1/auth/csrf', () =>
    HttpResponse.json({ csrf_token: 'required-session-csrf', expires_at_epoch: 1_900_000_000 }),
  )
}

async function enterPasswords(
  current = 'temporary password',
  next = 'a sufficiently long new password',
  confirmation = next,
) {
  const user = userEvent.setup()
  await user.type(screen.getByLabelText('Current password'), current)
  await user.type(screen.getByLabelText('New password'), next)
  await user.type(screen.getByLabelText('Confirm new password'), confirmation)
  return user
}

describe('mandatory password change', () => {
  it('protects the route from anonymous access', async () => {
    renderApp('/password-change')
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
  })

  it('presents only the approved password policy', async () => {
    server.use(requiredSessionHandler())
    renderApp('/password-change')
    await screen.findByRole('heading', { name: 'Change your password' })
    expect(screen.getByText('Use 14–128 Unicode characters.')).toBeInTheDocument()
    expect(screen.getByText('Do not use control characters.')).toBeInTheDocument()
    expect(screen.getByText('Do not reuse your current password.')).toBeInTheDocument()
    expect(screen.getByText(/normalized variants of your username or display name/)).toBeInTheDocument()
    expect(screen.getByText('Do not use a blocked common password.')).toBeInTheDocument()
    expect(screen.queryByText(/uppercase|lowercase|number|symbol/i)).not.toBeInTheDocument()
  })

  it('rejects a confirmation mismatch locally, clears secrets, and focuses the summary', async () => {
    let changeCalls = 0
    server.use(
      requiredSessionHandler(),
      http.post('/api/v1/auth/password/change', () => {
        changeCalls += 1
        return HttpResponse.json({})
      }),
    )
    renderApp('/password-change')
    await screen.findByRole('heading', { name: 'Change your password' })
    const user = await enterPasswords('current secret', 'first long password', 'different long password')
    await user.click(screen.getByRole('button', { name: 'Change password' }))
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('do not match')
    expect(alert).toHaveFocus()
    expect(screen.getByLabelText('Current password')).toHaveValue('')
    expect(screen.getByLabelText('New password')).toHaveValue('')
    expect(changeCalls).toBe(0)
  })

  it('accepts session and CSRF rotation, then reaches Inbox', async () => {
    let logoutCsrf = ''
    server.use(
      requiredSessionHandler(),
      csrfHandler(),
      http.post('/api/v1/auth/password/change', async ({ request }) => {
        expect(request.headers.get('X-CSRF-Token')).toBe('required-session-csrf')
        expect(await request.json()).toEqual({
          current_password: 'temporary password',
          new_password: 'a sufficiently long new password',
        })
        return HttpResponse.json({ ...sessionFixture(), csrf_token: 'rotated-session-csrf' })
      }),
      http.post('/api/v1/auth/logout', ({ request }) => {
        logoutCsrf = request.headers.get('X-CSRF-Token') ?? ''
        return HttpResponse.json({ logged_out: true })
      }),
    )
    renderApp('/password-change')
    await screen.findByRole('heading', { name: 'Change your password' })
    const user = await enterPasswords()
    await user.click(screen.getByRole('button', { name: 'Change password' }))
    expect(await screen.findByRole('heading', { name: 'Inbox' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /Omar Operator/ }))
    await user.click(screen.getByRole('menuitem', { name: 'Logout' }))
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
    expect(logoutCsrf).toBe('rotated-session-csrf')
  })

  it.each([
    ['backend policy rejection', 422, 'password_policy_violation', 'Check the information entered and try again.'],
    ['invalid current password', 401, 'invalid_credentials', 'The username or password is incorrect.'],
    ['throttling', 429, 'authentication_throttled', 'Too many attempts. Try again in 20 seconds.'],
    ['service unavailability', 503, 'authentication_unavailable', 'Authentication is temporarily unavailable. Please try again.'],
  ])('handles %s and clears all password fields', async (_name, status, code, message) => {
    server.use(
      requiredSessionHandler(),
      csrfHandler(),
      http.post('/api/v1/auth/password/change', () =>
        HttpResponse.json(
          { error: { code, request_id: `password-${status}` } },
          { status, headers: status === 429 ? { 'Retry-After': '20' } : {} },
        ),
      ),
    )
    renderApp('/password-change')
    await screen.findByRole('heading', { name: 'Change your password' })
    const user = await enterPasswords()
    await user.click(screen.getByRole('button', { name: 'Change password' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(message)
    expect(screen.getByLabelText('Current password')).toHaveValue('')
    expect(screen.getByLabelText('New password')).toHaveValue('')
    expect(screen.getByLabelText('Confirm new password')).toHaveValue('')
  })

  it('removes the protected page when the session expires during change', async () => {
    server.use(
      requiredSessionHandler(),
      csrfHandler(),
      http.post('/api/v1/auth/password/change', () =>
        HttpResponse.json({ error: { code: 'session_invalid', request_id: 'expired-change' } }, { status: 401 }),
      ),
    )
    renderApp('/password-change')
    await screen.findByRole('heading', { name: 'Change your password' })
    const user = await enterPasswords()
    await user.click(screen.getByRole('button', { name: 'Change password' }))
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toHaveFocus()
    expect(screen.queryByText('Password policy')).not.toBeInTheDocument()
  })

  it('has no automated accessibility violations', async () => {
    server.use(requiredSessionHandler())
    const { container } = renderApp('/password-change')
    await screen.findByRole('heading', { name: 'Change your password' })
    await expectAccessible(container)
  })
})
