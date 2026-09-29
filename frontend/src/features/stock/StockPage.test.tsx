import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import type { StockMovement } from '../../api/businessStock'
import { expectAccessible } from '../../test/accessibility'
import { sessionFixture } from '../../test/fixtures'
import { renderApp } from '../../test/renderApp'
import { server } from '../../test/server'

const productId = '11111111-1111-4111-8111-111111111111'
const itemId = '40000000-0000-4000-8000-000000000006'
const base = '/api/v1/business/stock'
const inventory = `/api/v1/operator/commerce/sellable-items/${itemId}/inventory`
const stockItem = {
  product_id: productId, sellable_item_id: itemId, product_name: 'Fictional Air Fryer',
  model_label: '6L', sku: 'FRY-6L', product_active: true, variant_active: true,
  availability: 'available', quantity: 10, inventory_updated_at: '2026-09-28T12:00:00Z',
}
const movement: StockMovement = {
  movement_id: '22222222-2222-4222-8222-222222222222', sellable_item_id: itemId,
  operation_kind: 'receive', before_quantity: 10, before_updated_at: stockItem.inventory_updated_at,
  received_amount: 5, corrected_quantity: null, after_quantity: 15, reason: null,
  actor_account_id: 'account-test-only', operation_key: 'key-test', occurred_at: '2026-09-29T12:00:00Z',
}
function setup(role: 'administrator' | 'operator' = 'administrator', quantity: number | null = 10) {
  let current = quantity
  let updatedAt: string | null = quantity === null ? null : stockItem.inventory_updated_at
  let movements = quantity === null ? [] : [movement]
  server.use(
    http.get('/api/v1/auth/session', () => HttpResponse.json({ ...sessionFixture(role), recent_reauthentication_expires_at_epoch: 2_000_000_000 })),
    http.get('/api/v1/auth/csrf', () => HttpResponse.json({ csrf_token: 'stock-csrf', expires_at_epoch: 2_000_000_000 })),
    http.get(base, ({ request }) => {
      const url = new URL(request.url)
      const query = url.searchParams.get('query')?.toLowerCase()
      const found = !query || ['fictional air fryer', '6l', 'fry-6l'].some((text) => text.includes(query))
      const exact = !url.searchParams.get('item_id') || url.searchParams.get('item_id') === itemId
      const item = { ...stockItem, quantity: current, inventory_updated_at: updatedAt,
        availability: current === null ? 'unknown' : current === 0 ? 'out_of_stock' : 'available' }
      if (role === 'operator') { delete (item as Partial<typeof item>).quantity; delete (item as Partial<typeof item>).inventory_updated_at }
      return HttpResponse.json({ items: found && exact ? [item] : [], has_more: false })
    }),
    http.get(`${inventory}/activity`, () => HttpResponse.json({ items: movements })),
    http.post(`${inventory}/receive`, async ({ request }) => {
      const body = await request.json() as { received_amount: number }
      const before = current
      current = (current ?? 0) + body.received_amount
      updatedAt = '2026-09-29T12:00:00Z'
      movements = [{ ...movement, before_quantity: before, received_amount: body.received_amount, after_quantity: current }]
      return HttpResponse.json(movements[0])
    }),
    http.post(`${inventory}/adjust`, async ({ request }) => {
      const body = await request.json() as { corrected_quantity: number; reason: string }
      const before = current
      current = body.corrected_quantity
      updatedAt = '2026-09-29T12:00:00Z'
      movements = [{ ...movement, operation_kind: 'adjust', before_quantity: before, received_amount: null,
        corrected_quantity: current, after_quantity: current, reason: body.reason }]
      return HttpResponse.json(movements[0])
    }),
  )
}

describe('Business Stock', () => {
  it('navigates Products and Stock, searches by product, variant, or SKU, and links to Product', async () => {
    setup()
    const user = userEvent.setup()
    const { container } = renderApp('/business/products')
    await user.click(await screen.findByRole('link', { name: 'Stock' }))
    expect(await screen.findByRole('heading', { name: 'Stock' })).toBeInTheDocument()
    for (const query of ['Fictional Air Fryer', '6L', 'FRY-6L']) {
      await user.clear(screen.getByRole('textbox', { name: 'Product, variant, or SKU' }))
      await user.type(screen.getByRole('textbox', { name: 'Product, variant, or SKU' }), query)
      await user.click(screen.getByRole('button', { name: 'Search' }))
      expect(await screen.findByRole('link', { name: 'Fictional Air Fryer · 6L' })).toBeInTheDocument()
    }
    await user.click(screen.getByRole('link', { name: 'Fictional Air Fryer · 6L' }))
    expect(await screen.findByRole('heading', { name: 'Recent stock activity' })).toBeInTheDocument()
    expect(screen.getByText('10 → 15')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'View product' })).toHaveAttribute('href', `/business/products/${productId}?variant=${itemId}`)
    await expectAccessible(container)
    await user.click(screen.getByRole('link', { name: 'Products' }))
    expect(await screen.findByRole('heading', { name: 'Products' })).toBeInTheDocument()
  })

  it('confirms Receive with reviewed before and after, then rereads stock and activity', async () => {
    setup()
    let submitted: unknown
    server.use(http.post(`${inventory}/receive`, async ({ request }) => {
      submitted = await request.json()
      return HttpResponse.json(movement)
    }))
    const user = userEvent.setup()
    renderApp(`/business/stock?item=${itemId}`)
    await user.click(await screen.findByRole('button', { name: 'Receive stock' }))
    await user.type(screen.getByRole('textbox', { name: 'Amount received' }), '5')
    await user.click(screen.getByRole('button', { name: 'Review change' }))
    expect(screen.getByText('10 → +5 → 15')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Confirm stock change' }))
    await waitFor(() => expect(submitted).toMatchObject({ received_amount: 5, expected_quantity: 10,
      expected_updated_at: stockItem.inventory_updated_at, operation_key: expect.any(String) }))
    expect(await screen.findByText(/Stock change saved/)).toBeInTheDocument()
  })

  it('adjusts unknown stock to a verified count with a required reason', async () => {
    setup('administrator', null)
    let submitted: unknown
    server.use(http.post(`${inventory}/adjust`, async ({ request }) => {
      submitted = await request.json()
      return HttpResponse.json({ ...movement, operation_kind: 'adjust', before_quantity: null,
        corrected_quantity: 7, after_quantity: 7, reason: 'Counted shelves' })
    }))
    const user = userEvent.setup()
    renderApp(`/business/stock?item=${itemId}`)
    expect(await screen.findByRole('button', { name: 'Receive stock' })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: 'Adjust stock' }))
    await user.type(screen.getByRole('textbox', { name: 'Corrected quantity' }), '7')
    await user.type(screen.getByRole('textbox', { name: 'Reason for correction' }), 'Counted shelves')
    await user.click(screen.getByRole('button', { name: 'Review change' }))
    expect(screen.getByText('Unknown → 7')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Confirm stock change' }))
    await waitFor(() => expect(submitted).toMatchObject({ corrected_quantity: 7, reason: 'Counted shelves', expected_quantity: null }))
  })

  it('rereads authoritative stock after a stale-state conflict', async () => {
    setup()
    server.use(http.post(`${inventory}/receive`, () => HttpResponse.json({ error: { code: 'STOCK_CONFLICT', message: 'stale' } }, { status: 409 })))
    const user = userEvent.setup()
    renderApp(`/business/stock?item=${itemId}`)
    await user.click(await screen.findByRole('button', { name: 'Receive stock' }))
    await user.type(screen.getByRole('textbox', { name: 'Amount received' }), '5')
    await user.click(screen.getByRole('button', { name: 'Review change' }))
    await user.click(screen.getByRole('button', { name: 'Confirm stock change' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Stock changed since your review')
    expect(screen.queryByRole('heading', { name: 'Confirm receipt' })).not.toBeInTheDocument()
  })

  it('retries an uncertain receipt with the same durable operation key', async () => {
    setup()
    const keys: string[] = []
    server.use(http.post(`${inventory}/receive`, async ({ request }) => {
      const body = await request.json() as { operation_key: string }
      keys.push(body.operation_key)
      if (keys.length === 1) return HttpResponse.json({ error: { code: 'SERVICE_UNAVAILABLE', message: 'try again' } }, { status: 503 })
      return HttpResponse.json(movement)
    }))
    const user = userEvent.setup()
    renderApp(`/business/stock?item=${itemId}`)
    await user.click(await screen.findByRole('button', { name: 'Receive stock' }))
    await user.type(screen.getByRole('textbox', { name: 'Amount received' }), '5')
    await user.click(screen.getByRole('button', { name: 'Review change' }))
    await user.click(screen.getByRole('button', { name: 'Confirm stock change' }))
    expect(await screen.findByRole('button', { name: 'Retry same operation' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Retry same operation' }))
    await waitFor(() => expect(keys).toHaveLength(2))
    expect(keys[0]).toBe(keys[1])
  })

  it('keeps the reviewed adjustment through recent reauthentication', async () => {
    setup()
    server.use(
      http.get('/api/v1/auth/session', () => HttpResponse.json(sessionFixture('administrator'))),
      http.post('/api/v1/auth/reauthenticate', () => HttpResponse.json({ ...sessionFixture('administrator'),
        recent_reauthentication_expires_at_epoch: 2_000_000_000, csrf_token: 'rotated-csrf' })),
    )
    const user = userEvent.setup()
    renderApp(`/business/stock?item=${itemId}`)
    await user.click(await screen.findByRole('button', { name: 'Adjust stock' }))
    await user.type(screen.getByRole('textbox', { name: 'Corrected quantity' }), '8')
    await user.type(screen.getByRole('textbox', { name: 'Reason for correction' }), 'Verified count')
    await user.click(screen.getByRole('button', { name: 'Review change' }))
    await user.click(screen.getByRole('button', { name: 'Confirm stock change' }))
    await user.type(await screen.findByLabelText('Confirm Administrator password'), 'Administrator-Access-42!')
    await user.click(screen.getByRole('button', { name: 'Confirm password' }))
    expect(await screen.findByText('10 → 8')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Confirm adjustment' })).toBeInTheDocument()
  })

  it('keeps Operator read-only with derived availability and no quantity or movement history', async () => {
    setup('operator')
    const user = userEvent.setup()
    const { container } = renderApp('/business/stock')
    const result = await screen.findByRole('link', { name: 'Fictional Air Fryer · 6L' })
    expect(within(result.closest('li')!).getByText('Available')).toBeInTheDocument()
    expect(screen.queryByText(/Quantity:/)).not.toBeInTheDocument()
    await user.click(result)
    expect(await screen.findByText('Availability')).toBeInTheDocument()
    expect(screen.queryByText('Current quantity')).not.toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Recent stock activity' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Receive stock|Adjust stock/ })).not.toBeInTheDocument()
    await expectAccessible(container)
  })
})
