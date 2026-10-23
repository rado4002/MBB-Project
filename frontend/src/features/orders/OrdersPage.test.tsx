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
const base = '/api/v1/business/orders'
const summary = {
  order_id: orderId, status: 'pending', created_at: '2026-10-01T12:00:00Z',
  updated_at: '2026-10-01T12:00:00Z',
  total_cdf: '309400.00', currency: 'CDF', item_count: 1,
  first_product_name: 'Fictional Air Fryer', conversation_id: conversationId,
}
const detail = {
  ...summary, delivery_zone: 'Kinshasa', items: [{
    product_id: productId, sellable_item_id: itemId, product_name: 'Fictional Air Fryer',
    model_label: '6L', quantity: 2, price_id: '55555555-5555-4555-8555-555555555555',
    unit_price_usd: '55.25', exchange_rate_id: '66666666-6666-4666-8666-666666666666',
    usd_to_cdf_rate: '2800.000000', unit_price_cdf: '154700.00',
  }],
  preparation: {
    eligible: false, order_state: 'not_confirmed', payment_state: 'missing',
    allocation_state: 'missing', required_quantity: 2, allocated_quantity: null,
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
    expect(within(region).getByText('No Payment recorded')).toBeInTheDocument()
    expect(within(region).getByText('No durable Stock allocation recorded')).toBeInTheDocument()
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
