import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import type { OperatorAccountSummary } from '../../api/contracts/operatorAccounts'
import { expectAccessible } from '../../test/accessibility'
import { sessionFixture } from '../../test/fixtures'
import { renderApp } from '../../test/renderApp'
import { server } from '../../test/server'

const activeOperator: OperatorAccountSummary = {
  account_id: '11111111-1111-4111-8111-111111111111',
  username: 'amina.operator',
  display_name: 'Amina Operator',
  email: 'amina@example.test',
  status: 'active',
  last_login_at: '2026-08-08T12:00:00Z',
  created_at: '2026-08-01T12:00:00Z',
}

const disabledOperator: OperatorAccountSummary = {
  account_id: '22222222-2222-4222-8222-222222222222',
  username: 'bayo.operator',
  display_name: 'Bayo Operator',
  email: null,
  status: 'disabled',
  last_login_at: null,
  created_at: '2026-08-02T12:00:00Z',
}

function administratorSession(recent = true) {
  return {
    ...sessionFixture('administrator'),
    recent_reauthentication_expires_at_epoch: recent ? 1_900_000_000 : null,
  }
}

function pageHandlers(recent = true) {
  return [
    http.get('/api/v1/auth/session', () => HttpResponse.json(administratorSession(recent))),
    http.get('/api/v1/auth/csrf', () =>
      HttpResponse.json({ csrf_token: 'account-csrf', expires_at_epoch: 1_900_000_000 }),
    ),
    http.get('/api/v1/operator/accounts', () =>
      HttpResponse.json({ items: [activeOperator, disabledOperator] }),
    ),
  ]
}

describe('Administrator Operator accounts page', () => {
  it('shows the Operators navigation to Administrators', async () => {
    server.use(...pageHandlers())
    renderApp('/operators')
    expect(await screen.findByRole('heading', { name: 'Operators' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Operators' })).toBeInTheDocument()
  })

  it.each(['operator', 'analyst'] as const)('rejects direct %s route access', async (role) => {
    server.use(
      http.get('/api/v1/auth/session', () => HttpResponse.json(sessionFixture(role))),
    )
    renderApp('/operators')
    await waitFor(() => expect(window.location.pathname).toBe('/inbox'))
    expect(screen.queryByRole('link', { name: 'Operators' })).not.toBeInTheDocument()
  })

  it('renders active and disabled states with the narrow action set', async () => {
    server.use(...pageHandlers())
    const { container } = renderApp('/operators')
    await screen.findByRole('heading', { name: 'Operators' })
    const activeCard = (await screen.findByRole('heading', { name: 'Amina Operator' })).closest('article')
    const disabledCard = screen.getByRole('heading', { name: 'Bayo Operator' }).closest('article')
    expect(activeCard).not.toBeNull()
    expect(disabledCard).not.toBeNull()
    expect(within(activeCard!).getByText('Active')).toBeInTheDocument()
    expect(within(activeCard!).getByRole('button', { name: 'Set New Password' })).toBeInTheDocument()
    expect(within(activeCard!).getByRole('button', { name: 'Disable' })).toBeInTheDocument()
    expect(within(disabledCard!).getByText('Disabled')).toBeInTheDocument()
    expect(within(disabledCard!).getByRole('button', { name: 'Re-enable' })).toBeInTheDocument()
    await expectAccessible(container)
  })

  it('creates an Operator from an accessible dialog and uses the authoritative response', async () => {
    let received: unknown
    const created: OperatorAccountSummary = {
      account_id: '33333333-3333-4333-8333-333333333333',
      username: 'new.operator',
      display_name: 'New Operator',
      email: null,
      status: 'active',
      last_login_at: null,
      created_at: '2026-08-09T12:00:00Z',
    }
    server.use(
      ...pageHandlers(),
      http.post('/api/v1/operator/accounts', async ({ request }) => {
        expect(request.headers.get('X-CSRF-Token')).toBe('account-csrf')
        received = await request.json()
        return HttpResponse.json(created, { status: 201 })
      }),
    )
    const user = userEvent.setup()
    renderApp('/operators')
    const trigger = await screen.findByRole('button', { name: '+ Create Operator' })
    await user.click(trigger)
    const dialog = screen.getByRole('dialog', { name: 'Create Operator' })
    await expectAccessible(document.body)
    await user.type(within(dialog).getByLabelText('Username'), 'new.operator')
    await user.type(within(dialog).getByLabelText('Display name'), 'New Operator')
    await user.type(within(dialog).getByLabelText('Password'), 'Cobalt-River-83!')
    await user.type(within(dialog).getByLabelText('Confirm password'), 'Cobalt-River-83!')
    await user.click(within(dialog).getByRole('button', { name: 'Create Operator' }))
    expect(await screen.findByText('New Operator can now sign in as an Operator.')).toBeInTheDocument()
    expect(received).toEqual({
      username: 'new.operator',
      display_name: 'New Operator',
      email: null,
      password: 'Cobalt-River-83!',
    })
    expect(screen.queryByDisplayValue('Cobalt-River-83!')).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'New Operator' })).toBeInTheDocument()
  })

  it('requires recent Administrator confirmation and restores focus on Escape', async () => {
    server.use(
      ...pageHandlers(false),
      http.post('/api/v1/auth/reauthenticate', async ({ request }) => {
        expect(await request.json()).toEqual({ password: 'Administrator-Only-42!' })
        return HttpResponse.json({
          ...administratorSession(true),
          csrf_token: 'rotated-account-csrf',
        })
      }),
    )
    const user = userEvent.setup()
    renderApp('/operators')
    const trigger = await screen.findByRole('button', { name: '+ Create Operator' })
    await user.click(trigger)
    const reauth = screen.getByRole('dialog', { name: 'Confirm Administrator password' })
    await user.type(within(reauth).getByLabelText('Administrator password'), 'Administrator-Only-42!')
    await user.click(within(reauth).getByRole('button', { name: 'Confirm' }))
    expect(await screen.findByRole('dialog', { name: 'Create Operator' })).toBeInTheDocument()
    expect(screen.queryByDisplayValue('Administrator-Only-42!')).not.toBeInTheDocument()
    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(trigger).toHaveFocus()
  })

  it('resets, disables, and re-enables only from authoritative responses', async () => {
    server.use(
      ...pageHandlers(),
      http.post('/api/v1/operator/accounts/:accountId/password', () =>
        HttpResponse.json({ ...activeOperator }),
      ),
      http.post('/api/v1/operator/accounts/:accountId/disable', () =>
        HttpResponse.json({ ...activeOperator, status: 'disabled' }),
      ),
      http.post('/api/v1/operator/accounts/:accountId/enable', () =>
        HttpResponse.json({ ...disabledOperator, status: 'active' }),
      ),
    )
    const user = userEvent.setup()
    renderApp('/operators')
    const activeHeading = await screen.findByRole('heading', { name: 'Amina Operator' })
    const activeCard = activeHeading.closest('article')!
    await user.click(within(activeCard).getByRole('button', { name: 'Set New Password' }))
    let dialog = screen.getByRole('dialog', { name: 'Set New Password' })
    await user.type(within(dialog).getByLabelText('New password'), 'Sunset-Lantern-74!')
    await user.type(within(dialog).getByLabelText('Confirm new password'), 'Sunset-Lantern-74!')
    await user.click(within(dialog).getByRole('button', { name: 'Set New Password' }))
    expect(await screen.findByText(/password was updated/)).toBeInTheDocument()

    await user.click(within(activeCard).getByRole('button', { name: 'Disable' }))
    dialog = screen.getByRole('dialog', { name: 'Disable Operator' })
    expect(dialog).toHaveTextContent('historical messages, notes, and audit attribution will be preserved')
    await user.click(within(dialog).getByRole('button', { name: 'Disable Operator' }))
    await waitFor(() => expect(within(activeCard).getByText('Disabled')).toBeInTheDocument())

    const disabledCard = screen.getByRole('heading', { name: 'Bayo Operator' }).closest('article')!
    await user.click(within(disabledCard).getByRole('button', { name: 'Re-enable' }))
    dialog = screen.getByRole('dialog', { name: 'Re-enable Operator' })
    await user.type(within(dialog).getByLabelText('New password'), 'Marble-Window-61!')
    await user.type(within(dialog).getByLabelText('Confirm new password'), 'Marble-Window-61!')
    await user.click(within(dialog).getByRole('button', { name: 'Re-enable Operator' }))
    await waitFor(() => expect(within(disabledCard).getByText('Active')).toBeInTheDocument())
  })

  it('clears passwords after a failed request and never renders the secret', async () => {
    server.use(
      ...pageHandlers(),
      http.post('/api/v1/operator/accounts/:accountId/password', () =>
        HttpResponse.json(
          { error: { code: 'PASSWORD_POLICY_VIOLATION', message: 'The password does not meet the password policy.', request_id: 'safe-reference' } },
          { status: 422 },
        ),
      ),
    )
    const user = userEvent.setup()
    renderApp('/operators')
    const card = (await screen.findByRole('heading', { name: 'Amina Operator' })).closest('article')!
    await user.click(within(card).getByRole('button', { name: 'Set New Password' }))
    const dialog = screen.getByRole('dialog', { name: 'Set New Password' })
    const secret = 'Rejected-Secret-90!'
    await user.type(within(dialog).getByLabelText('New password'), secret)
    await user.type(within(dialog).getByLabelText('Confirm new password'), secret)
    await user.click(within(dialog).getByRole('button', { name: 'Set New Password' }))
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('does not meet the password policy')
    expect(within(dialog).getByLabelText('New password')).toHaveValue('')
    expect(within(dialog).getByLabelText('Confirm new password')).toHaveValue('')
    expect(document.body).not.toHaveTextContent(secret)
  })
})
