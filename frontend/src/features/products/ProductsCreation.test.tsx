import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import type { ProductVariant } from '../../api/businessProducts'
import { expectAccessible } from '../../test/accessibility'
import { sessionFixture } from '../../test/fixtures'
import { renderApp } from '../../test/renderApp'
import { server } from '../../test/server'

const productId = '11111111-1111-4111-8111-111111111111'
const variantId = '40000000-0000-4000-8000-000000000006'
const browse = '/api/v1/business/products'
const products = '/api/v1/operator/commerce/products'
const product = {
  product_id: productId, name: 'Fictional Air Fryer', category_code: 'air_fryer',
  description: 'A family cooking appliance.', active: false, primary_media: null,
}
const variant: ProductVariant = {
  sellable_item_id: variantId, product_id: productId, model_label: null,
  sku: null, attributes: {}, active: false,
}

function setup(role: 'administrator' | 'operator' = 'administrator', recent = true) {
  let variants: ProductVariant[] = []
  server.use(
    http.get('/api/v1/auth/session', () => HttpResponse.json({ ...sessionFixture(role),
      recent_reauthentication_expires_at_epoch: recent ? 2_000_000_000 : null })),
    http.get('/api/v1/auth/csrf', () => HttpResponse.json({ csrf_token: 'commerce-csrf', expires_at_epoch: 2_000_000_000 })),
    http.get(browse, () => HttpResponse.json({ items: [product], has_more: false })),
    http.get(`${browse}/:id`, () => HttpResponse.json({ ...product, variants, has_more_variants: false })),
    http.get(`/api/v1/operator/product-offers/${variantId}`, () => HttpResponse.json({
      ...product, ...variant, product_name: product.name, price_id: null, current_usd_price: null,
      price_currency: 'USD', price_effective_at: null, cdf_quote_status: 'cdf_quote_unavailable',
      cdf_quote_unavailable_reason: 'current_usd_price_unavailable', derived_cdf_quote: null,
      inventory_status: 'unknown', inventory_configured: false, inventory_updated_at: null,
      offer_status: 'inactive', is_sellable_now: false, reason_code: 'product_inactive',
      read_at: '2026-09-28T12:00:00Z',
    })),
  )
  return { setVariants: (next: ProductVariant[]) => { variants = next } }
}

async function fillProduct(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole('button', { name: 'Add product' }))
  await user.type(screen.getByRole('textbox', { name: 'Product name' }), product.name)
  await user.type(screen.getByRole('textbox', { name: 'Category code' }), product.category_code)
  await user.type(screen.getByRole('textbox', { name: 'Description' }), product.description)
}

describe('Administrator Product creation', () => {
  it('creates Product and Standard variant inactive, then selects it in existing detail', async () => {
    const state = setup()
    let productWrites = 0
    let variantWrites = 0
    server.use(
      http.post(products, async ({ request }) => {
        productWrites++
        expect(request.headers.get('X-CSRF-Token')).toBe('commerce-csrf')
        expect(await request.json()).toEqual({ name: product.name, category_code: product.category_code,
          description: product.description, active: false })
        return HttpResponse.json(product, { status: 201 })
      }),
      http.post(`${products}/:id/sellable-items`, async ({ request }) => {
        variantWrites++
        expect(request.headers.get('X-CSRF-Token')).toBe('commerce-csrf')
        expect(await request.json()).toEqual({ model_label: null, sku: null, attributes: {}, active: false })
        state.setVariants([variant])
        return HttpResponse.json(variant, { status: 201 })
      }),
    )
    const { container } = renderApp('/business/products')
    const user = userEvent.setup()
    await fillProduct(user)
    await expectAccessible(container)
    await user.click(screen.getByRole('button', { name: 'Save product' }))
    expect(await screen.findByRole('heading', { name: 'Add first variant' })).toBeInTheDocument()
    expect(productWrites).toBe(1)
    expect(screen.getByText(/saved inactive/)).toBeInTheDocument()
    await expectAccessible(container)
    await user.click(screen.getByRole('button', { name: 'Save first variant' }))
    expect(await screen.findByRole('heading', { name: product.name })).toBeInTheDocument()
    expect(await screen.findByRole('button', { name: 'Standard variant' })).toHaveAttribute('aria-pressed', 'true')
    expect(await screen.findByText('Price not set')).toBeInTheDocument()
    expect(variantWrites).toBe(1)
    expect(screen.getByText('Inactive product')).toBeInTheDocument()
  })

  it('keeps creation unavailable to Operator', async () => {
    setup('operator')
    renderApp('/business/products')
    const user = userEvent.setup()
    await screen.findByRole('heading', { name: 'Products' })
    expect(screen.queryByRole('button', { name: 'Add product' })).not.toBeInTheDocument()
    await user.click(await screen.findByRole('link', { name: product.name }))
    await screen.findByRole('heading', { name: product.name })
    expect(screen.queryByRole('heading', { name: 'Add first variant' })).not.toBeInTheDocument()
  })

  it('resumes first variant from inactive empty Product after a failed variant save', async () => {
    const state = setup()
    let productWrites = 0
    let variantWrites = 0
    server.use(
      http.post(products, () => { productWrites++; return HttpResponse.json(product, { status: 201 }) }),
      http.post(`${products}/:id/sellable-items`, () => {
        variantWrites++
        if (variantWrites === 1) return HttpResponse.json({ error: { code: 'COMMERCE_VALIDATION_FAILED' } }, { status: 422 })
        state.setVariants([variant])
        return HttpResponse.json(variant, { status: 201 })
      }),
    )
    renderApp('/business/products')
    const user = userEvent.setup()
    await fillProduct(user)
    await user.click(screen.getByRole('button', { name: 'Save product' }))
    await screen.findByRole('heading', { name: 'Add first variant' })
    await user.type(screen.getByRole('textbox', { name: 'Variant name (optional)' }), '6L')
    await user.click(screen.getByRole('button', { name: 'Save first variant' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Check the information')
    expect(screen.getByRole('textbox', { name: 'Variant name (optional)' })).toHaveValue('6L')
    await user.click(screen.getByRole('link', { name: 'View Product detail' }))
    expect(await screen.findByText('No variants have been added.')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Add first variant' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Save first variant' }))
    await screen.findByRole('button', { name: 'Standard variant' })
    expect(productWrites).toBe(1)
    expect(variantWrites).toBe(2)
  })

  it('keeps SKU after a duplicate conflict and permits correction', async () => {
    setup()
    let writes = 0
    server.use(http.post(`${products}/:id/sellable-items`, async ({ request }) => {
      writes++
      const body = await request.json() as { sku: string }
      if (writes === 1) {
        expect(body.sku).toBe('FRY-6L')
        return HttpResponse.json({ error: { code: 'COMMERCE_CONFLICT' } }, { status: 409 })
      }
      expect(body.sku).toBe('FRY-8L')
      return HttpResponse.json({ ...variant, sku: body.sku }, { status: 201 })
    }))
    renderApp(`/business/products/${productId}`)
    const user = userEvent.setup()
    await screen.findByRole('heading', { name: 'Add first variant' })
    const sku = screen.getByRole('textbox', { name: 'SKU (optional)' })
    await user.type(sku, 'fry-6l')
    await user.click(screen.getByRole('button', { name: 'Save first variant' }))
    expect(await screen.findByText(/SKU is already in use/)).toBeInTheDocument()
    expect(sku).toHaveValue('fry-6l')
    await user.clear(sku); await user.type(sku, 'fry-8l')
    await user.click(screen.getByRole('button', { name: 'Save first variant' }))
    await waitFor(() => expect(writes).toBe(2))
  })

  it('does not repeat an ambiguous Product create and keeps entered values for review', async () => {
    setup()
    let writes = 0
    server.use(http.post(products, () => {
      writes++
      return HttpResponse.json({ error: { code: 'SERVICE_UNAVAILABLE' } }, { status: 503 })
    }))
    renderApp('/business/products')
    const user = userEvent.setup()
    await fillProduct(user)
    await user.click(screen.getByRole('button', { name: 'Save product' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('could not be confirmed')
    expect(screen.getByRole('button', { name: 'Save product' })).toBeDisabled()
    expect(screen.getByRole('textbox', { name: 'Product name' })).toHaveValue(product.name)
    await user.click(screen.getByRole('button', { name: 'Refresh Products list' }))
    await screen.findByRole('link', { name: product.name })
    expect(writes).toBe(1)
  })

  it('treats a create success without a usable Product ID as uncertain', async () => {
    setup()
    let writes = 0
    server.use(http.post(products, () => { writes++; return HttpResponse.json({}, { status: 201 }) }))
    renderApp('/business/products')
    const user = userEvent.setup()
    await fillProduct(user)
    await user.click(screen.getByRole('button', { name: 'Save product' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('could not be confirmed')
    expect(screen.getByRole('button', { name: 'Save product' })).toBeDisabled()
    expect(writes).toBe(1)
  })

  it('can retry after CSRF retrieval fails before any Product create is sent', async () => {
    setup()
    let csrfReads = 0
    let writes = 0
    server.use(
      http.get('/api/v1/auth/csrf', () => {
        csrfReads++
        return csrfReads === 1
          ? HttpResponse.json({ error: { code: 'SERVICE_UNAVAILABLE' } }, { status: 503 })
          : HttpResponse.json({ csrf_token: 'commerce-csrf', expires_at_epoch: 2_000_000_000 })
      }),
      http.post(products, () => { writes++; return HttpResponse.json(product, { status: 201 }) }),
    )
    renderApp('/business/products')
    const user = userEvent.setup()
    await fillProduct(user)
    await user.click(screen.getByRole('button', { name: 'Save product' }))
    expect(await screen.findByRole('alert')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Save product' })).toBeEnabled()
    expect(writes).toBe(0)
    await user.click(screen.getByRole('button', { name: 'Save product' }))
    await screen.findByRole('heading', { name: 'Add first variant' })
    expect(writes).toBe(1)
  })

  it('does not repeat an ambiguous variant create and points to Product detail', async () => {
    setup()
    let writes = 0
    server.use(http.post(`${products}/:id/sellable-items`, () => {
      writes++
      return HttpResponse.json({ error: { code: 'SERVICE_UNAVAILABLE' } }, { status: 503 })
    }))
    renderApp(`/business/products/${productId}`)
    const user = userEvent.setup()
    await screen.findByRole('heading', { name: 'Add first variant' })
    await user.type(screen.getByRole('textbox', { name: 'Variant name (optional)' }), '6L')
    await user.click(screen.getByRole('button', { name: 'Save first variant' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('could not be confirmed')
    expect(screen.getByRole('button', { name: 'Save first variant' })).toBeDisabled()
    expect(screen.getByRole('textbox', { name: 'Variant name (optional)' })).toHaveValue('6L')
    expect(screen.getByRole('button', { name: 'Refresh Product detail' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Refresh Product detail' }))
    expect(await screen.findByText(/No variant is currently listed/)).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Variant name (optional)' })).toHaveValue('6L')
    expect(writes).toBe(1)
  })

  it('reveals a variant already saved after an ambiguous create without repeating it', async () => {
    const state = setup()
    let writes = 0
    server.use(http.post(`${products}/:id/sellable-items`, () => {
      writes++
      state.setVariants([variant])
      return HttpResponse.json({ error: { code: 'SERVICE_UNAVAILABLE' } }, { status: 503 })
    }))
    renderApp(`/business/products/${productId}`)
    const user = userEvent.setup()
    await screen.findByRole('heading', { name: 'Add first variant' })
    await user.click(screen.getByRole('button', { name: 'Save first variant' }))
    await screen.findByText(/Variant creation could not be confirmed/)
    await user.click(screen.getByRole('button', { name: 'Refresh Product detail' }))
    expect(await screen.findByRole('link', { name: 'Standard variant' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Save first variant' })).toBeDisabled()
    expect(writes).toBe(1)
  })

  it('preserves first-variant values while confirming Administrator password', async () => {
    const state = setup('administrator', false)
    let writes = 0
    server.use(
      http.post('/api/v1/auth/reauthenticate', () => HttpResponse.json({ ...sessionFixture('administrator'),
        recent_reauthentication_expires_at_epoch: 2_000_000_000, csrf_token: 'rotated-csrf' })),
      http.post(`${products}/:id/sellable-items`, async ({ request }) => {
        writes++
        expect(request.headers.get('X-CSRF-Token')).toBe('rotated-csrf')
        expect(await request.json()).toEqual({ model_label: '6L', sku: 'FRY-6L', attributes: {}, active: false })
        state.setVariants([{ ...variant, model_label: '6L', sku: 'FRY-6L' }])
        return HttpResponse.json({ ...variant, model_label: '6L', sku: 'FRY-6L' }, { status: 201 })
      }),
    )
    renderApp(`/business/products/${productId}`)
    const user = userEvent.setup()
    await screen.findByRole('heading', { name: 'Add first variant' })
    await user.type(screen.getByRole('textbox', { name: 'Variant name (optional)' }), '6L')
    await user.type(screen.getByRole('textbox', { name: 'SKU (optional)' }), 'fry-6l')
    await user.click(screen.getByRole('button', { name: 'Save first variant' }))
    await user.type(await screen.findByLabelText('Confirm Administrator password'), 'Fictional-Password-42!')
    await user.click(screen.getByRole('button', { name: 'Confirm password' }))
    await screen.findByText('Password confirmed. Review and save the variant.')
    expect(screen.getByRole('textbox', { name: 'Variant name (optional)' })).toHaveValue('6L')
    expect(screen.getByRole('textbox', { name: 'SKU (optional)' })).toHaveValue('fry-6l')
    expect(writes).toBe(0)
    await user.click(screen.getByRole('button', { name: 'Save first variant' }))
    expect(await screen.findByRole('button', { name: '6L · FRY-6L' })).toHaveAttribute('aria-pressed', 'true')
    expect(writes).toBe(1)
  })

  it('keeps Product values through validation and password confirmation', async () => {
    setup('administrator', false)
    let writes = 0
    server.use(
      http.post('/api/v1/auth/reauthenticate', () => HttpResponse.json({ ...sessionFixture('administrator'),
        recent_reauthentication_expires_at_epoch: 2_000_000_000, csrf_token: 'rotated-csrf' })),
      http.post(products, async ({ request }) => {
        writes++
        expect(request.headers.get('X-CSRF-Token')).toBe('rotated-csrf')
        return HttpResponse.json({ error: { code: 'COMMERCE_VALIDATION_FAILED' } }, { status: 422 })
      }),
    )
    const { container } = renderApp('/business/products')
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Add product' }))
    await user.click(screen.getByRole('button', { name: 'Save product' }))
    expect(screen.getByText(/Enter a product name/)).toBeInTheDocument()
    expect(writes).toBe(0)
    await user.type(screen.getByRole('textbox', { name: 'Product name' }), product.name)
    await user.type(screen.getByRole('textbox', { name: 'Category code' }), product.category_code)
    await user.type(screen.getByRole('textbox', { name: 'Description' }), product.description)
    await user.click(screen.getByRole('button', { name: 'Save product' }))
    await user.type(await screen.findByLabelText('Confirm Administrator password'), 'Fictional-Password-42!')
    await expectAccessible(container)
    await user.click(screen.getByRole('button', { name: 'Confirm password' }))
    await screen.findByText('Password confirmed. Review and save the product.')
    expect(screen.getByRole('textbox', { name: 'Product name' })).toHaveValue(product.name)
    expect(writes).toBe(0)
    await user.click(screen.getByRole('button', { name: 'Save product' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Check the information')
    expect(screen.getByRole('textbox', { name: 'Description' })).toHaveValue(product.description)
    expect(writes).toBe(1)
  })
})
