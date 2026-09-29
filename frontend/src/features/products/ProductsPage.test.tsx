import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { delay, http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import type { BusinessProductDetail } from '../../api/businessProducts'
import type { ProductOffer } from '../../api/productOffers'
import { expectAccessible } from '../../test/accessibility'
import { sessionFixture } from '../../test/fixtures'
import { renderApp } from '../../test/renderApp'
import { server } from '../../test/server'

const productId = '11111111-1111-4111-8111-111111111111'
const itemId = '40000000-0000-4000-8000-000000000006'
const base = '/api/v1/business/products'
const offerPath = `/api/v1/operator/product-offers/${itemId}`
const commerce = `/api/v1/operator/commerce/sellable-items/${itemId}`
const product: BusinessProductDetail = {
  product_id: productId, name: 'Fictional Air Fryer', category_code: 'air_fryer',
  description: 'A family cooking appliance.', active: true, primary_media: null,
  variants: [{ sellable_item_id: itemId, product_id: productId, model_label: '6L', sku: 'FRY-6L', attributes: { capacity_l: 6 }, active: true }],
  has_more_variants: false,
}
function offer(overrides: Partial<ProductOffer> = {}): ProductOffer {
  return {
    product_id: productId, sellable_item_id: itemId, sku: 'FRY-6L', product_name: product.name,
    category_code: product.category_code, description: product.description, model_label: '6L',
    attributes: { capacity_l: 6, washable: true }, primary_media: null,
    price_id: productId, current_usd_price: '55.00', price_currency: 'USD', price_effective_at: '2026-09-01T12:00:00Z',
    cdf_quote_status: 'available', cdf_quote_unavailable_reason: null,
    derived_cdf_quote: { currency: 'CDF', cdf_amount: '154000.00', exchange_rate_id: productId,
      usd_to_cdf_rate: '2800.000000', exchange_rate_effective_at: '2026-09-01T12:00:00Z' },
    inventory_status: 'available', inventory_configured: true, inventory_updated_at: '2026-09-01T12:00:00Z',
    offer_status: 'sellable_now', is_sellable_now: true, reason_code: 'sellable_now', read_at: '2026-09-26T12:00:00Z',
    ...overrides,
  }
}
function setup(role: 'operator' | 'administrator' | 'analyst' = 'operator', value = offer(), recent = true) {
  server.use(
    http.get('/api/v1/auth/session', () => HttpResponse.json({ ...sessionFixture(role),
      recent_reauthentication_expires_at_epoch: recent ? 2_000_000_000 : null })),
    http.get('/api/v1/auth/csrf', () => HttpResponse.json({ csrf_token: 'commerce-csrf', expires_at_epoch: 2_000_000_000 })),
    http.get(base, () => HttpResponse.json({ items: [product], has_more: false })),
    http.get(`${base}/:id`, () => HttpResponse.json(product)),
    http.get(offerPath, () => HttpResponse.json(value)),
  )
}
async function openVariant() {
  renderApp(`/business/products/${productId}`)
  const user = userEvent.setup()
  await user.click(await screen.findByRole('button', { name: '6L · FRY-6L' }))
  await screen.findByRole('heading', { name: '6L' })
  return user
}

describe('shared Business Products', () => {
  it('navigates from Inbox through the Product list and exact variant, read-only for Operator', async () => {
    setup()
    const user = userEvent.setup()
    const { container } = renderApp('/inbox')
    await user.click(await screen.findByRole('link', { name: 'Business' }))
    await screen.findByRole('heading', { name: 'Products' })
    const list = screen.getByRole('list')
    expect(within(list).queryByText(/USD|Available|Inactive/)).not.toBeInTheDocument()
    await user.click(await screen.findByRole('link', { name: product.name }))
    await user.click(await screen.findByRole('button', { name: '6L · FRY-6L' }))
    expect(await screen.findByText('USD 55.00')).toBeInTheDocument()
    expect(screen.getByText('CDF 154000.00')).toBeInTheDocument()
    expect(screen.getByText('capacity l')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '6L' })).toHaveFocus()
    expect(screen.queryByRole('button', { name: /Change price|Update stock quantity|Create|Edit|Activate/ })).not.toBeInTheDocument()
    await expectAccessible(container)
    await user.click(screen.getByRole('link', { name: 'Back to products' }))
    await screen.findByRole('heading', { name: 'Products' })
  })

  it('does not expose Business to a role without product read authority', async () => {
    setup('analyst')
    renderApp('/business/products')
    await screen.findByRole('heading', { name: 'Inbox' })
    expect(screen.queryByRole('link', { name: 'Business' })).not.toBeInTheDocument()
  })

  it('discloses long secondary content without hiding current commercial facts', async () => {
    const description = 'Long fictional product description. '.repeat(20)
    setup('operator', offer({ attributes: { capacity_l: 6, washable: true, color: 'Blue', material: 'Steel', power: 'Electric' } }))
    server.use(http.get(`${base}/:id`, () => HttpResponse.json({ ...product, description })))
    const user = await openVariant()
    const facts = screen.getByRole('region', { name: 'Current commercial facts' })
    expect(within(facts).getByText('USD 55.00')).toBeVisible()
    expect(within(facts).getByText('CDF 154000.00')).toBeVisible()
    expect(within(facts).getByText('Available')).toBeVisible()
    expect(within(facts).getByText('Sellable now')).toBeVisible()
    expect(screen.getByText(description.trim())).not.toBeVisible()
    expect(screen.getByText('Steel')).not.toBeVisible()
    await user.click(screen.getByText('Show product description'))
    expect(screen.getByText(description.trim())).toBeVisible()
    await user.click(screen.getByText('Show characteristics (5)'))
    expect(screen.getByText('Steel')).toBeVisible()
    expect(within(facts).queryByRole('button', { name: /Change price|Update stock quantity/ })).not.toBeInTheDocument()
  })

  it('selects variants by keyboard and keeps availability distinct from commercial status', async () => {
    setup('operator', offer({ current_usd_price: null, derived_cdf_quote: null,
      cdf_quote_status: 'cdf_quote_unavailable', offer_status: 'price_unavailable', reason_code: 'price_unavailable' }))
    renderApp(`/business/products/${productId}`)
    const user = userEvent.setup()
    const choice = await screen.findByRole('button', { name: '6L · FRY-6L' })
    expect(choice).toHaveAttribute('aria-pressed', 'false')
    choice.focus()
    await user.keyboard('{Enter}')
    expect(await screen.findByRole('heading', { name: '6L' })).toHaveFocus()
    expect(choice).toHaveAttribute('aria-pressed', 'true')
    const facts = screen.getByRole('region', { name: 'Current commercial facts' })
    expect(within(within(facts).getByText('Availability').parentElement!).getByText('Available')).toBeVisible()
    expect(within(within(facts).getByText('Commercial status').parentElement!).getByText('Price not set')).toBeVisible()
  })

  it.each([
    { label: 'missing price', value: offer({ current_usd_price: null, derived_cdf_quote: null, cdf_quote_status: 'cdf_quote_unavailable', offer_status: 'price_unavailable', reason_code: 'price_unavailable', is_sellable_now: false }), text: 'Price not set' },
    { label: 'unknown', value: offer({ inventory_status: 'unknown', offer_status: 'availability_unconfirmed', reason_code: 'availability_unconfirmed', is_sellable_now: false }), text: 'Availability unconfirmed' },
    { label: 'unavailable', value: offer({ inventory_status: 'out_of_stock', offer_status: 'out_of_stock', reason_code: 'inventory_out_of_stock', is_sellable_now: false }), text: 'Out of stock' },
    { label: 'missing FX', value: offer({ derived_cdf_quote: null, cdf_quote_status: 'cdf_quote_unavailable', cdf_quote_unavailable_reason: 'current_fx_unavailable' }), text: 'CDF quote unavailable' },
  ])('shows $label without inventing facts', async ({ value, text }) => {
    setup('operator', value)
    await openVariant()
    expect(screen.getAllByText(text).length).toBeGreaterThan(0)
    if (value.current_usd_price === null) expect(screen.queryByText(/USD 0/)).not.toBeInTheDocument()
    if (!value.derived_cdf_quote) expect(screen.queryByText('CDF 154000.00')).not.toBeInTheDocument()
    expect(screen.queryByText(/quantity|restock/i)).not.toBeInTheDocument()
  })

  it('uses the effective variant image and falls back on failure', async () => {
    setup('operator', offer({ primary_media: { media_id: itemId, asset_url: 'https://example.invalid/variant.png', alt_text: 'Variant image', source_scope: 'sellable_item' } }))
    await openVariant()
    const section = screen.getByRole('region', { name: 'Selected variant' })
    fireEvent.error(within(section).getByRole('img', { name: 'Variant image' }))
    expect(within(section).getByText('No image')).toBeInTheDocument()
  })

  it('supports explicit search, bounded/empty results and API retry', async () => {
    setup()
    let fail = true
    server.use(http.get(base, ({ request }) => {
      if (fail) return HttpResponse.json({ error: { code: 'SERVICE_UNAVAILABLE' } }, { status: 503 })
      const query = new URL(request.url).searchParams.get('query')
      return HttpResponse.json({ items: query ? [] : [product], has_more: !query })
    }))
    const user = userEvent.setup()
    renderApp('/business/products')
    await screen.findByRole('alert')
    fail = false
    await user.click(screen.getByRole('button', { name: 'Retry' }))
    await screen.findByText(/Showing the first 50/)
    await user.type(screen.getByRole('textbox', { name: 'Product name or category' }), 'Missing')
    expect(screen.getByRole('link', { name: product.name })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Search' }))
    await screen.findByText('No matching products.')
  })

  it('lets Administrator inspect inactive and empty setup', async () => {
    setup('administrator')
    server.use(http.get(`${base}/:id`, () => HttpResponse.json({ ...product, active: false, variants: [] })))
    renderApp(`/business/products/${productId}`)
    expect(await screen.findByText('Inactive product')).toBeInTheDocument()
    expect(screen.getByText('No variants have been added.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Change price' })).not.toBeInTheDocument()
  })

  it('changes price as a decimal string with CSRF and rereads the Product Offer', async () => {
    setup('administrator')
    let current = offer()
    let reads = 0
    server.use(
      http.get(offerPath, () => { reads++; return HttpResponse.json(current) }),
      http.put(`${commerce}/price`, async ({ request }) => {
        expect(request.headers.get('X-CSRF-Token')).toBe('commerce-csrf')
        expect(await request.json()).toEqual({ amount: '60.25', currency: 'USD' })
        current = offer({ current_usd_price: '60.25' })
        return HttpResponse.json({ amount: '999.00' }) // UI must use the authoritative reread.
      }),
    )
    const user = await openVariant()
    expect(screen.getByRole('button', { name: 'Change price' }).closest('div')).toHaveTextContent('Current priceUSD 55.00')
    await user.click(screen.getByRole('button', { name: 'Change price' }))
    await user.clear(screen.getByRole('textbox', { name: 'New USD price' }))
    await user.type(screen.getByRole('textbox', { name: 'New USD price' }), '60.25')
    await user.click(screen.getByRole('button', { name: 'Save price' }))
    expect(await screen.findByText('USD 60.25')).toBeInTheDocument()
    expect(screen.queryByText('USD 999.00')).not.toBeInTheDocument()
    expect(reads).toBe(2)
    await screen.findByText('Change saved. Showing refreshed current facts.')
  })

  it('keeps a saved change separate from a failed authoritative reread', async () => {
    setup('administrator')
    let saved = false
    let allowRead = false
    let writes = 0
    server.use(
      http.get(offerPath, () => saved && !allowRead
        ? HttpResponse.json({ error: { code: 'SERVICE_UNAVAILABLE' } }, { status: 503 })
        : HttpResponse.json(offer({ current_usd_price: saved ? '60.25' : '55.00' }))),
      http.put(`${commerce}/price`, () => { saved = true; writes++; return HttpResponse.json({}) }),
    )
    const user = await openVariant()
    await user.click(screen.getByRole('button', { name: 'Change price' }))
    const input = screen.getByRole('textbox', { name: 'New USD price' })
    expect(input).toHaveFocus()
    await user.clear(input); await user.type(input, '60.25')
    await user.click(screen.getByRole('button', { name: 'Save price' }))
    await screen.findByText('Change saved, but current facts could not be loaded. Refresh before another change.')
    expect(screen.queryByText('USD 55.00')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Change price' })).not.toBeInTheDocument()
    allowRead = true
    await user.click(screen.getByRole('button', { name: 'Refresh current facts' }))
    await screen.findByText('USD 60.25')
    expect(writes).toBe(1)
  })

  it('keeps price editing on Product detail and links exact stock work to Stock', async () => {
    setup('administrator')
    const user = await openVariant()
    const stock = screen.getByRole('link', { name: 'View stock' })
    expect(stock).toHaveAttribute('href', `/business/stock?item=${itemId}`)
    expect(screen.queryByRole('button', { name: 'Update stock quantity' })).not.toBeInTheDocument()
    const trigger = screen.getByRole('button', { name: 'Change price' })
    await user.click(trigger)
    expect(screen.getByRole('textbox', { name: 'New USD price' })).toHaveFocus()
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(trigger).toHaveFocus())
    expect(screen.queryByRole('textbox', { name: 'New USD price' })).not.toBeInTheDocument()
  })

  it('rejects invalid decimals locally and preserves input through backend validation errors', async () => {
    setup('administrator')
    let writes = 0
    server.use(http.put(`${commerce}/price`, () => { writes++; return HttpResponse.json({ error: { code: 'COMMERCE_VALIDATION_FAILED' } }, { status: 422 }) }))
    const user = await openVariant()
    await user.click(screen.getByRole('button', { name: 'Change price' }))
    const input = screen.getByRole('textbox', { name: 'New USD price' })
    await user.clear(input); await user.type(input, '0.00')
    await user.click(screen.getByRole('button', { name: 'Save price' }))
    expect(await screen.findByText(/Enter a positive USD/)).toBeInTheDocument()
    expect(writes).toBe(0)
    await user.clear(input); await user.type(input, '25.10')
    await user.click(screen.getByRole('button', { name: 'Save price' }))
    await screen.findByText('Check the information entered and try again.')
    expect(input).toHaveValue('25.10'); expect(writes).toBe(1)
  })

  it('requires password confirmation without losing the proposed price or automatically writing', async () => {
    setup('administrator', offer(), false)
    let writes = 0
    server.use(
      http.post('/api/v1/auth/reauthenticate', async ({ request }) => {
        expect(await request.json()).toEqual({ password: 'Fictional-Password-42!' })
        return HttpResponse.json({ ...sessionFixture('administrator'), recent_reauthentication_expires_at_epoch: 2_000_000_000, csrf_token: 'rotated-csrf' })
      }),
      http.put(`${commerce}/price`, () => { writes++; return HttpResponse.json({}) }),
    )
    const user = await openVariant()
    await user.click(screen.getByRole('button', { name: 'Change price' }))
    const input = screen.getByRole('textbox', { name: 'New USD price' })
    await user.clear(input); await user.type(input, '60.25')
    await user.click(screen.getByRole('button', { name: 'Save price' }))
    await user.type(await screen.findByLabelText('Confirm Administrator password'), 'Fictional-Password-42!')
    await user.click(screen.getByRole('button', { name: 'Confirm password' }))
    await screen.findByText('Password confirmed. Review and save your change.')
    expect(input).toHaveValue('60.25'); expect(writes).toBe(0)
  })

  it('removes maintenance actions when the server denies permission', async () => {
    setup('administrator')
    server.use(http.put(`${commerce}/price`, () => HttpResponse.json({ error: { code: 'capability_required' } }, { status: 403 })))
    const user = await openVariant()
    await user.click(screen.getByRole('button', { name: 'Change price' }))
    await user.click(await screen.findByRole('button', { name: 'Save price' }))
    await screen.findByText('You do not have permission to maintain products.')
    expect(screen.queryByRole('button', { name: 'Change price' })).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'View stock' })).toBeInTheDocument()
  })

  it('requires authoritative refresh after an ambiguous write failure', async () => {
    setup('administrator')
    let writes = 0
    server.use(http.put(`${commerce}/price`, () => { writes++; return HttpResponse.json({ error: { code: 'SERVICE_UNAVAILABLE' } }, { status: 503 }) }))
    const user = await openVariant()
    await user.click(screen.getByRole('button', { name: 'Change price' }))
    await user.click(screen.getByRole('button', { name: 'Save price' }))
    await screen.findByText(/The change could not be confirmed/)
    expect(screen.getByRole('button', { name: 'Save price' })).toBeDisabled()
    expect(screen.queryByText('USD 55.00')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Refresh current facts' }))
    await screen.findByText('USD 55.00')
    expect(screen.getByRole('button', { name: 'Save price' })).toBeEnabled()
    expect(writes).toBe(1)
  })

  it('does not show late variant detail after leaving the Product', async () => {
    setup()
    server.use(http.get(offerPath, async () => { await delay(120); return HttpResponse.json(offer()) }))
    const user = userEvent.setup()
    renderApp(`/business/products/${productId}`)
    await user.click(await screen.findByRole('button', { name: '6L · FRY-6L' }))
    await user.click(screen.getByRole('link', { name: 'Back to products' }))
    await screen.findByRole('heading', { name: 'Products' })
    await waitFor(() => expect(screen.queryByRole('region', { name: 'Selected variant' })).not.toBeInTheDocument())
    expect(screen.queryByText('USD 55.00')).not.toBeInTheDocument()
  })
})
