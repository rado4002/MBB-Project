import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { delay, http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import type { BrowserSession } from '../../api/contracts/auth'
import type {
  OperatorConversationDetail,
  OperatorConversationQueueItem,
} from '../../api/contracts/conversations'
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

function humanDetail(): OperatorConversationDetail {
  return {
    ...conversationDetailFixture(),
    ownership: {
      owner_type: 'human',
      human_owner: {
        account_id: 'account-test-only',
        display_name: 'Omar Operator',
      },
      ai_execution_state: 'paused',
      version: 2,
      updated_at: '2026-08-04T10:30:00Z',
    },
  }
}

function humanQueue(): OperatorConversationQueueItem {
  return {
    ...conversationFixture(),
    ownership: humanDetail().ownership,
  }
}

function handlers(
  detail: () => OperatorConversationDetail = conversationDetailFixture,
  queue: () => OperatorConversationQueueItem = conversationFixture,
  session: BrowserSession = sessionFixture(),
) {
  return [
    http.get('/api/v1/auth/session', () => HttpResponse.json(session)),
    http.get('/api/v1/auth/csrf', () =>
      HttpResponse.json({
        csrf_token: 'ownership-csrf',
        expires_at_epoch: 1_900_000_000,
      }),
    ),
    http.get('/api/v1/operator/conversations', () =>
      HttpResponse.json({ items: [queue()], next_cursor: null }),
    ),
    http.get('/api/v1/operator/conversations/:conversationId', () =>
      HttpResponse.json(detail()),
    ),
    http.get('/api/v1/operator/conversations/:conversationId/timeline', () =>
      HttpResponse.json({ items: [messageFixture()], next_older_cursor: null }),
    ),
  ]
}

describe('Human and AI conversation ownership control', () => {
  it('shows exactly one contextual action and no ticket-style fields', async () => {
    server.use(...handlers())
    const user = userEvent.setup()
    const { container } = renderApp('/inbox/' + conversationId)

    const trigger = await screen.findByRole('button', {
      name: 'Escalate to Human',
    })
    expect(screen.queryByRole('button', { name: 'Return to AI' })).not.toBeInTheDocument()
    expect(screen.getAllByText('Controlled by MBB AI Assistant').length).toBeGreaterThan(0)
    await user.click(trigger)

    const dialog = screen.getByRole('dialog', { name: 'Escalate to Human' })
    expect(within(dialog).getByRole('button', { name: 'Take Control' })).toHaveFocus()
    expect(within(dialog).queryByRole('textbox')).not.toBeInTheDocument()
    expect(within(dialog).queryByRole('combobox')).not.toBeInTheDocument()
    expect(within(dialog).queryByText(/type|priority|10–500|reason/i)).not.toBeInTheDocument()
    await expectAccessible(container)
    await expectAccessible(dialog)
  })

  it('takes control once, refreshes detail and queue, swaps the action, and moves focus', async () => {
    let state: 'ai' | 'human' = 'ai'
    let posts = 0
    let detailCalls = 0
    let queueCalls = 0
    let sentBody: unknown
    server.use(
      ...handlers(
        () => {
          detailCalls += 1
          return state === 'ai' ? conversationDetailFixture() : humanDetail()
        },
        () => {
          queueCalls += 1
          return state === 'ai' ? conversationFixture() : humanQueue()
        },
      ),
      http.post('/api/v1/operator/conversations/:conversationId/ownership', async ({ request }) => {
        posts += 1
        sentBody = await request.json()
        await delay(80)
        state = 'human'
        return HttpResponse.json({
          conversation_id: conversationId,
          ownership: humanDetail().ownership,
        })
      }),
    )
    const user = userEvent.setup()
    renderApp('/inbox/' + conversationId)
    await user.click(await screen.findByRole('button', { name: 'Escalate to Human' }))
    const submit = screen.getByRole('button', { name: 'Take Control' })
    await user.dblClick(submit)

    expect(screen.getByRole('button', { name: 'Taking control…' })).toBeDisabled()
    const returnToAi = await screen.findByRole('button', { name: 'Return to AI' })
    expect(returnToAi).toHaveClass('button--secondary')
    expect(screen.queryByRole('button', { name: 'Escalate to Human' })).not.toBeInTheDocument()
    expect(screen.getAllByText(/Controlled by Omar Operator/).length).toBeGreaterThan(0)
    expect(screen.getByText('AI paused')).toBeInTheDocument()
    await waitFor(() =>
      expect(document.querySelector('.ownership-summary')).toHaveFocus(),
    )
    expect(posts).toBe(1)
    expect(sentBody).toEqual({ target_owner_type: 'human', expected_version: 1 })
    expect(detailCalls).toBeGreaterThanOrEqual(2)
    expect(queueCalls).toBeGreaterThanOrEqual(2)
  })

  it('returns human ownership to AI only after confirmation and preserves it after remount', async () => {
    let state: 'human' | 'ai' = 'human'
    const detailWithHistoricalEscalation = () => ({
      ...(state === 'human' ? humanDetail() : conversationDetailFixture()),
      open_escalation: { exists: true },
    })
    const queueWithHistoricalEscalation = () => ({
      ...(state === 'human' ? humanQueue() : conversationFixture()),
      open_escalation: { exists: true },
    })
    server.use(
      ...handlers(
        detailWithHistoricalEscalation,
        queueWithHistoricalEscalation,
      ),
      http.post('/api/v1/operator/conversations/:conversationId/ownership', async ({ request }) => {
        expect(await request.json()).toEqual({
          target_owner_type: 'ai',
          expected_version: 2,
        })
        state = 'ai'
        return HttpResponse.json({
          conversation_id: conversationId,
          ownership: conversationDetailFixture().ownership,
        })
      }),
    )
    const user = userEvent.setup()
    const first = renderApp('/inbox/' + conversationId)
    expect(await screen.findByRole('button', { name: 'Return to AI' })).toBeInTheDocument()
    expect(screen.getAllByText('Open escalation').length).toBeGreaterThan(0)
    expect(screen.queryByRole('button', { name: 'Escalate to Human' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Return to AI' }))
    const dialog = screen.getByRole('dialog', { name: 'Return to AI' })
    await user.click(within(dialog).getByRole('button', { name: 'Return to AI' }))
    expect(await screen.findByRole('button', { name: 'Escalate to Human' })).toBeInTheDocument()
    expect(screen.getAllByText('Open escalation').length).toBeGreaterThan(0)
    first.unmount()

    renderApp('/inbox/' + conversationId)
    expect(await screen.findByRole('button', { name: 'Escalate to Human' })).toBeInTheDocument()
    expect(screen.getAllByText('Controlled by MBB AI Assistant').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Open escalation').length).toBeGreaterThan(0)
  })

  it('keeps human control and announces authoritative AI-disabled errors', async () => {
    server.use(
      ...handlers(humanDetail, humanQueue),
      http.post('/api/v1/operator/conversations/:conversationId/ownership', () =>
        HttpResponse.json(
          {
            error: {
              code: 'AI_DISABLED',
              message: 'The MBB AI Assistant is currently disabled. This conversation remains under human control.',
              request_id: 'ownership-disabled',
            },
          },
          { status: 409 },
        ),
      ),
    )
    const user = userEvent.setup()
    renderApp('/inbox/' + conversationId)
    await user.click(await screen.findByRole('button', { name: 'Return to AI' }))
    const returnButtons = screen.getAllByRole('button', { name: 'Return to AI' })
    await user.click(returnButtons[returnButtons.length - 1])

    expect(await screen.findByText(/currently disabled/)).toBeInTheDocument()
    expect(screen.getByText('ownership-disabled')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Escalate to Human' })).not.toBeInTheDocument()
    expect(screen.getAllByText(/Controlled by Omar Operator/).length).toBeGreaterThan(0)
  })

  it('shows the current owner on conflict and keeps the conversation selected', async () => {
    server.use(
      ...handlers(),
      http.post('/api/v1/operator/conversations/:conversationId/ownership', () =>
        HttpResponse.json(
          {
            error: {
              code: 'OWNERSHIP_CONFLICT',
              message: 'This conversation is now controlled by Alice.',
              request_id: 'ownership-conflict',
            },
          },
          { status: 409 },
        ),
      ),
    )
    const user = userEvent.setup()
    renderApp('/inbox/' + conversationId + '?status=active')
    await user.click(await screen.findByRole('button', { name: 'Escalate to Human' }))
    await user.click(screen.getByRole('button', { name: 'Take Control' }))

    expect(await screen.findByText('This conversation is now controlled by Alice.')).toBeInTheDocument()
    expect(window.location.pathname).toBe('/inbox/' + conversationId)
    expect(window.location.search).toBe('?status=active')
  })

  it('hides the control without permission or authoritative ownership and supports Escape cancellation', async () => {
    const analyst = sessionFixture('analyst')
    server.use(...handlers(conversationDetailFixture, conversationFixture, analyst))
    const first = renderApp('/inbox/' + conversationId)
    await screen.findByText('Solar starter kit')
    expect(screen.queryByRole('button', { name: /Escalate to Human|Return to AI/ })).not.toBeInTheDocument()
    first.unmount()

    server.use(
      http.get('/api/v1/operator/conversations/:conversationId', () =>
        HttpResponse.json(
          { error: { code: 'SERVICE_UNAVAILABLE', request_id: 'ownership-missing' } },
          { status: 503 },
        ),
      ),
      ...handlers(),
    )
    const second = renderApp('/inbox/' + conversationId)
    await screen.findByText(/Conversation data is temporarily unavailable/)
    expect(screen.queryByRole('button', { name: /Escalate to Human|Return to AI/ })).not.toBeInTheDocument()
    second.unmount()

    server.use(...handlers())
    const user = userEvent.setup()
    renderApp('/inbox/' + conversationId)
    const trigger = await screen.findByRole('button', { name: 'Escalate to Human' })
    await user.click(trigger)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    await waitFor(() => expect(trigger).toHaveFocus())
  })
})
