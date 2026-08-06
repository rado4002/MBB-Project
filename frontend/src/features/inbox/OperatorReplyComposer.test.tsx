import { fireEvent, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { delay, http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import type { BrowserSession } from '../../api/contracts/auth'
import type { OperatorConversationDetail } from '../../api/contracts/conversations'
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

const conversationId = '11111111-1111-4111-8111-111111111111'
const replyId = '99999999-9999-4999-8999-999999999999'

function humanDetail(overrides: Partial<OperatorConversationDetail> = {}) {
  return {
    ...conversationDetailFixture(),
    ownership: {
      owner_type: 'human' as const,
      human_owner: {
        account_id: 'account-test-only',
        display_name: 'Omar Operator',
      },
      ai_execution_state: 'paused' as const,
      version: 2,
      updated_at: '2026-08-05T10:00:00Z',
    },
    ...overrides,
  }
}

function acceptedReply(text: string) {
  return messageFixture(replyId, {
    occurred_at: '2026-08-05T10:01:00Z',
    direction: 'outbound',
    sender_type: 'operator',
    operator_author: {
      account_id: 'account-test-only',
      display_name: 'Omar Operator',
    },
    delivery_state: 'accepted',
    delivery_state_timestamp: '2026-08-05T10:01:00Z',
    text,
  })
}

function handlers(
  detail: () => OperatorConversationDetail = humanDetail,
  session: BrowserSession = sessionFixture(),
) {
  return [
    http.get('/api/v1/auth/session', () => HttpResponse.json(session)),
    http.get('/api/v1/auth/csrf', () =>
      HttpResponse.json({
        csrf_token: 'reply-csrf',
        expires_at_epoch: 1_900_000_000,
      }),
    ),
    http.get('/api/v1/operator/conversations', () =>
      HttpResponse.json({ items: [conversationFixture()], next_cursor: null }),
    ),
    http.get('/api/v1/operator/conversations/:conversationId', () =>
      HttpResponse.json(detail()),
    ),
    http.get('/api/v1/operator/conversations/:conversationId/timeline', () =>
      HttpResponse.json({ items: [messageFixture()], next_older_cursor: null }),
    ),
  ]
}

describe('manual Human Operator replies', () => {
  it('submits the authoritative contract and immediately shows Operator authorship as Accepted', async () => {
    let requestBody: unknown
    let csrfHeader = ''
    let idempotencyHeader = ''
    server.use(
      ...handlers(),
      http.post('/api/v1/operator/conversations/:conversationId/replies', async ({ request }) => {
        requestBody = await request.json()
        csrfHeader = request.headers.get('X-CSRF-Token') ?? ''
        idempotencyHeader = request.headers.get('Idempotency-Key') ?? ''
        return HttpResponse.json(acceptedReply('Bonjour Marie'), { status: 202 })
      }),
    )
    const user = userEvent.setup()
    const { container } = renderApp(`/inbox/${conversationId}`)
    const textbox = await screen.findByRole('textbox', { name: 'Reply to Customer' })
    expect(screen.getByRole('radio', { name: 'Reply' })).toBeEnabled()
    expect(screen.queryByText(/^Reply unavailable/)).not.toBeInTheDocument()
    expect(screen.queryByText('Composer mode')).not.toBeInTheDocument()
    expect(screen.getByText('Reply to Customer')).toHaveClass('visually-hidden')
    expect(screen.getByText('Sent to the customer through the conversation channel.'))
      .toBeInTheDocument()
    expect(textbox).toHaveAttribute('maxlength', '4096')
    expect(textbox).toHaveAttribute('rows', '3')

    await user.type(textbox, 'Bonjour Marie')
    await user.click(screen.getByRole('button', { name: 'Submit Reply' }))

    expect(await screen.findByText('Omar Operator — Operator')).toBeInTheDocument()
    expect(screen.getByText('Accepted')).toBeInTheDocument()
    expect(screen.queryByText('Sent')).not.toBeInTheDocument()
    expect(textbox).toHaveValue('')
    await waitFor(() => expect(textbox).toHaveFocus())
    expect(requestBody).toEqual({
      text: 'Bonjour Marie',
      expected_ownership_version: 2,
    })
    expect(csrfHeader).toBe('reply-csrf')
    expect(idempotencyHeader).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    )
    await expectAccessible(container)
  })

  it('rejects blank text and supports Ctrl+Enter keyboard submission', async () => {
    let calls = 0
    server.use(
      ...handlers(),
      http.post('/api/v1/operator/conversations/:conversationId/replies', async ({ request }) => {
        calls += 1
        const body = await request.json() as { text: string }
        return HttpResponse.json(acceptedReply(body.text), { status: 202 })
      }),
    )
    const user = userEvent.setup()
    renderApp(`/inbox/${conversationId}`)
    const textbox = await screen.findByRole('textbox', { name: 'Reply to Customer' })

    await user.type(textbox, '   ')
    await user.click(screen.getByRole('button', { name: 'Submit Reply' }))
    expect(screen.getByText('Enter a reply before submitting.')).toBeInTheDocument()
    expect(calls).toBe(0)

    await user.clear(textbox)
    await user.type(textbox, 'Keyboard reply')
    await user.keyboard('{Control>}{Enter}{/Control}')
    expect(await screen.findByText('Keyboard reply')).toBeInTheDocument()
    expect(calls).toBe(1)
  })

  it('keeps long multiline input editable inside the bounded textarea', async () => {
    server.use(...handlers())
    renderApp(`/inbox/${conversationId}`)
    const textbox = await screen.findByRole('textbox', { name: 'Reply to Customer' })
    const longMultilineDraft = Array.from(
      { length: 12 },
      (_, index) => `Draft line ${index + 1}`,
    ).join('\n')

    fireEvent.change(textbox, { target: { value: longMultilineDraft } })

    expect(textbox).toHaveValue(longMultilineDraft)
    expect(screen.getByText(`${longMultilineDraft.length}/4,096 · Ctrl+Enter to submit`))
      .toBeInTheDocument()
  })

  it('prevents double submission while the first request is pending', async () => {
    let calls = 0
    server.use(
      ...handlers(),
      http.post('/api/v1/operator/conversations/:conversationId/replies', async () => {
        calls += 1
        await delay(100)
        return HttpResponse.json(acceptedReply('Only once'), { status: 202 })
      }),
    )
    const user = userEvent.setup()
    renderApp(`/inbox/${conversationId}`)
    await user.type(
      await screen.findByRole('textbox', { name: 'Reply to Customer' }),
      'Only once',
    )
    const button = screen.getByRole('button', { name: 'Submit Reply' })
    fireEvent.click(button)
    fireEvent.click(button)
    expect(await screen.findByRole('button', { name: 'Submitting…' })).toBeDisabled()
    expect(await screen.findByText('Accepted')).toBeInTheDocument()
    expect(calls).toBe(1)
  })

  it('preserves text and the retry UUID after a recoverable failure', async () => {
    const keys: string[] = []
    let calls = 0
    server.use(
      ...handlers(),
      http.post('/api/v1/operator/conversations/:conversationId/replies', ({ request }) => {
        calls += 1
        keys.push(request.headers.get('Idempotency-Key') ?? '')
        if (calls === 1) {
          return HttpResponse.json(
            { error: { code: 'REPLY_PUBLICATION_FAILED', request_id: 'reply-ref' } },
            { status: 503 },
          )
        }
        return HttpResponse.json(acceptedReply('Preserve me'), { status: 200 })
      }),
    )
    const user = userEvent.setup()
    renderApp(`/inbox/${conversationId}`)
    const textbox = await screen.findByRole('textbox', { name: 'Reply to Customer' })
    await user.type(textbox, 'Preserve me')
    await user.click(screen.getByRole('button', { name: 'Submit Reply' }))

    expect(await screen.findByText('reply-ref')).toBeInTheDocument()
    expect(textbox).toHaveValue('Preserve me')
    await user.click(screen.getByRole('button', { name: 'Submit Reply' }))
    expect(await screen.findByText('Accepted')).toBeInTheDocument()
    expect(keys[0]).toBe(keys[1])
  })

  it.each([
    [
      'AI control',
      conversationDetailFixture(),
      sessionFixture(),
      'Reply unavailable — this conversation is controlled by MBB AI Assistant.',
    ],
    [
      'another Human Operator for an Administrator',
      humanDetail({
        ownership: {
          ...humanDetail().ownership,
          human_owner: { account_id: 'other-account', display_name: 'Other Operator' },
        },
      }),
      sessionFixture('administrator'),
      'Reply unavailable — only Other Operator may reply.',
    ],
    [
      'another Human Operator for an Operator',
      humanDetail({
        ownership: {
          ...humanDetail().ownership,
          human_owner: { account_id: 'other-account', display_name: 'Other Operator' },
        },
      }),
      sessionFixture('operator'),
      'Reply unavailable — only Other Operator may reply.',
    ],
    [
      'missing reply permission',
      humanDetail(),
      {
        ...sessionFixture(),
        capabilities: sessionFixture().capabilities.filter((item) => item !== 'message.reply'),
      },
      'Reply unavailable — your account does not have permission to reply.',
    ],
    [
      'non-reply-eligible status',
      humanDetail({ status: 'dormant' }),
      sessionFixture(),
      'Reply unavailable — this conversation is not currently eligible for replies.',
    ],
  ])('explains unavailable Reply and preserves Internal Note for %s', async (
    _case,
    detail,
    session,
    expectedReason,
  ) => {
    server.use(...handlers(() => detail, session))
    renderApp(`/inbox/${conversationId}`)
    await screen.findByRole('heading', { name: 'Timeline' })
    const replyMode = screen.getByRole('radio', { name: 'Reply' })
    expect(replyMode).toBeDisabled()
    const explanation = await screen.findByText(expectedReason)
    expect(replyMode).toHaveAccessibleDescription(expectedReason)
    expect(explanation).toHaveAttribute('role', 'note')
    expect(explanation).toHaveAttribute('tabindex', '0')
    expect(screen.queryByRole('textbox', { name: 'Reply to Customer' })).not.toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Internal Note' })).toBeInTheDocument()
  })

  it('keeps Internal Note available and explains Reply when ownership data is unavailable', async () => {
    server.use(
      http.get('/api/v1/operator/conversations/:conversationId', () =>
        HttpResponse.json(
          { error: { code: 'SERVICE_UNAVAILABLE', request_id: 'ownership-unavailable' } },
          { status: 503 },
        ),
      ),
      ...handlers(),
    )
    renderApp(`/inbox/${conversationId}`)
    expect(await screen.findByText('ownership-unavailable')).toBeInTheDocument()
    expect(screen.getByRole('radio', { name: 'Reply' })).toBeDisabled()
    expect(screen.getByText('Reply unavailable — conversation ownership is unavailable.'))
      .toHaveAttribute('role', 'note')
    expect(screen.queryByRole('textbox', { name: 'Reply to Customer' })).not.toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Internal Note' })).toBeInTheDocument()
  })

  it('removes the composer when refreshed ownership changes after acceptance', async () => {
    let detailCalls = 0
    server.use(
      ...handlers(() => {
        detailCalls += 1
        return detailCalls === 1 ? humanDetail() : conversationDetailFixture()
      }),
      http.post('/api/v1/operator/conversations/:conversationId/replies', () =>
        HttpResponse.json(acceptedReply('Ownership refresh'), { status: 202 }),
      ),
    )
    const user = userEvent.setup()
    renderApp(`/inbox/${conversationId}`)
    const textbox = await screen.findByRole('textbox', { name: 'Reply to Customer' })
    await user.type(textbox, 'Ownership refresh')
    await user.click(screen.getByRole('button', { name: 'Submit Reply' }))

    expect(await screen.findByText('Accepted')).toBeInTheDocument()
    await waitFor(() =>
      expect(screen.queryByRole('textbox', { name: 'Reply to Customer' })).not.toBeInTheDocument(),
    )
    expect(screen.getAllByText('Controlled by MBB AI Assistant').length).toBeGreaterThan(0)
  })
})

describe('internal conversation notes', () => {
  it('keeps Reply and Internal Note drafts separate and renders escaped internal content', async () => {
    const noteText = '  Private — 你好\n<script>alert(1)</script>  '
    let requestBody: unknown
    let idempotencyHeader = ''
    let replyIdempotencyHeader = ''
    server.use(
      ...handlers(),
      http.post('/api/v1/operator/conversations/:conversationId/notes', async ({ request }) => {
        requestBody = await request.json()
        idempotencyHeader = request.headers.get('Idempotency-Key') ?? ''
        return HttpResponse.json(internalNoteFixture(undefined, { text: noteText }), {
          status: 201,
        })
      }),
      http.post('/api/v1/operator/conversations/:conversationId/replies', ({ request }) => {
        replyIdempotencyHeader = request.headers.get('Idempotency-Key') ?? ''
        return HttpResponse.json(acceptedReply('Reply draft stays here'), { status: 202 })
      }),
    )
    const user = userEvent.setup()
    const { container } = renderApp(`/inbox/${conversationId}`)
    const reply = await screen.findByRole('textbox', { name: 'Reply to Customer' })
    await user.type(reply, 'Reply draft stays here')
    await user.click(screen.getByRole('radio', { name: 'Internal Note' }))

    const note = screen.getByRole('textbox', { name: 'Internal Note' })
    expect(note).toHaveValue('')
    expect(screen.getByText('Internal only — not sent to the customer or available to AI.'))
      .toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Add Internal Note' })).toBeInTheDocument()
    await user.type(note, noteText)
    await user.keyboard('{Control>}{Enter}{/Control}')

    const card = await screen.findByRole('article', { name: 'Internal note by Omar Operator' })
    expect(card).toBeInTheDocument()
    expect(card.querySelector('.message-text')?.textContent).toBe(noteText)
    expect(container.querySelector('script')).not.toBeInTheDocument()
    expect(screen.queryByText('Accepted')).not.toBeInTheDocument()
    expect(screen.queryByText('Sent')).not.toBeInTheDocument()
    expect(note).toHaveValue('')
    expect(requestBody).toEqual({ text: noteText })
    expect(idempotencyHeader).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    )
    expect(window.location.href).not.toContain(encodeURIComponent(noteText))
    expect(window.localStorage.length).toBe(0)
    expect(window.sessionStorage.length).toBe(0)

    await user.click(screen.getByRole('radio', { name: 'Reply' }))
    expect(screen.getByRole('textbox', { name: 'Reply to Customer' }))
      .toHaveValue('Reply draft stays here')
    await user.click(screen.getByRole('button', { name: 'Submit Reply' }))
    expect(await screen.findByText('Accepted')).toBeInTheDocument()
    expect(replyIdempotencyHeader).not.toBe(idempotencyHeader)
    await expectAccessible(container)
  })

  it('preserves note text and retry UUID after a transient failure', async () => {
    const keys: string[] = []
    let calls = 0
    server.use(
      ...handlers(),
      http.post('/api/v1/operator/conversations/:conversationId/notes', ({ request }) => {
        calls += 1
        keys.push(request.headers.get('Idempotency-Key') ?? '')
        if (calls === 1) {
          return HttpResponse.json(
            { error: { code: 'SERVICE_UNAVAILABLE', request_id: 'note-ref' } },
            { status: 503 },
          )
        }
        return HttpResponse.json(
          internalNoteFixture(undefined, { text: 'Preserve private note' }),
          { status: 200 },
        )
      }),
    )
    const user = userEvent.setup()
    renderApp(`/inbox/${conversationId}`)
    await screen.findByRole('textbox', { name: 'Reply to Customer' })
    await user.click(screen.getByRole('radio', { name: 'Internal Note' }))
    const note = screen.getByRole('textbox', { name: 'Internal Note' })
    await user.type(note, 'Preserve private note')
    await user.click(screen.getByRole('button', { name: 'Add Internal Note' }))

    expect(await screen.findByText('note-ref')).toBeInTheDocument()
    expect(note).toHaveValue('Preserve private note')
    await user.click(screen.getByRole('button', { name: 'Add Internal Note' }))
    expect(await screen.findByRole('article', { name: 'Internal note by Omar Operator' }))
      .toBeInTheDocument()
    expect(keys[0]).toBe(keys[1])
  })

  it('uses a new retry UUID after an idempotency conflict without duplicating the timeline', async () => {
    const keys: string[] = []
    let calls = 0
    server.use(
      ...handlers(),
      http.post('/api/v1/operator/conversations/:conversationId/notes', ({ request }) => {
        calls += 1
        keys.push(request.headers.get('Idempotency-Key') ?? '')
        if (calls === 1) {
          return HttpResponse.json(
            { error: { code: 'IDEMPOTENCY_CONFLICT', request_id: 'note-conflict' } },
            { status: 409 },
          )
        }
        return HttpResponse.json(
          internalNoteFixture(undefined, { text: 'Conflict-safe note' }),
          { status: 201 },
        )
      }),
    )
    const user = userEvent.setup()
    renderApp(`/inbox/${conversationId}`)
    await screen.findByRole('textbox', { name: 'Reply to Customer' })
    await user.click(screen.getByRole('radio', { name: 'Internal Note' }))
    const note = screen.getByRole('textbox', { name: 'Internal Note' })
    await user.type(note, 'Conflict-safe note')
    await user.click(screen.getByRole('button', { name: 'Add Internal Note' }))

    expect(await screen.findByText('note-conflict')).toBeInTheDocument()
    expect(note).toHaveValue('Conflict-safe note')
    await user.click(screen.getByRole('button', { name: 'Add Internal Note' }))
    await screen.findByRole('article', { name: 'Internal note by Omar Operator' })
    expect(keys[0]).not.toBe(keys[1])
    expect(screen.getAllByRole('article', { name: 'Internal note by Omar Operator' }))
      .toHaveLength(1)
  })
})
