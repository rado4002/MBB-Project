import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { delay, http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import type { BrowserSession, HumanRole } from '../../api/contracts/auth'
import type { OperatorEscalationResponse } from '../../api/contracts/escalations'
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
const reason = 'Customer payment cannot be confirmed.'

const escalationResult: OperatorEscalationResponse = {
  escalation_id: '33333333-3333-4333-8333-333333333333',
  conversation_id: conversationId,
  status: 'open',
  reason,
  type: 'payment_issue',
  priority: 'high',
  source: 'operator_browser',
  created_at: '2026-08-04T10:30:00Z',
  created_by: {
    account_id: '44444444-4444-4444-8444-444444444444',
    display_name: 'Omar Operator',
  },
}

function workspaceHandlers(
  session: BrowserSession = sessionFixture(),
  detail = conversationDetailFixture(),
) {
  return [
    http.get('/api/v1/auth/session', () => HttpResponse.json(session)),
    http.get('/api/v1/auth/csrf', () =>
      HttpResponse.json({ csrf_token: 'csrf-f8', expires_at_epoch: 1_900_000_000 }),
    ),
    http.get('/api/v1/operator/conversations', () =>
      HttpResponse.json({ items: [conversationFixture()], next_cursor: null }),
    ),
    http.get('/api/v1/operator/conversations/:conversationId', () =>
      HttpResponse.json(detail),
    ),
    http.get('/api/v1/operator/conversations/:conversationId/messages', () =>
      HttpResponse.json({ items: [messageFixture()], next_older_cursor: null }),
    ),
  ]
}

function apiError(code: string, status: number, headers?: Record<string, string>) {
  return HttpResponse.json(
    { error: { code, message: 'Safe test error.', request_id: `f8-${status}` } },
    { status, headers },
  )
}

async function openForm(user = userEvent.setup()) {
  const trigger = await screen.findByRole('button', { name: 'Escalate' })
  await user.click(trigger)
  const dialog = screen.getByRole('dialog', { name: 'Create escalation' })
  return { user, trigger, dialog }
}

async function fillValidForm(
  user: ReturnType<typeof userEvent.setup>,
  nextReason = reason,
) {
  await user.selectOptions(screen.getByLabelText('Type'), 'payment_issue')
  await user.selectOptions(screen.getByLabelText('Priority'), 'high')
  await user.type(screen.getByLabelText('Reason'), nextReason)
}

describe('frontend escalation creation', () => {
  it('opens and cancels the accessible modal, traps focus, and returns focus', async () => {
    server.use(...workspaceHandlers())
    renderApp(`/inbox/${conversationId}`)
    const { user, trigger, dialog } = await openForm()

    expect(screen.getByLabelText('Type')).toHaveFocus()
    expect(dialog).toHaveAttribute('aria-modal', 'true')
    await expectAccessible(document.body)

    const cancel = within(dialog).getByRole('button', { name: 'Cancel' })
    const submit = within(dialog).getByRole('button', { name: 'Create escalation' })
    cancel.focus()
    await user.keyboard('{Shift>}{Tab}{/Shift}')
    expect(submit).toHaveFocus()
    await user.click(cancel)

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    await waitFor(() => expect(trigger).toHaveFocus())
  })

  it('supports Escape and validates trimmed reason bounds before any request', async () => {
    let posts = 0
    server.use(
      ...workspaceHandlers(),
      http.post('/api/v1/operator/conversations/:conversationId/escalations', () => {
        posts += 1
        return HttpResponse.json(escalationResult, { status: 201 })
      }),
    )
    renderApp(`/inbox/${conversationId}`)
    const { user, trigger } = await openForm()

    await user.click(screen.getByRole('button', { name: 'Create escalation' }))
    expect(screen.getByText('Choose an escalation type.')).toBeInTheDocument()
    expect(screen.getByLabelText('Type')).toHaveFocus()

    await user.selectOptions(screen.getByLabelText('Type'), 'complex_issue')
    await user.type(screen.getByLabelText('Reason'), '   short   ')
    await user.click(screen.getByRole('button', { name: 'Create escalation' }))
    expect(screen.getByText(/at least 10 characters/i)).toBeInTheDocument()
    expect(screen.getByLabelText('Reason')).toHaveFocus()

    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'x'.repeat(501) } })
    await user.click(screen.getByRole('button', { name: 'Create escalation' }))
    expect(screen.getByText(/no more than 500 characters/i)).toBeInTheDocument()
    expect(posts).toBe(0)

    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    await waitFor(() => expect(trigger).toHaveFocus())
  })

  it('creates once on a double submit, sends only the escalation payload, refreshes detail, and shows the authoritative result', async () => {
    let detailCalls = 0
    let postCalls = 0
    let sentBody: unknown
    let idempotencyKey = ''
    let csrfToken = ''
    server.use(
      http.get('/api/v1/operator/conversations/:conversationId', () => {
        detailCalls += 1
        return HttpResponse.json({
          ...conversationDetailFixture(),
          open_escalation: { exists: detailCalls > 1 },
        })
      }),
      ...workspaceHandlers(),
      http.post('/api/v1/operator/conversations/:conversationId/escalations', async ({ request }) => {
        postCalls += 1
        sentBody = await request.json()
        idempotencyKey = request.headers.get('Idempotency-Key') ?? ''
        csrfToken = request.headers.get('X-CSRF-Token') ?? ''
        await delay(80)
        return HttpResponse.json(escalationResult, { status: 201 })
      }),
    )
    renderApp(`/inbox/${conversationId}`)
    const { user } = await openForm()
    await fillValidForm(user)
    const form = screen.getByRole('button', { name: 'Create escalation' }).closest('form')
    expect(form).not.toBeNull()
    fireEvent.submit(form!)
    fireEvent.submit(form!)

    const resultHeading = await screen.findByRole('heading', { name: 'Escalation created' })
    expect(resultHeading).toBeInTheDocument()
    await waitFor(() => expect(resultHeading).toHaveFocus())
    expect(postCalls).toBe(1)
    expect(sentBody).toEqual({
      type: 'payment_issue',
      priority: 'high',
      reason,
    })
    expect(idempotencyKey).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i,
    )
    expect(csrfToken).toBe('csrf-f8')
    await waitFor(() => expect(detailCalls).toBeGreaterThanOrEqual(2))
    expect(screen.queryByRole('button', { name: 'Escalate' })).not.toBeInTheDocument()
    const resultCard = resultHeading.closest('section')
    expect(resultCard).not.toBeNull()
    expect(within(resultCard!).getByText('Omar Operator')).toBeInTheDocument()
    expect(within(resultCard!).getByText(reason)).toBeInTheDocument()
    expect(within(resultCard!).getByText(escalationResult.escalation_id)).toBeInTheDocument()
    expect(within(resultCard!).getByText('Conversation state and routing were not changed.')).toBeInTheDocument()
    expect(within(screen.getByRole('region', { name: 'Message history' })).getByText(messageFixture().text!)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /reply|send|assign|take over|resolve/i })).not.toBeInTheDocument()
    expect(screen.queryByText(/ownership|handoff/i)).not.toBeInTheDocument()
  })

  it('treats an exact 200 replay as success and shows the same authoritative result', async () => {
    server.use(
      ...workspaceHandlers(),
      http.post('/api/v1/operator/conversations/:conversationId/escalations', () =>
        HttpResponse.json(escalationResult, {
          status: 200,
          headers: { 'Idempotent-Replayed': 'true' },
        }),
      ),
    )
    renderApp(`/inbox/${conversationId}`)
    const { user } = await openForm()
    await fillValidForm(user)
    await user.click(screen.getByRole('button', { name: 'Create escalation' }))

    expect(await screen.findByRole('heading', { name: 'Escalation created' })).toBeInTheDocument()
    expect(screen.getByText(reason)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Escalate' })).not.toBeInTheDocument()
  })

  it('reuses one key and preserves values when an unchanged submission is retried', async () => {
    const keys: string[] = []
    let attempts = 0
    server.use(
      ...workspaceHandlers(),
      http.post('/api/v1/operator/conversations/:conversationId/escalations', ({ request }) => {
        keys.push(request.headers.get('Idempotency-Key') ?? '')
        attempts += 1
        return attempts === 1
          ? apiError('SERVICE_UNAVAILABLE', 503)
          : HttpResponse.json(escalationResult, { status: 201 })
      }),
    )
    renderApp(`/inbox/${conversationId}`)
    const { user } = await openForm()
    await fillValidForm(user)
    await user.click(screen.getByRole('button', { name: 'Create escalation' }))

    expect(await screen.findByText(/temporarily unavailable/i)).toBeInTheDocument()
    expect(screen.getByLabelText('Reason')).toHaveValue(reason)
    expect(screen.getByLabelText('Type')).toHaveValue('payment_issue')
    await user.click(screen.getByRole('button', { name: 'Create escalation' }))

    expect(await screen.findByRole('heading', { name: 'Escalation created' })).toBeInTheDocument()
    expect(keys).toHaveLength(2)
    expect(keys[1]).toBe(keys[0])
  })

  it('generates a new key when the payload changes after a retriable failure', async () => {
    const keys: string[] = []
    let attempts = 0
    server.use(
      ...workspaceHandlers(),
      http.post('/api/v1/operator/conversations/:conversationId/escalations', ({ request }) => {
        keys.push(request.headers.get('Idempotency-Key') ?? '')
        attempts += 1
        return attempts === 1
          ? apiError('IDEMPOTENCY_IN_PROGRESS', 409)
          : HttpResponse.json({ ...escalationResult, reason: `${reason} Updated.` }, { status: 201 })
      }),
    )
    renderApp(`/inbox/${conversationId}`)
    const { user } = await openForm()
    await fillValidForm(user)
    await user.click(screen.getByRole('button', { name: 'Create escalation' }))
    expect(await screen.findByText(/still being processed/i)).toBeInTheDocument()

    await user.type(screen.getByLabelText('Reason'), ' Updated.')
    await user.click(screen.getByRole('button', { name: 'Create escalation' }))
    expect(await screen.findByRole('heading', { name: 'Escalation created' })).toBeInTheDocument()
    expect(keys).toHaveLength(2)
    expect(keys[1]).not.toBe(keys[0])
  })

  it.each([
    ['IDEMPOTENCY_IN_PROGRESS', 409, undefined, /still being processed/i],
    ['IDEMPOTENCY_CONFLICT', 409, undefined, /retry key was used/i],
    ['RATE_LIMITED', 429, { 'Retry-After': '12' }, /try again in 12 seconds/i],
    ['SERVICE_UNAVAILABLE', 503, undefined, /temporarily unavailable/i],
    ['CAPABILITY_REQUIRED', 403, undefined, /do not have permission/i],
  ] as const)(
    'preserves the form for %s without automatic resubmission',
    async (code, status, headers, expectedMessage) => {
      let posts = 0
      server.use(
        ...workspaceHandlers(),
        http.post('/api/v1/operator/conversations/:conversationId/escalations', () => {
          posts += 1
          return apiError(code, status, headers)
        }),
      )
      renderApp(`/inbox/${conversationId}`)
      const { user } = await openForm()
      await fillValidForm(user)
      await user.click(screen.getByRole('button', { name: 'Create escalation' }))

      expect(await screen.findByText(expectedMessage)).toBeInTheDocument()
      expect(screen.getByLabelText('Reason')).toHaveValue(reason)
      expect(screen.getByRole('dialog')).toBeInTheDocument()
      await delay(20)
      expect(posts).toBe(1)
    },
  )

  it('refreshes and reports an already-open escalation without resubmitting', async () => {
    let detailCalls = 0
    let posts = 0
    server.use(
      http.get('/api/v1/operator/conversations/:conversationId', () => {
        detailCalls += 1
        return HttpResponse.json({
          ...conversationDetailFixture(),
          open_escalation: { exists: detailCalls > 1 },
        })
      }),
      ...workspaceHandlers(),
      http.post('/api/v1/operator/conversations/:conversationId/escalations', () => {
        posts += 1
        return apiError('ESCALATION_ALREADY_OPEN', 409)
      }),
    )
    renderApp(`/inbox/${conversationId}`)
    const { user } = await openForm()
    await fillValidForm(user)
    await user.click(screen.getByRole('button', { name: 'Create escalation' }))

    expect((await screen.findAllByText(/already has an active escalation/i)).length).toBeGreaterThan(0)
    await waitFor(() => expect(detailCalls).toBeGreaterThanOrEqual(2))
    expect(posts).toBe(1)
    expect(screen.getByLabelText('Reason')).toHaveValue(reason)
    expect(screen.getByRole('button', { name: 'Create escalation' })).toBeDisabled()
    expect(screen.queryByRole('button', { name: 'Escalate' })).not.toBeInTheDocument()
  })

  it('ends the workspace on session expiration during creation', async () => {
    server.use(
      ...workspaceHandlers(),
      http.post('/api/v1/operator/conversations/:conversationId/escalations', () =>
        apiError('SESSION_INVALID', 401),
      ),
    )
    renderApp(`/inbox/${conversationId}`)
    const { user } = await openForm()
    await fillValidForm(user)
    await user.click(screen.getByRole('button', { name: 'Create escalation' }))

    expect(await screen.findByRole('heading', { name: 'Sign in' })).toHaveFocus()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.queryByText('Marie Client')).not.toBeInTheDocument()
  })

  it.each([
    ['operator', true],
    ['administrator', true],
    ['analyst', false],
  ] as [HumanRole, boolean][])(
    'shows the action for an active %s session: %s',
    async (role, shouldShow) => {
      server.use(...workspaceHandlers(sessionFixture(role)))
      renderApp(`/inbox/${conversationId}`)
      await screen.findByText('Solar starter kit')
      if (shouldShow) {
        expect(screen.getByRole('button', { name: 'Escalate' })).toBeInTheDocument()
      } else {
        expect(screen.queryByRole('button', { name: 'Escalate' })).not.toBeInTheDocument()
      }
    },
  )

  it('hides the action when conversation detail already reports an active escalation', async () => {
    server.use(
      ...workspaceHandlers(sessionFixture(), {
        ...conversationDetailFixture(),
        open_escalation: { exists: true },
      }),
    )
    renderApp(`/inbox/${conversationId}`)

    expect(await screen.findByText('Open escalation')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Escalate' })).not.toBeInTheDocument()
  })
})
