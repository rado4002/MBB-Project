import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import type { BusinessProductDetail, ProductCommercialReview } from '../../api/businessProducts'
import type { ProductOffer } from '../../api/productOffers'
import { sessionFixture } from '../../test/fixtures'
import { renderApp } from '../../test/renderApp'
import { server } from '../../test/server'

const productId = '11111111-1111-4111-8111-111111111111'
const firstId = '40000000-0000-4000-8000-000000000006'
const siblingId = '40000000-0000-4000-8000-000000000008'
const base = '/api/v1/business/products'
const commerceProducts = '/api/v1/operator/commerce/products'
const commerceItems = '/api/v1/operator/commerce/sellable-items'
const offerPath = `/api/v1/operator/product-offers/${firstId}`

function product(active: boolean, firstActive = true): BusinessProductDetail {
  return {
    product_id: productId, name: 'Fictional Air Fryer', category_code: 'air_fryer',
    description: 'Fictional appliance.', active, primary_media: null,
    variants: [
      { sellable_item_id: firstId, product_id: productId, model_label: '6L', sku: 'FRY-6L', attributes: {}, active: firstActive },
      { sellable_item_id: siblingId, product_id: productId, model_label: '8L', sku: 'FRY-8L', attributes: {}, active: true },
    ],
    has_more_variants: false,
  }
}

function offer(productActive: boolean, itemActive: boolean): ProductOffer {
  return {
    product_id: productId, sellable_item_id: firstId, product_name: 'Fictional Air Fryer',
    category_code: 'air_fryer', description: 'Fictional appliance.', model_label: '6L', sku: 'FRY-6L',
    attributes: {}, primary_media: null, price_id: productId, current_usd_price: '55.00',
    price_currency: 'USD', price_effective_at: null, cdf_quote_status: 'cdf_quote_unavailable',
    cdf_quote_unavailable_reason: 'current_fx_unavailable', derived_cdf_quote: null,
    inventory_status: 'available', inventory_configured: true, inventory_updated_at: null,
    offer_status: !productActive || !itemActive ? 'inactive' : 'sellable_now',
    is_sellable_now: productActive && itemActive,
    reason_code: !productActive ? 'product_inactive' : !itemActive ? 'sellable_item_inactive' : 'sellable_now',
    read_at: '2026-09-29T12:00:00Z',
  }
}

function review(): ProductCommercialReview {
  return {
    product_id: productId, read_at: '2026-09-29T12:00:00Z', has_more: false,
    items: [
      { sellable_item_id: firstId, model_label: '6L', sku: 'FRY-6L', active: true,
        current_usd_price: null, inventory_status: 'unknown', offer_status: 'inactive' },
      { sellable_item_id: siblingId, model_label: '8L', sku: 'FRY-8L', active: true,
        current_usd_price: '70.00', inventory_status: 'out_of_stock', offer_status: 'inactive' },
    ],
  }
}

function setup(role: 'administrator' | 'operator', state: { productActive: boolean; firstActive: boolean }, recent = true) {
  server.use(
    http.get('/api/v1/auth/session', () => HttpResponse.json({ ...sessionFixture(role),
      recent_reauthentication_expires_at_epoch: recent ? 2_000_000_000 : null })),
    http.get('/api/v1/auth/csrf', () => HttpResponse.json({ csrf_token: 'commerce-csrf', expires_at_epoch: 2_000_000_000 })),
    http.get(`${base}/:id`, () => HttpResponse.json(product(state.productActive, state.firstActive))),
    http.get(offerPath, () => HttpResponse.json(offer(state.productActive, state.firstActive))),
  )
}

describe('Product lifecycle controls', () => {
  it('reviews all variant facts in one request, confirms Product activation, and rereads', async () => {
    const state = { productActive: false, firstActive: true }
    setup('administrator', state)
    let reviewReads = 0; let offerReads = 0; let writes = 0; let productReads = 0
    server.use(
      http.get(`${base}/:id`, () => { productReads++; return HttpResponse.json(product(state.productActive, state.firstActive)) }),
      http.get(`${commerceProducts}/:id/commercial-review`, () => { reviewReads++; return HttpResponse.json(review()) }),
      http.get(offerPath, () => { offerReads++; return HttpResponse.json(offer(state.productActive, state.firstActive)) }),
      http.patch(`${commerceProducts}/:id`, async ({ request }) => {
        writes++
        expect(request.headers.get('X-CSRF-Token')).toBe('commerce-csrf')
        expect(await request.json()).toEqual({ active: true })
        state.productActive = true
        return HttpResponse.json({ active: false })
      }),
    )
    renderApp(`/business/products/${productId}`)
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Activate product' }))
    const reviewPanel = screen.getByRole('region', { name: 'product status control' })
    const first = within(reviewPanel).getByText(/6L · FRY-6L/).closest('li')!
    const sibling = within(reviewPanel).getByText(/8L · FRY-8L/).closest('li')!
    expect(within(first).getByText('Price not set')).toBeInTheDocument()
    expect(within(first).getByText('Availability unconfirmed')).toBeInTheDocument()
    expect(within(first).getByText('Inactive')).toBeInTheDocument()
    expect(within(sibling).getByText('USD 70.00')).toBeInTheDocument()
    expect(within(sibling).getByText('Out of stock')).toBeInTheDocument()
    expect(reviewReads).toBe(1); expect(offerReads).toBe(0); expect(writes).toBe(0)
    await user.click(within(reviewPanel).getByRole('button', { name: 'Confirm activate product' }))
    expect(await screen.findByText('Active product')).toBeInTheDocument()
    expect(productReads).toBeGreaterThanOrEqual(2)
    expect(writes).toBe(1)
  })

  it('confirms Product deactivation and preserves variants after reread', async () => {
    const state = { productActive: true, firstActive: true }
    setup('administrator', state)
    let writes = 0
    server.use(http.patch(`${commerceProducts}/:id`, async ({ request }) => {
      writes++; expect(await request.json()).toEqual({ active: false })
      state.productActive = false
      return HttpResponse.json({ active: false })
    }))
    renderApp(`/business/products/${productId}`)
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Deactivate product' }))
    expect(screen.getByText(/stops the Product and all its variants/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Confirm deactivate product' }))
    expect(await screen.findByText('Inactive product')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '6L · FRY-6L' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '8L · FRY-8L' })).toBeInTheDocument()
    expect(writes).toBe(1)
  })

  it('loads additional Product facts by page before enabling confirmation', async () => {
    const state = { productActive: false, firstActive: true }
    setup('administrator', state)
    const calls: string[] = []
    server.use(http.get(`${commerceProducts}/:id/commercial-review`, ({ request }) => {
      const offset = new URL(request.url).searchParams.get('offset') ?? ''
      calls.push(offset)
      const full = review()
      return HttpResponse.json({ ...full, items: offset === '0' ? full.items.slice(0, 1) : full.items.slice(1),
        has_more: offset === '0' })
    }))
    renderApp(`/business/products/${productId}`)
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Activate product' }))
    expect(screen.getByRole('button', { name: 'Confirm activate product' })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: 'Load more variant facts' }))
    await screen.findByText(/8L · FRY-8L/)
    expect(screen.getByRole('button', { name: 'Confirm activate product' })).toBeEnabled()
    expect(calls).toEqual(['0', '1'])
  })

  it.each([
    { start: false, action: 'Activate', finish: true },
    { start: true, action: 'Deactivate', finish: false },
  ])('$action affects only the selected variant and rereads its Offer', async ({ start, action, finish }) => {
    const state = { productActive: true, firstActive: start }
    setup('administrator', state)
    let writes = 0; let offerReads = 0
    server.use(
      http.get(offerPath, () => { offerReads++; return HttpResponse.json(offer(state.productActive, state.firstActive)) }),
      http.patch(`${commerceItems}/:id`, async ({ request }) => {
        writes++
        expect(request.headers.get('X-CSRF-Token')).toBe('commerce-csrf')
        expect(await request.json()).toEqual({ active: finish })
        state.firstActive = finish
        return HttpResponse.json({ active: start })
      }),
    )
    renderApp(`/business/products/${productId}`)
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: '6L · FRY-6L' }))
    await user.click(await screen.findByRole('button', { name: `${action} variant` }))
    expect(screen.getByText(/Current facts: USD 55.00 · Available/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: `Confirm ${action.toLowerCase()} variant` }))
    await waitFor(() => expect(screen.getByRole('button', { name: '6L · FRY-6L' }).parentElement).toHaveTextContent(finish ? 'Active variant' : 'Inactive variant'))
    expect(screen.getByRole('button', { name: '8L · FRY-8L' }).parentElement).toHaveTextContent('Active variant')
    await waitFor(() => expect(offerReads).toBeGreaterThanOrEqual(2))
    expect(within(screen.getByRole('region', { name: 'Current commercial facts' })).getByText(finish ? 'Sellable now' : 'Inactive')).toBeInTheDocument()
    expect(writes).toBe(1)
  })

  it('preserves the review through reauthentication and requires a separate final confirmation', async () => {
    const state = { productActive: false, firstActive: true }
    setup('administrator', state, false)
    let writes = 0
    server.use(
      http.get(`${commerceProducts}/:id/commercial-review`, () => HttpResponse.json(review())),
      http.post('/api/v1/auth/reauthenticate', () => HttpResponse.json({ ...sessionFixture('administrator'),
        recent_reauthentication_expires_at_epoch: 2_000_000_000, csrf_token: 'rotated-csrf' })),
      http.patch(`${commerceProducts}/:id`, () => { writes++; state.productActive = true; return HttpResponse.json({ active: true }) }),
    )
    renderApp(`/business/products/${productId}`)
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Activate product' }))
    await user.click(screen.getByRole('button', { name: 'Confirm activate product' }))
    await user.type(await screen.findByLabelText('Confirm Administrator password'), 'Fictional-Password-42!')
    await user.click(screen.getByRole('button', { name: 'Confirm password' }))
    expect(await screen.findByRole('button', { name: 'Confirm activate product' })).toBeInTheDocument()
    expect(within(screen.getByRole('region', { name: 'product status control' })).getByText('Price not set')).toBeInTheDocument()
    expect(writes).toBe(0)
  })

  it('does not offer lifecycle controls to an Operator', async () => {
    const state = { productActive: true, firstActive: true }
    setup('operator', state)
    renderApp(`/business/products/${productId}`)
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: '6L · FRY-6L' }))
    expect(await screen.findByText('USD 55.00')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Activate|Deactivate/i })).not.toBeInTheDocument()
  })
})
