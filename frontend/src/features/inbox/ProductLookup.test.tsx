import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it, vi } from 'vitest'
import * as productOffers from '../../api/productOffers'
import type { ProductOffer } from '../../api/productOffers'
import { expectAccessible } from '../../test/accessibility'
import { conversationDetailFixture, conversationFixture, sessionFixture } from '../../test/fixtures'
import { renderApp } from '../../test/renderApp'
import { server } from '../../test/server'

const first = '11111111-1111-4111-8111-111111111111'
const second = '22222222-2222-4222-8222-222222222222'
const itemId = '40000000-0000-4000-8000-000000000006'
const path = '/api/v1/operator/product-offers'

function offer(overrides: Partial<ProductOffer> = {}): ProductOffer {
  return {
    product_id: first, sellable_item_id: itemId, sku: 'FRY-6L', product_name: 'Air Fryer',
    category_code: 'air_fryer', description: 'Family cooking appliance.', model_label: '6L',
    attributes: { capacity_l: 6, washable: true }, primary_media: null,
    price_id: second, current_usd_price: '55.00', price_currency: 'USD', price_effective_at: '2026-09-01T12:00:00Z',
    cdf_quote_status: 'available', cdf_quote_unavailable_reason: null,
    derived_cdf_quote: { currency: 'CDF', cdf_amount: '154000.00', exchange_rate_id: second,
      usd_to_cdf_rate: '2800.000000', exchange_rate_effective_at: '2026-09-01T12:00:00Z' },
    inventory_status: 'available', inventory_configured: true, inventory_updated_at: '2026-09-01T12:00:00Z',
    offer_status: 'sellable_now', is_sellable_now: true, reason_code: 'sellable_now', read_at: '2026-09-26T12:00:00Z',
    ...overrides,
  }
}

function workspace(role: 'operator' | 'administrator' = 'operator', owned = true) {
  server.use(
    http.get('/api/v1/auth/session', () => HttpResponse.json(sessionFixture(role))),
    http.get('/api/v1/operator/conversations', () => HttpResponse.json({
      items: [conversationFixture(first), conversationFixture(second)], next_cursor: null,
    })),
    http.get('/api/v1/operator/conversations/:conversationId', ({ params }) => {
      const detail = conversationDetailFixture(String(params.conversationId))
      if (owned) detail.ownership = {
        owner_type: 'human', human_owner: { account_id: 'account-test-only', display_name: 'Omar Operator' },
        ai_execution_state: 'paused', version: 2, updated_at: '2026-09-26T12:00:00Z',
      }
      return HttpResponse.json(detail)
    }),
    http.get('/api/v1/operator/conversations/:conversationId/timeline', () => HttpResponse.json({ items: [], next_older_cursor: null })),
    http.get(path, () => HttpResponse.json({ items: [offer()] })),
    http.get(`${path}/:id`, () => HttpResponse.json(offer())),
  )
  renderApp(`/inbox/${first}`)
}

async function openSearch() {
  const user = userEvent.setup()
  await user.click(await screen.findByRole('button', { name: 'Find a product' }))
  const dialog = screen.getByRole('dialog', { name: 'Find a product' })
  const search = within(dialog).getByRole('textbox', { name: 'Product name, model or SKU' })
  await user.type(search, 'Air Fryer')
  await user.click(within(dialog).getByRole('button', { name: 'Search' }))
  return { user, dialog }
}

describe('Inbox authoritative product lookup', () => {
  it('searches only on submit, inspects fresh detail and returns with the unsent reply intact', async () => {
    workspace()
    let searches = 0
    let details = 0
    server.use(
      http.get(path, ({ request }) => {
        searches++
        expect(new URL(request.url).searchParams.get('query')).toBe('Air Fryer')
        expect(new URL(request.url).searchParams.get('limit')).toBe('20')
        return HttpResponse.json({ items: [offer()] })
      }),
      http.get(`${path}/:id`, () => { details++; return HttpResponse.json(offer()) }),
    )
    const user = userEvent.setup()
    const reply = await screen.findByRole('textbox', { name: 'Reply to Customer' })
    await user.type(reply, 'Bonjour, voici le modèle demandé.')
    const trigger = screen.getByRole('button', { name: 'Find a product' })
    await user.click(trigger)
    const dialog = screen.getByRole('dialog')
    const query = within(dialog).getByRole('textbox')
    expect(query).toHaveFocus()
    await user.type(query, 'Air Fryer')
    expect(searches).toBe(0)
    await user.click(within(dialog).getByRole('button', { name: 'Search' }))
    await user.click(await within(dialog).findByRole('button', { name: 'Air Fryer — 6L' }))
    expect(await within(dialog).findByText('CDF 154000.00')).toBeInTheDocument()
    expect(within(dialog).getByText('Family cooking appliance.')).toBeInTheDocument()
    expect(within(dialog).getByText('capacity l')).toBeInTheDocument()
    expect(within(dialog).getByRole('heading', { name: 'Air Fryer — 6L' })).toHaveFocus()
    expect(within(dialog).queryByRole('button', { name: /Change price|Set availability|Activate|Edit/ })).not.toBeInTheDocument()
    await expectAccessible(dialog)
    await user.click(within(dialog).getByRole('button', { name: 'Refresh' }))
    await within(dialog).findByText('CDF 154000.00')
    expect(details).toBe(2)
    await user.click(within(dialog).getByRole('button', { name: 'Back to results' }))
    expect(within(dialog).getByRole('button', { name: 'Air Fryer — 6L' })).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: 'Back to conversation' }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(reply).toHaveValue('Bonjour, voici le modèle demandé.')
    await waitFor(() => expect(trigger).toHaveFocus())
  })

  it.each([
    { label: 'unavailable', value: offer({ inventory_status: 'out_of_stock', offer_status: 'out_of_stock', reason_code: 'inventory_out_of_stock', is_sellable_now: false }), text: 'Unavailable' },
    { label: 'unknown', value: offer({ inventory_status: 'unknown', inventory_configured: false, offer_status: 'availability_unconfirmed', reason_code: 'availability_unconfirmed', is_sellable_now: false }), text: 'Availability unconfirmed' },
    { label: 'missing price', value: offer({ current_usd_price: null, price_id: null, offer_status: 'price_unavailable', reason_code: 'price_unavailable', is_sellable_now: false, derived_cdf_quote: null, cdf_quote_status: 'cdf_quote_unavailable', cdf_quote_unavailable_reason: 'current_usd_price_unavailable' }), text: 'Price not set' },
    { label: 'missing FX', value: offer({ derived_cdf_quote: null, cdf_quote_status: 'cdf_quote_unavailable', cdf_quote_unavailable_reason: 'current_fx_unavailable' }), text: 'CDF quote unavailable' },
  ])('represents $label without inventing commercial facts', async ({ value, text }) => {
    workspace()
    server.use(http.get(path, () => HttpResponse.json({ items: [value] })), http.get(`${path}/:id`, () => HttpResponse.json(value)))
    const { user, dialog } = await openSearch()
    await user.click(await within(dialog).findByRole('button', { name: 'Air Fryer — 6L' }))
    expect((await within(dialog).findAllByText(text)).length).toBeGreaterThan(0)
    if (!value.derived_cdf_quote) expect(within(dialog).queryByText('CDF 154000.00')).not.toBeInTheDocument()
    if (value.current_usd_price === null) expect(within(dialog).queryByText('USD 55.00')).not.toBeInTheDocument()
    expect(within(dialog).queryByText(/quantity|units in stock/i)).not.toBeInTheDocument()
  })

  it('falls back after an image fails and shows a placeholder when absent', async () => {
    workspace()
    const pictured = offer({ primary_media: { media_id: itemId, asset_url: 'https://example.invalid/fryer.jpg', alt_text: 'Six litre fryer', source_scope: 'sellable_item' } })
    server.use(http.get(path, () => HttpResponse.json({ items: [pictured] })))
    const { user, dialog } = await openSearch()
    fireEvent.error(await within(dialog).findByRole('img', { name: 'Six litre fryer' }))
    expect(within(dialog).getByText('No image')).toBeInTheDocument()
    expect(within(dialog).getByText('USD 55.00')).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: 'Air Fryer — 6L' }))
    await within(dialog).findByText('Family cooking appliance.')
    expect(within(dialog).getByText('No image')).toBeInTheDocument()
  })

  it('retries API failure and hides old detail during a failed refresh', async () => {
    workspace()
    let fail = true
    server.use(http.get(path, () => fail ? HttpResponse.json({ error: { code: 'SERVICE_UNAVAILABLE' } }, { status: 503 }) : HttpResponse.json({ items: [offer()] })))
    const { user, dialog } = await openSearch()
    expect(await within(dialog).findByRole('alert')).toBeInTheDocument()
    fail = false
    await user.click(within(dialog).getByRole('button', { name: 'Retry' }))
    await user.click(await within(dialog).findByRole('button', { name: 'Air Fryer — 6L' }))
    await within(dialog).findByText('CDF 154000.00')
    server.use(http.get(`${path}/:id`, () => HttpResponse.json({ error: { code: 'SERVICE_UNAVAILABLE' } }, { status: 503 })))
    await user.click(within(dialog).getByRole('button', { name: 'Refresh' }))
    await within(dialog).findByRole('alert')
    expect(within(dialog).queryByText('USD 55.00')).not.toBeInTheDocument()
    expect(within(dialog).queryByText('CDF 154000.00')).not.toBeInTheDocument()
    server.use(http.get(`${path}/:id`, () => HttpResponse.json(offer({ current_usd_price: '56.00' }))))
    await user.click(within(dialog).getByRole('button', { name: 'Retry' }))
    expect(await within(dialog).findByText('USD 56.00')).toBeInTheDocument()
  })

  it('shows permission failure and empty results honestly', async () => {
    workspace('administrator')
    server.use(http.get(path, () => HttpResponse.json({ error: { code: 'FORBIDDEN' } }, { status: 403 })))
    const { user, dialog } = await openSearch()
    expect(await within(dialog).findByRole('alert')).toHaveTextContent(/permitted|permission/i)
    server.use(http.get(path, () => HttpResponse.json({ items: [] })))
    await user.click(within(dialog).getByRole('button', { name: 'Retry' }))
    expect(await within(dialog).findByText(/No matching active variants/)).toBeInTheDocument()
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it.each(['search', 'detail'] as const)('discards late $0 responses even if transport ignores abort on conversation switch', async (kind) => {
    let resolveSearch!: (response: { items: ProductOffer[] }) => void
    let resolveDetail!: (response: ProductOffer) => void
    const search = vi.fn(() => kind === 'search' ? new Promise<{ items: ProductOffer[] }>((resolve) => { resolveSearch = resolve }) : Promise.resolve({ items: [offer()] }))
    const detail = vi.fn(() => new Promise<ProductOffer>((resolve) => { resolveDetail = resolve }))
    const spy = vi.spyOn(productOffers, 'createProductOfferClient').mockReturnValue({ search, detail })
    try {
      workspace()
      const { user, dialog } = await openSearch()
      if (kind === 'detail') await user.click(await within(dialog).findByRole('button', { name: 'Air Fryer — 6L' }))
      const signal = kind === 'search' ? search.mock.calls[0] : detail.mock.calls[0]
      expect(signal).toBeDefined()
      act(() => {
        window.history.pushState({}, '', `/inbox/${second}`)
        fireEvent(window, new PopStateEvent('popstate'))
      })
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
      await act(async () => {
        if (kind === 'search') resolveSearch({ items: [offer({ product_name: 'Late old product' })] })
        else resolveDetail(offer({ product_name: 'Late old product' }))
      })
      await user.click(await screen.findByRole('button', { name: 'Find a product' }))
      expect(within(screen.getByRole('dialog')).getByRole('textbox')).toHaveValue('')
      expect(screen.queryByText(/Late old product/)).not.toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Air Fryer — 6L' })).not.toBeInTheDocument()
    } finally { spy.mockRestore() }
  })

  it('does not acquire ownership or enable replies when AI owns the conversation', async () => {
    workspace('operator', false)
    let writes = 0
    server.use(http.post('/api/v1/operator/conversations/:id/:action', () => {
      writes++
      return new HttpResponse(null, { status: 500 })
    }))
    const { user, dialog } = await openSearch()
    await user.click(await within(dialog).findByRole('button', { name: 'Air Fryer — 6L' }))
    await within(dialog).findByText('Family cooking appliance.')
    await user.click(within(dialog).getByRole('button', { name: 'Back to conversation' }))
    expect(screen.getByRole('button', { name: 'Take over conversation' })).toBeInTheDocument()
    expect(screen.getByRole('radio', { name: 'Reply' })).toBeDisabled()
    expect(writes).toBe(0)
  })
})
