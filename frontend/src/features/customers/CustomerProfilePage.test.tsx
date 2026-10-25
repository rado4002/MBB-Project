import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { expectAccessible } from '../../test/accessibility'
import { sessionFixture } from '../../test/fixtures'
import { renderApp } from '../../test/renderApp'
import { server } from '../../test/server'

const conversationId = '11111111-1111-4111-8111-111111111111'
const evidenceId = '22222222-2222-4222-8222-222222222222'
const base = `/api/v1/business/customers/${conversationId}`
const profile = {
  conversation_id: conversationId,
  display_name: 'Marie Client',
  phone_masked: '***5101',
  city: 'Kinshasa',
  preferred_language: 'french',
  opted_out: false,
  opted_out_at: null,
  consent_given: false,
  consent_timestamp: null,
  communication_channels: [{ channel: 'whatsapp', provenance: 'verified_provider_message' }],
  acquisition_sources: ['referral_partner'],
  opt_out_evidence: [{
    message_id: evidenceId,
    received_at: '2026-10-25T10:00:00Z',
    content_excerpt: 'Arrête, ne plus contacter',
    content_truncated: false,
    provenance: 'inbound_whatsapp_customer_message',
  }],
}

function setup(role: 'operator' | 'administrator') {
  server.use(
    http.get('/api/v1/auth/session', () => HttpResponse.json({
      ...sessionFixture(role),
      recent_reauthentication_expires_at_epoch: 2_000_000_000,
    })),
    http.get(base, () => HttpResponse.json(profile)),
  )
}

describe('Customer Profile & Contact Preferences', () => {
  it('shows masked identity and distinct channel/source provenance read-only to an Operator', async () => {
    setup('operator')
    renderApp(`/business/customers/${conversationId}`)
    expect(await screen.findByRole('heading', { name: 'Profile & contact preferences' })).toBeInTheDocument()
    expect(await screen.findByText('***5101')).toBeInTheDocument()
    expect(screen.getByText('WhatsApp · verified provider message')).toBeInTheDocument()
    expect(screen.getByText('Referral Partner')).toBeInTheDocument()
    expect(screen.getByText(/does not merge identities across channels/)).toBeInTheDocument()
    expect(screen.getByText(/Administrator permission is required/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Record opt-out' })).not.toBeInTheDocument()
    await expectAccessible(document.body)
  })

  it('submits an audited profile correction and evidence-backed opt-out for an Administrator', async () => {
    setup('administrator')
    const updates: unknown[] = []
    const optOuts: unknown[] = []
    server.use(
      http.get('/api/v1/auth/csrf', () => HttpResponse.json({ csrf_token: 'customer-csrf', expires_at_epoch: 2_000_000_000 })),
      http.put(`${base}/profile`, async ({ request }) => {
        const body = await request.json()
        updates.push(body)
        return HttpResponse.json({ ...profile, display_name: 'Marie Updated', city: 'Matadi', preferred_language: 'lingala' })
      }),
      http.post(`${base}/opt-out`, async ({ request }) => {
        optOuts.push(await request.json())
        return HttpResponse.json({ ...profile, opted_out: true, opted_out_at: '2026-10-25T11:00:00Z' })
      }),
    )
    const user = userEvent.setup()
    renderApp(`/business/customers/${conversationId}`)
    await user.clear(await screen.findByLabelText('Name'))
    await user.type(screen.getByLabelText('Name'), 'Marie Updated')
    await user.clear(screen.getByLabelText('City'))
    await user.type(screen.getByLabelText('City'), 'Matadi')
    await user.selectOptions(screen.getByLabelText('Preferred language'), 'lingala')
    await user.selectOptions(screen.getByLabelText('Reason'), 'operator_correction')
    await user.click(screen.getByRole('button', { name: 'Save profile' }))
    expect(await screen.findByText('Customer profile updated and audited.')).toBeInTheDocument()
    expect(updates).toEqual([{
      expected: { display_name: 'Marie Client', city: 'Kinshasa', preferred_language: 'french' },
      profile: { display_name: 'Marie Updated', city: 'Matadi', preferred_language: 'lingala' },
      reason_code: 'operator_correction',
    }])
    await user.selectOptions(screen.getByLabelText('Customer opt-out message'), evidenceId)
    await user.click(screen.getByLabelText(/confirm this message/))
    await user.click(screen.getByRole('button', { name: 'Record opt-out' }))
    expect(await screen.findByText(/Customer opt-out recorded with message evidence/)).toBeInTheDocument()
    expect(optOuts).toEqual([{
      expected_opted_out: false,
      evidence_message_id: evidenceId,
      attested: true,
    }])
    expect(screen.getByText(/Consent cannot be granted or an opt-out reversed/)).toBeInTheDocument()
    await expectAccessible(document.body)
  })
})
