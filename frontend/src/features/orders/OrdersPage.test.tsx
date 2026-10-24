import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { expectAccessible } from '../../test/accessibility'
import { sessionFixture } from '../../test/fixtures'
import { renderApp } from '../../test/renderApp'
import { server } from '../../test/server'

const orderId = '11111111-1111-4111-8111-111111111111'
const conversationId = '22222222-2222-4222-8222-222222222222'
const productId = '33333333-3333-4333-8333-333333333333'
const itemId = '44444444-4444-4444-8444-444444444444'
const evidenceId = '77777777-7777-4777-8777-777777777777'
const base = '/api/v1/business/orders'
const summary = {
  order_id: orderId, status: 'pending', created_at: '2026-10-01T12:00:00Z',
  updated_at: '2026-10-01T12:00:00Z',
  total_cdf: '309400.00', currency: 'CDF', item_count: 1,
  first_product_name: 'Fictional Air Fryer', conversation_id: conversationId,
}
const detail = {
  ...summary, delivery_zone: 'Kinshasa', delivered_at: null, items: [{
    product_id: productId, sellable_item_id: itemId, product_name: 'Fictional Air Fryer',
    model_label: '6L', quantity: 2, price_id: '55555555-5555-4555-8555-555555555555',
    unit_price_usd: '55.25', exchange_rate_id: '66666666-6666-4666-8666-666666666666',
    usd_to_cdf_rate: '2800.000000', unit_price_cdf: '154700.00',
  }],
  preparation: {
    eligible: false, order_state: 'not_confirmed', payment_state: 'missing',
    allocation_state: 'missing', required_quantity: 2, allocated_quantity: null,
  },
  dispatch: {
    eligible: false, order_state: 'not_preparing', payment_state: 'missing',
    allocation_state: 'missing', inventory_state: 'conflict',
    delivery_method_state: 'supported', required_quantity: 2,
  },
  delivery: {
    eligible: false, order_state: 'not_delivering', delivery_method_state: 'supported',
    line_state: 'single', dispatch_state: 'missing', association_state: 'not_applicable',
    evidence_state: 'not_applicable', dispatch_occurred_at: null,
    evidence_messages: [], evidence_has_more: false,
  },
}

function setup(conversation: string | null = conversationId, role: 'operator' | 'administrator' = 'operator') {
  server.use(
    http.get('/api/v1/auth/session', () => HttpResponse.json({
      ...sessionFixture(role),
      recent_reauthentication_expires_at_epoch: role === 'administrator' ? 2_000_000_000 : null,
    })),
    http.get(base, ({ request }) => HttpResponse.json({
      items: new URL(request.url).searchParams.get('status') === 'delivered' ? [] : [summary],
      has_more: false,
    })),
    http.get(`${base}/${orderId}`, () => HttpResponse.json({ ...detail, conversation_id: conversation })),
  )
}

describe('read-only Business Orders', () => {
  it('shows the accepted snapshot and associated Inbox link without actions', async () => {
    setup()
    const user = userEvent.setup()
    renderApp('/business/orders')
    await user.click(await screen.findByRole('link', { name: /Fictional Air Fryer.*pending/ }))
    const region = await screen.findByRole('region', { name: 'Order detail' })
    expect(await within(region).findByText('pending')).toBeInTheDocument()
    expect(within(region).getByText('CDF 309,400.00')).toBeInTheDocument()
    expect(within(region).getByText(/Accepted USD price USD 55.25/)).toBeInTheDocument()
    expect(within(region).getByText(/Accepted USD\/CDF rate 2800/)).toBeInTheDocument()
    expect(within(region).getAllByText('No Payment recorded')).toHaveLength(2)
    expect(within(region).getAllByText('No durable Stock allocation recorded')).toHaveLength(2)
    expect(within(region).getByRole('link', { name: 'Open associated Inbox conversation' })).toHaveAttribute('href', `/inbox/${conversationId}`)
    expect(within(region).queryByRole('button')).not.toBeInTheDocument()
  })

  it('lets an authorized Administrator confirm preparation using reviewed evidence', async () => {
    const submissions: { body: unknown; key: string | null; csrf: string | null }[] = []
    setup(conversationId, 'administrator')
    server.use(
      http.get(`${base}/${orderId}`, () => HttpResponse.json({
        ...detail,
        status: 'confirmed',
        preparation: {
          eligible: true, order_state: 'confirmed', payment_state: 'verified',
          allocation_state: 'full', required_quantity: 2, allocated_quantity: 2,
        },
      })),
      http.get('/api/v1/auth/csrf', () => HttpResponse.json({ csrf_token: 'prepare-csrf', expires_at_epoch: 2_000_000_000 })),
      http.post(`${base}/${orderId}/prepare`, async ({ request }) => {
        submissions.push({
          body: await request.json(),
          key: request.headers.get('Idempotency-Key'),
          csrf: request.headers.get('X-CSRF-Token'),
        })
        return HttpResponse.json({
          order_id: orderId, status: 'preparing',
          updated_at: '2026-10-01T12:02:00Z', replayed: false,
        })
      }),
    )
    const user = userEvent.setup()
    renderApp(`/business/orders/${orderId}`)
    const region = await screen.findByRole('region', { name: 'Order detail' })
    expect(await within(region).findByText('Ready to prepare')).toBeInTheDocument()
    expect(within(region).getByText('Verified completed Payment matches the accepted Order')).toBeInTheDocument()
    expect(within(region).getByText('Full allocation · 2 of 2')).toBeInTheDocument()
    await expectAccessible(document.body)
    await user.click(within(region).getByRole('button', { name: 'Prepare Order' }))
    expect(within(region).getByText(/does not dispatch, deliver, or settle Payment/)).toBeInTheDocument()
    await expectAccessible(document.body)
    await user.click(within(region).getByRole('button', { name: 'Confirm preparation' }))
    expect(await within(region).findByText('Order preparation started.')).toBeInTheDocument()
    expect(within(region).getByText('preparing')).toBeInTheDocument()
    expect(submissions).toHaveLength(1)
    expect(submissions[0]).toMatchObject({
      body: { expected_updated_at: detail.updated_at }, csrf: 'prepare-csrf',
    })
    expect(submissions[0]?.key).toMatch(/^[0-9a-f-]{36}$/)
  })

  it('refreshes evidence after a conflicting preparation write', async () => {
    let detailReads = 0
    setup(conversationId, 'administrator')
    server.use(
      http.get(`${base}/${orderId}`, () => {
        detailReads += 1
        return HttpResponse.json(detailReads === 1 ? {
          ...detail,
          status: 'confirmed',
          preparation: {
            eligible: true, order_state: 'confirmed', payment_state: 'verified',
            allocation_state: 'full', required_quantity: 2, allocated_quantity: 2,
          },
        } : {
          ...detail,
          status: 'confirmed', updated_at: '2026-10-01T12:01:00Z',
          preparation: { ...detail.preparation, order_state: 'confirmed', payment_state: 'not_verified' },
        })
      }),
      http.get('/api/v1/auth/csrf', () => HttpResponse.json({ csrf_token: 'prepare-csrf', expires_at_epoch: 2_000_000_000 })),
      http.post(`${base}/${orderId}/prepare`, () => HttpResponse.json({
        error: { code: 'ORDER_PAYMENT_REQUIRED', message: 'Payment changed.' },
      }, { status: 409 })),
    )
    const user = userEvent.setup()
    renderApp(`/business/orders/${orderId}`)
    const region = await screen.findByRole('region', { name: 'Order detail' })
    await user.click(await within(region).findByRole('button', { name: 'Prepare Order' }))
    await user.click(within(region).getByRole('button', { name: 'Confirm preparation' }))
    expect(await within(region).findByRole('alert')).toHaveTextContent(/matching verified Payment evidence/)
    expect(within(region).getByText('Payment is not verified as completed')).toBeInTheDocument()
    expect(within(region).queryByRole('button', { name: 'Prepare Order' })).not.toBeInTheDocument()
  })

  it('retries an unconfirmed result with the same key and reports replay', async () => {
    const keys: (string | null)[] = []
    let writes = 0
    setup(conversationId, 'administrator')
    server.use(
      http.get(`${base}/${orderId}`, () => HttpResponse.json({
        ...detail,
        status: 'confirmed',
        preparation: {
          eligible: true, order_state: 'confirmed', payment_state: 'verified',
          allocation_state: 'full', required_quantity: 2, allocated_quantity: 2,
        },
      })),
      http.get('/api/v1/auth/csrf', () => HttpResponse.json({ csrf_token: 'prepare-csrf', expires_at_epoch: 2_000_000_000 })),
      http.post(`${base}/${orderId}/prepare`, ({ request }) => {
        keys.push(request.headers.get('Idempotency-Key'))
        writes += 1
        if (writes === 1) return HttpResponse.json({}, { status: 503 })
        return HttpResponse.json({
          order_id: orderId, status: 'preparing',
          updated_at: '2026-10-01T12:02:00Z', replayed: true,
        })
      }),
    )
    const user = userEvent.setup()
    renderApp(`/business/orders/${orderId}`)
    const region = await screen.findByRole('region', { name: 'Order detail' })
    await user.click(await within(region).findByRole('button', { name: 'Prepare Order' }))
    await user.click(within(region).getByRole('button', { name: 'Confirm preparation' }))
    expect(await within(region).findByRole('alert')).toHaveTextContent(/result could not be confirmed/)
    await user.click(within(region).getByRole('button', { name: 'Confirm preparation' }))
    expect(await within(region).findByText(/Preparation was already recorded/)).toBeInTheDocument()
    expect(keys).toHaveLength(2)
    expect(keys[0]).toBe(keys[1])
  })

  it('lets an authorized Administrator attest and dispatch allocated Stock', async () => {
    const submissions: { body: unknown; key: string | null; csrf: string | null }[] = []
    setup(conversationId, 'administrator')
    server.use(
      http.get(`${base}/${orderId}`, () => HttpResponse.json({
        ...detail,
        status: 'preparing',
        preparation: { ...detail.preparation, payment_state: 'verified' },
        dispatch: {
          eligible: true, order_state: 'preparing', payment_state: 'verified',
          allocation_state: 'full_unconsumed', inventory_state: 'ready',
          delivery_method_state: 'supported', required_quantity: 2,
        },
      })),
      http.get('/api/v1/auth/csrf', () => HttpResponse.json({ csrf_token: 'dispatch-csrf', expires_at_epoch: 2_000_000_000 })),
      http.post(`${base}/${orderId}/dispatch`, async ({ request }) => {
        submissions.push({
          body: await request.json(),
          key: request.headers.get('Idempotency-Key'),
          csrf: request.headers.get('X-CSRF-Token'),
        })
        return HttpResponse.json({
          order_id: orderId, status: 'delivering',
          updated_at: '2026-10-01T12:03:00Z', replayed: false,
        })
      }),
    )
    const user = userEvent.setup()
    renderApp(`/business/orders/${orderId}`)
    const region = await screen.findByRole('region', { name: 'Order detail' })
    expect(await within(region).findByText('Ready to dispatch')).toBeInTheDocument()
    expect(within(region).getByText('Full allocation ready · quantity 2')).toBeInTheDocument()
    await user.click(within(region).getByRole('button', { name: 'Dispatch Order' }))
    await user.type(within(region).getByLabelText('Internal handoff reference'), 'MBB-HANDOFF-1042')
    await user.click(within(region).getByLabelText(/full allocated quantity physically left/))
    await expectAccessible(document.body)
    await user.click(within(region).getByRole('button', { name: 'Confirm dispatch' }))
    expect(await within(region).findByText('Order dispatched. Customer delivery is not confirmed.')).toBeInTheDocument()
    expect(within(region).getByText('delivering')).toBeInTheDocument()
    expect(within(region).getByText('Allocation consumed at dispatch')).toBeInTheDocument()
    expect(submissions[0]).toMatchObject({
      body: {
        expected_updated_at: detail.updated_at,
        handoff_reference: 'MBB-HANDOFF-1042',
        attested: true,
      },
      csrf: 'dispatch-csrf',
    })
    expect(submissions[0]?.key).toMatch(/^[0-9a-f-]{36}$/)
  })

  it('lets an Administrator select customer evidence and attest reported receipt', async () => {
    const submissions: { body: unknown; key: string | null; csrf: string | null }[] = []
    setup(conversationId, 'administrator')
    server.use(
      http.get(`${base}/${orderId}`, () => HttpResponse.json({
        ...detail,
        status: 'delivering', updated_at: '2026-10-01T12:03:00Z',
        dispatch: { ...detail.dispatch, allocation_state: 'consumed' },
        delivery: {
          eligible: true, order_state: 'delivering', delivery_method_state: 'supported',
          line_state: 'single', dispatch_state: 'matched', association_state: 'verified',
          evidence_state: 'available', dispatch_occurred_at: '2026-10-01T12:03:00Z',
          evidence_messages: [{
            message_id: evidenceId,
            content_excerpt: 'Commande reçue, matondo mingi — nimepokea vizuri.',
            content_truncated: false,
            received_at: '2026-10-01T12:14:00Z',
            inbound_receipt_order: 8,
            provenance: 'inbound_whatsapp_customer_message',
          }],
          evidence_has_more: false,
        },
      })),
      http.get('/api/v1/auth/csrf', () => HttpResponse.json({ csrf_token: 'delivery-csrf', expires_at_epoch: 2_000_000_000 })),
      http.post(`${base}/${orderId}/confirm-delivery`, async ({ request }) => {
        submissions.push({
          body: await request.json(),
          key: request.headers.get('Idempotency-Key'),
          csrf: request.headers.get('X-CSRF-Token'),
        })
        return HttpResponse.json({
          order_id: orderId, status: 'delivered', delivered_at: '2026-10-01T12:14:00Z',
          updated_at: '2026-10-01T12:15:00Z', replayed: false,
        })
      }),
    )
    const user = userEvent.setup()
    renderApp(`/business/orders/${orderId}`)
    const region = await screen.findByRole('region', { name: 'Order detail' })
    expect(await within(region).findByText('Customer receipt evidence is ready for Administrator review')).toBeInTheDocument()
    expect(within(region).getByText(/not the exact physical handoff time/i)).toBeInTheDocument()
    expect(within(region).getByText(/Commande reçue, matondo mingi/)).toBeInTheDocument()
    await user.click(within(region).getByRole('button', { name: 'Confirm customer receipt' }))
    await user.click(within(region).getByLabelText('Inbound WhatsApp customer message'))
    await user.click(within(region).getByLabelText(/attest that the customer unambiguously reported receiving/))
    await expectAccessible(document.body)
    await user.click(within(region).getByRole('button', { name: 'Confirm delivery' }))
    expect(await within(region).findByText(/Customer-reported receipt recorded/)).toBeInTheDocument()
    expect(within(region).getByText('delivered')).toBeInTheDocument()
    expect(submissions[0]).toMatchObject({
      body: {
        expected_updated_at: '2026-10-01T12:03:00Z',
        evidence_message_id: evidenceId,
        attested: true,
      },
      csrf: 'delivery-csrf',
    })
    expect(submissions[0]?.key).toMatch(/^[0-9a-f-]{36}$/)
  })

  it('keeps delivery evidence read-only for Operators', async () => {
    setup(conversationId, 'operator')
    server.use(http.get(`${base}/${orderId}`, () => HttpResponse.json({
      ...detail,
      status: 'delivering',
      delivery: {
        eligible: true, order_state: 'delivering', delivery_method_state: 'supported',
        line_state: 'single', dispatch_state: 'matched', association_state: 'verified',
        evidence_state: 'available', dispatch_occurred_at: '2026-10-01T12:03:00Z',
        evidence_messages: [{
          message_id: evidenceId, content_excerpt: 'Nimepokea, merci.', content_truncated: false,
          received_at: '2026-10-01T12:14:00Z', inbound_receipt_order: 8,
          provenance: 'inbound_whatsapp_customer_message',
        }],
        evidence_has_more: false,
      },
    })))
    renderApp(`/business/orders/${orderId}`)
    const region = await screen.findByRole('region', { name: 'Order detail' })
    expect(await within(region).findByText('Nimepokea, merci.')).toBeInTheDocument()
    expect(within(region).queryByRole('button', { name: 'Confirm customer receipt' })).not.toBeInTheDocument()
    expect(within(region).queryByRole('radio')).not.toBeInTheDocument()
  })

  it('preserves delivery review through reauthentication and reuses a retry key', async () => {
    const keys: (string | null)[] = []
    let writes = 0
    setup(conversationId, 'administrator')
    const delivering = {
      ...detail,
      status: 'delivering', updated_at: '2026-10-01T12:03:00Z',
      delivery: {
        eligible: true, order_state: 'delivering', delivery_method_state: 'supported',
        line_state: 'single', dispatch_state: 'matched', association_state: 'verified',
        evidence_state: 'available', dispatch_occurred_at: '2026-10-01T12:03:00Z',
        evidence_messages: [{
          message_id: evidenceId, content_excerpt: 'Oui, reçu. Asante sana.',
          content_truncated: false, received_at: '2026-10-01T12:14:00Z',
          inbound_receipt_order: 8, provenance: 'inbound_whatsapp_customer_message',
        }],
        evidence_has_more: false,
      },
    }
    server.use(
      http.get('/api/v1/auth/session', () => HttpResponse.json({
        ...sessionFixture('administrator'), recent_reauthentication_expires_at_epoch: null,
      })),
      http.get(`${base}/${orderId}`, () => HttpResponse.json(delivering)),
      http.get('/api/v1/auth/csrf', () => HttpResponse.json({ csrf_token: 'pre-reauth-csrf', expires_at_epoch: 2_000_000_000 })),
      http.post('/api/v1/auth/reauthenticate', () => HttpResponse.json({
        ...sessionFixture('administrator'), recent_reauthentication_expires_at_epoch: 2_000_000_000,
        csrf_token: 'reauth-delivery-csrf',
      })),
      http.post(`${base}/${orderId}/confirm-delivery`, ({ request }) => {
        keys.push(request.headers.get('Idempotency-Key'))
        writes += 1
        if (writes === 1) return HttpResponse.json({}, { status: 503 })
        return HttpResponse.json({
          order_id: orderId, status: 'delivered', delivered_at: '2026-10-01T12:14:00Z',
          updated_at: '2026-10-01T12:15:00Z', replayed: true,
        })
      }),
    )
    const user = userEvent.setup()
    renderApp(`/business/orders/${orderId}`)
    const region = await screen.findByRole('region', { name: 'Order detail' })
    await user.click(await within(region).findByRole('button', { name: 'Confirm customer receipt' }))
    await user.click(within(region).getByLabelText('Inbound WhatsApp customer message'))
    await user.click(within(region).getByLabelText(/attest that the customer unambiguously reported receiving/))
    await user.click(within(region).getByRole('button', { name: 'Confirm delivery' }))
    await user.type(within(region).getByLabelText('Confirm Administrator password'), 'Administrator-Only-42!')
    await user.click(within(region).getByRole('button', { name: 'Confirm password' }))
    expect(await within(region).findByText(/Password confirmed/)).toBeInTheDocument()
    await user.click(within(region).getByRole('button', { name: 'Confirm delivery' }))
    expect(await within(region).findByRole('alert')).toHaveTextContent(/result could not be confirmed/)
    await user.click(within(region).getByRole('button', { name: 'Confirm delivery' }))
    expect(await within(region).findByText(/already recorded/)).toBeInTheDocument()
    expect(keys).toHaveLength(2)
    expect(keys[0]).toBe(keys[1])
  })

  it('refreshes delivery eligibility after evidence conflict', async () => {
    let detailReads = 0
    setup(conversationId, 'administrator')
    server.use(
      http.get(`${base}/${orderId}`, () => {
        detailReads += 1
        return HttpResponse.json({
          ...detail,
          status: 'delivering', updated_at: '2026-10-01T12:03:00Z',
          delivery: detailReads === 1 ? {
            eligible: true, order_state: 'delivering', delivery_method_state: 'supported',
            line_state: 'single', dispatch_state: 'matched', association_state: 'verified',
            evidence_state: 'available', dispatch_occurred_at: '2026-10-01T12:03:00Z',
            evidence_messages: [{
              message_id: evidenceId, content_excerpt: 'Reçu, merci.', content_truncated: false,
              received_at: '2026-10-01T12:14:00Z', inbound_receipt_order: 8,
              provenance: 'inbound_whatsapp_customer_message',
            }],
            evidence_has_more: false,
          } : {
            ...detail.delivery, order_state: 'delivering', dispatch_state: 'matched',
            association_state: 'verified', evidence_state: 'missing',
            dispatch_occurred_at: '2026-10-01T12:03:00Z',
          },
        })
      }),
      http.get('/api/v1/auth/csrf', () => HttpResponse.json({ csrf_token: 'delivery-csrf', expires_at_epoch: 2_000_000_000 })),
      http.post(`${base}/${orderId}/confirm-delivery`, () => HttpResponse.json({
        error: { code: 'ORDER_DELIVERY_EVIDENCE_REUSED', message: 'Evidence reused.' },
      }, { status: 409 })),
    )
    const user = userEvent.setup()
    renderApp(`/business/orders/${orderId}`)
    const region = await screen.findByRole('region', { name: 'Order detail' })
    await user.click(await within(region).findByRole('button', { name: 'Confirm customer receipt' }))
    await user.click(within(region).getByLabelText('Inbound WhatsApp customer message'))
    await user.click(within(region).getByLabelText(/attest that the customer unambiguously reported receiving/))
    await user.click(within(region).getByRole('button', { name: 'Confirm delivery' }))
    expect(await within(region).findByRole('alert')).toHaveTextContent(/already used/)
    expect(within(region).getByText('No eligible post-dispatch inbound customer text has been received')).toBeInTheDocument()
    expect(within(region).queryByRole('button', { name: 'Confirm customer receipt' })).not.toBeInTheDocument()
  })

  it('lets an Administrator cancel a reviewed pending Order with a structured reason', async () => {
    const submissions: { body: unknown; key: string | null; csrf: string | null }[] = []
    setup(conversationId, 'administrator')
    server.use(
      http.get('/api/v1/auth/csrf', () => HttpResponse.json({ csrf_token: 'cancel-csrf', expires_at_epoch: 2_000_000_000 })),
      http.post(`${base}/${orderId}/cancel`, async ({ request }) => {
        submissions.push({
          body: await request.json(),
          key: request.headers.get('Idempotency-Key'),
          csrf: request.headers.get('X-CSRF-Token'),
        })
        return HttpResponse.json({
          order_id: orderId, status: 'cancelled',
          updated_at: '2026-10-01T12:01:00Z', replayed: false,
        })
      }),
    )
    const user = userEvent.setup()
    renderApp(`/business/orders/${orderId}`)
    const region = await screen.findByRole('region', { name: 'Order detail' })
    await within(region).findByText('pending')
    await user.click(within(region).getByRole('button', { name: 'Cancel Order' }))
    await user.selectOptions(within(region).getByLabelText('Cancellation reason'), 'customer_requested')
    await user.click(within(region).getByRole('button', { name: 'Confirm cancellation' }))
    expect(await within(region).findByText('Order cancelled.')).toBeInTheDocument()
    expect(within(region).getByText('cancelled')).toBeInTheDocument()
    expect(submissions[0]).toMatchObject({
      body: { reason_code: 'customer_requested', expected_updated_at: detail.updated_at },
      csrf: 'cancel-csrf',
    })
    expect(submissions[0]?.key).toMatch(/^[0-9a-f-]{36}$/)
    expect(within(region).queryByRole('button', { name: 'Cancel Order' })).not.toBeInTheDocument()
  })

  it('omits an unverified Inbox link and filters status in the queue', async () => {
    setup(null)
    const user = userEvent.setup()
    renderApp(`/business/orders/${orderId}`)
    const region = await screen.findByRole('region', { name: 'Order detail' })
    expect(await within(region).findByText('Kinshasa')).toBeInTheDocument()
    expect(within(region).queryByRole('link', { name: 'Open associated Inbox conversation' })).not.toBeInTheDocument()
    await user.selectOptions(screen.getByLabelText('Order status'), 'delivered')
    expect(await screen.findByText('No Orders found.')).toBeInTheDocument()
  })
})
