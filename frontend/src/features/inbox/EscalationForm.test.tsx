import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { conversationDetailFixture, conversationFixture, sessionFixture } from '../../test/fixtures'
import { renderApp } from '../../test/renderApp'
import { server } from '../../test/server'
import { expectAccessible } from '../../test/accessibility'

const conversationId = '11111111-1111-4111-8111-111111111111'
const endpoint = '/api/v1/operator/conversations/:conversationId/escalations'

function setup(capability = true, alreadyOpen = false) {
  let detailReads = 0
  let queueReads = 0
  const writes: string[] = []
  const session = sessionFixture()
  if (!capability) session.capabilities = session.capabilities.filter((item) => item !== 'escalation.create')
  server.use(
    http.get('/api/v1/auth/session', () => HttpResponse.json(session)),
    http.get('/api/v1/auth/csrf', () => HttpResponse.json({ csrf_token: 'escalation-csrf', expires_at_epoch: 1_900_000_000 })),
    http.get('/api/v1/operator/conversations', () => {
      queueReads++
      return HttpResponse.json({ items: [conversationFixture()], next_cursor: null })
    }),
    http.get('/api/v1/operator/conversations/:conversationId', () => {
      detailReads++
      return HttpResponse.json({ ...conversationDetailFixture(), open_escalation: { exists: alreadyOpen || detailReads > 1 } })
    }),
    http.get('/api/v1/operator/conversations/:conversationId/timeline', () => HttpResponse.json({ items: [], next_older_cursor: null })),
    http.post('/api/v1/operator/*', ({ request }) => {
      writes.push(new URL(request.url).pathname)
      return HttpResponse.json({}, { status: 500 })
    }),
  )
  return { reads: () => ({ detailReads, queueReads }), writes }
}

async function openForm() {
  const user = userEvent.setup()
  await user.click(await screen.findByRole('button', { name: 'Create escalation' }))
  fireEvent.change(screen.getByLabelText('Escalation reason'), { target: { value: '  Customer needs help  ' } })
  return user
}

describe('conversation escalation creation', () => {
  it('creates only an escalation, trims input, preserves control and refreshes authoritative data', async () => {
    const state = setup()
    const requests: Request[] = []
    let body: unknown
    server.use(http.post(endpoint, async ({ request }) => {
      requests.push(request)
      body = await request.json()
      return HttpResponse.json({ escalation_id: 'ticket', status: 'open' }, { status: 201 })
    }))
    const { container } = renderApp(`/inbox/${conversationId}`)
    const user = await openForm()
    await user.selectOptions(screen.getByLabelText('Escalation type'), 'payment_issue')
    await user.selectOptions(screen.getByLabelText('Escalation priority'), 'high')
    await expectAccessible(container)
    await user.click(screen.getByRole('button', { name: 'Submit escalation' }))
    expect(await screen.findByText('Escalation created.')).toBeInTheDocument()
    await waitFor(() => expect(state.reads()).toEqual({ detailReads: 2, queueReads: 2 }))
    expect(body).toEqual({ reason: 'Customer needs help', type: 'payment_issue', priority: 'high' })
    expect(requests).toHaveLength(1)
    expect(requests[0].headers.get('X-CSRF-Token')).toBe('escalation-csrf')
    expect(requests[0].headers.get('Idempotency-Key')).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/)
    expect(state.writes).toEqual([])
    const workspace = within(screen.getByRole('region', { name: 'Marie Client' }))
    expect(within(workspace.getByRole('group', { name: 'Conversation authority' })).getByText('MBB AI Assistant')).toBeInTheDocument()
    expect(workspace.getByText('Open escalation ticket')).toBeInTheDocument()
  })

  it.each(['network', 'IDEMPOTENCY_IN_PROGRESS'])('retries %s with the same frozen payload and key, accepting replay', async (failure) => {
    setup()
    const attempts: Array<{ key: string | null; body: unknown }> = []
    server.use(http.post(endpoint, async ({ request }) => {
      attempts.push({ key: request.headers.get('Idempotency-Key'), body: await request.json() })
      if (attempts.length === 1) return failure === 'network'
        ? HttpResponse.error()
        : HttpResponse.json({ error: { code: failure } }, { status: 409 })
      return HttpResponse.json({ escalation_id: 'ticket' }, { status: 200 })
    }))
    renderApp(`/inbox/${conversationId}`)
    const user = await openForm()
    await user.click(screen.getByRole('button', { name: 'Submit escalation' }))
    await screen.findByRole('button', { name: 'Retry same submission' })
    await user.click(screen.getByRole('button', { name: 'Hide escalation form' }))
    await user.click(screen.getByRole('button', { name: 'Create escalation' }))
    expect(screen.getByLabelText('Escalation reason')).toBeDisabled()
    expect(screen.getByLabelText('Escalation type')).toBeDisabled()
    expect(screen.getByLabelText('Escalation priority')).toBeDisabled()
    await user.click(screen.getByRole('button', { name: 'Retry same submission' }))
    expect(await screen.findByText('Escalation created.')).toBeInTheDocument()
    expect(attempts).toHaveLength(2)
    expect(attempts[1]).toEqual(attempts[0])
  })

  it.each(['ESCALATION_ALREADY_OPEN', 'IDEMPOTENCY_CONFLICT'])('stops duplicate writes and refreshes on %s', async (code) => {
    const state = setup()
    server.use(http.post(endpoint, () => HttpResponse.json({ error: { code } }, { status: 409 })))
    renderApp(`/inbox/${conversationId}`)
    const user = await openForm()
    await user.click(screen.getByRole('button', { name: 'Submit escalation' }))
    await waitFor(() => expect(state.reads()).toEqual({ detailReads: 2, queueReads: 2 }))
    expect(screen.getByRole('button', { name: 'Retry same submission' })).toBeDisabled()
    expect(screen.getByText(code === 'ESCALATION_ALREADY_OPEN'
      ? 'An escalation is already open for this conversation.'
      : 'This submission key conflicts with another request. Refresh and review the conversation before starting again.')).toBeInTheDocument()
  })

  it('validates trimmed character boundaries before writing', async () => {
    const state = setup()
    renderApp(`/inbox/${conversationId}`)
    const user = await openForm()
    for (const reason of ['   ', '123456789', 'a'.repeat(501)]) {
      fireEvent.change(screen.getByLabelText('Escalation reason'), { target: { value: reason } })
      await user.click(screen.getByRole('button', { name: 'Submit escalation' }))
      expect(screen.getByText('Enter a reason containing 10–500 trimmed characters.')).toBeInTheDocument()
    }
    expect(state.writes).toEqual([])
  })

  it.each([10, 500])('accepts %s trimmed Unicode characters and prevents concurrent submissions', async (length) => {
    setup()
    let calls = 0
    let received: unknown
    let release!: () => void
    const pending = new Promise<void>((resolve) => { release = resolve })
    server.use(http.post(endpoint, async ({ request }) => {
      calls++
      received = await request.json()
      await pending
      return HttpResponse.json({ escalation_id: 'ticket' }, { status: 201 })
    }))
    renderApp(`/inbox/${conversationId}`)
    const user = await openForm()
    const reason = '😀'.repeat(length)
    const input = screen.getByLabelText('Escalation reason')
    fireEvent.change(input, { target: { value: `  ${reason}  ` } })
    const form = input.closest('form')!
    fireEvent.submit(form)
    fireEvent.submit(form)
    await waitFor(() => expect(calls).toBe(1))
    expect(received).toEqual({ reason, type: 'complex_issue', priority: 'medium' })
    expect(screen.getByRole('button', { name: 'Submitting escalation…' })).toBeDisabled()
    release()
    expect(await screen.findByText('Escalation created.')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Hide escalation form' }))
  })

  it('keeps successful creation final when the detail refresh fails', async () => {
    setup()
    let created = false
    server.use(
      http.post(endpoint, () => {
        created = true
        return HttpResponse.json({ escalation_id: 'ticket' }, { status: 201 })
      }),
      http.get('/api/v1/operator/conversations/:conversationId', () => created
        ? HttpResponse.json({ error: { code: 'SERVICE_UNAVAILABLE' } }, { status: 503 })
        : HttpResponse.json(conversationDetailFixture())),
    )
    renderApp(`/inbox/${conversationId}`)
    const user = await openForm()
    await user.click(screen.getByRole('button', { name: 'Submit escalation' }))
    expect(await screen.findByRole('button', { name: 'Retry details' })).toBeInTheDocument()
    expect(screen.getByText('Escalation created.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Retry same submission' })).toBeDisabled()
  })

  it('hides the action without capability', async () => {
    setup(false)
    renderApp(`/inbox/${conversationId}`)
    await screen.findByRole('region', { name: 'Marie Client' })
    expect(screen.queryByRole('button', { name: 'Create escalation' })).not.toBeInTheDocument()
  })

  it('disables creation when authoritative detail already has an open escalation', async () => {
    setup(true, true)
    renderApp(`/inbox/${conversationId}`)
    expect(await screen.findByRole('button', { name: 'Create escalation' })).toBeDisabled()
  })
})
