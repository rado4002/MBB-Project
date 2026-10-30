import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { expectAccessible } from '../../test/accessibility'
import { sessionFixture } from '../../test/fixtures'
import { renderApp } from '../../test/renderApp'
import { server } from '../../test/server'

const conversationId = '11111111-1111-4111-8111-111111111111'
const customerReference = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
const profile = {
  customer_id: '+243810005101',
  conversation_id: conversationId,
  display_name: 'Marie Client',
  phone_number: '+243810005101',
  city: 'Kinshasa',
  preferred_language: 'french',
  opted_out: false,
  opted_out_at: null,
  consent_given: false,
  consent_timestamp: null,
  communication_channels: [{ channel: 'whatsapp', provenance: 'verified_provider_message' }],
  acquisition_sources: ['referral_partner'],
  opt_out_evidence: [],
}

function setup() {
  server.use(
    http.get('/api/v1/auth/session', () => HttpResponse.json(sessionFixture('operator'))),
    http.get('/api/v1/business/customers', ({ request }) => {
      const url = new URL(request.url)
      return HttpResponse.json({
        items: [{
          customer_id: '+243810005101',
          customer_reference: customerReference,
          conversation_id: conversationId,
          display_name: 'Marie Client',
          phone_number: '+243810005101',
          communication_channel: 'whatsapp',
          acquisition_source: 'referral_partner',
          acquisition_source_provenance: 'earliest_recorded_lead',
          last_interaction_at: '2026-10-25T10:00:00Z',
        }],
        has_more: false,
        observed_query: url.searchParams.get('q'),
      })
    }),
    http.get(`/api/v1/business/customers/${conversationId}`, () => HttpResponse.json(profile)),
    http.get('/api/v1/business/customers/by-reference/:customerReference', () => HttpResponse.json(profile)),
    http.get(`/api/v1/operator/conversations/${conversationId}/customer-activity`, () => HttpResponse.json({
      customer: { display_name: 'Marie Client', phone_masked: '***5101', city: 'Kinshasa', preferred_language: 'french', opted_out: false, consent_given: false },
      conversations: [{ conversation_id: conversationId, status: 'active', language: 'french', last_activity_at: '2026-10-25T10:00:00Z', ownership: { owner_type: 'ai', human_owner: null, ai_execution_state: 'eligible', version: 1, updated_at: '2026-10-25T10:00:00Z' }, communication_channel: 'whatsapp', acquisition_source: 'referral_partner', is_current: true }],
      orders: [],
    })),
  )
}

describe('Customers workspace', () => {
  it('finds a Customer, shows distinct authority fields, and returns to the same overview state', async () => {
    setup()
    const user = userEvent.setup()
    renderApp('/business/customers?q=Marie&offset=25')
    expect(await screen.findByRole('heading', { name: 'Customers' })).toBeInTheDocument()
    expect(await screen.findByText(/Customer ID · \+243810005101/)).toBeInTheDocument()
    expect(screen.getByText('WhatsApp')).toBeInTheDocument()
    expect(screen.getByText('Referral Partner')).toBeInTheDocument()
    await user.click(screen.getByRole('link', { name: 'Marie Client' }))
    expect(window.location.pathname).toBe(`/business/customers/ref/${customerReference}`)
    expect(await screen.findByRole('heading', { name: 'Profile & contact preferences' })).toBeInTheDocument()
    expect(screen.getByText('WhatsApp · verified provider message')).toBeInTheDocument()
    expect(screen.getAllByText('+243810005101')).toHaveLength(2)
    expect(screen.getByText('Preferred contact channel').nextSibling).toHaveTextContent('Not recorded')
    expect(await screen.findByRole('heading', { name: 'Relationship & activity' })).toBeInTheDocument()
    await user.click(screen.getByRole('link', { name: 'Back to Customers' }))
    await waitFor(() => expect(window.location.search).toBe('?q=Marie&offset=25'))
    expect(await screen.findByDisplayValue('Marie')).toBeInTheDocument()
    await expectAccessible(document.body)
  })

  it('supports filtered empty and retryable error states', async () => {
    server.use(
      http.get('/api/v1/auth/session', () => HttpResponse.json(sessionFixture('operator'))),
      http.get('/api/v1/business/customers', () => HttpResponse.json({ items: [], has_more: false })),
    )
    const user = userEvent.setup()
    renderApp('/business/customers?q=missing')
    expect(await screen.findByRole('heading', { name: 'No Customers match this search' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Previous' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled()
    await user.clear(screen.getByLabelText('Find by name or last 4 phone digits'))
    await user.type(screen.getByLabelText('Find by name or last 4 phone digits'), '+243810005101')
    await user.click(screen.getByRole('button', { name: 'Search' }))
    await waitFor(() => expect(window.location.search).toBe('?q=5101'))
    expect(window.location.href).not.toContain('%2B243810005101')
  })

  it('keeps a permanent Customer in the index without a Conversation or Lead', async () => {
    const orphanProfile = {
      ...profile,
      customer_id: '+243810005103',
      conversation_id: null,
      display_name: 'Customer Without Conversation',
      phone_number: '+243810005103',
      communication_channels: [],
      acquisition_sources: [],
    }
    server.use(
      http.get('/api/v1/auth/session', () => HttpResponse.json(sessionFixture('administrator'))),
      http.get('/api/v1/business/customers', () => HttpResponse.json({
        items: [{
          customer_id: '+243810005103',
          customer_reference: 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb',
          conversation_id: null,
          display_name: 'Customer Without Conversation',
          phone_number: '+243810005103',
          communication_channel: null,
          acquisition_source: null,
          acquisition_source_provenance: null,
          last_interaction_at: '2026-10-25T10:00:00Z',
        }],
        has_more: false,
      })),
      http.get('/api/v1/business/customers/by-reference/:customerReference', () => HttpResponse.json(orphanProfile)),
    )
    const user = userEvent.setup()
    renderApp('/business/customers')
    await user.click(await screen.findByRole('link', { name: 'Customer Without Conversation' }))
    expect(await screen.findByRole('heading', { name: 'Profile & contact preferences' })).toBeInTheDocument()
    expect(screen.getAllByText('+243810005103')).toHaveLength(2)
    expect(screen.getByText('No accessible conversations.')).toBeInTheDocument()
    expect(screen.getByText('No recent Orders.')).toBeInTheDocument()
    expect(screen.getByText(/require an authoritative Customer conversation context/)).toBeInTheDocument()
  })
})
