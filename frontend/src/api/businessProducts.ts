import { requestJson } from './client'
import type { ProductOffer } from './productOffers'

export interface BusinessProduct {
  product_id: string
  name: string
  category_code: string
  description: string
  active: boolean
  primary_media: ProductOffer['primary_media']
}
export interface ProductVariant {
  sellable_item_id: string
  product_id: string
  model_label: string | null
  sku: string | null
  attributes: ProductOffer['attributes']
  active: boolean
}
export interface BusinessProductDetail extends BusinessProduct {
  variants: ProductVariant[]
  has_more_variants: boolean
}

export interface ProductCommercialReview {
  product_id: string
  items: Array<{
    sellable_item_id: string
    model_label: string | null
    sku: string | null
    active: boolean
    current_usd_price: string | null
    inventory_status: ProductOffer['inventory_status']
    offer_status: ProductOffer['offer_status']
  }>
  has_more: boolean
  read_at: string
}

export interface ProductCreateInput {
  name: string
  category_code: string
  description: string
  active: false
}

export interface FirstVariantCreateInput {
  model_label: string | null
  sku: string | null
  attributes: Record<string, never>
  active: false
}

export interface CatalogMedia {
  media_id: string
  product_id: string | null
  sellable_item_id: string | null
  asset_url: string
  alt_text: string | null
  is_primary: boolean
  active: boolean
}

export function createBusinessProductsClient(onSessionExpired: () => void) {
  const base = '/api/v1/business/products'
  const products = '/api/v1/operator/commerce/products'
  const commerce = '/api/v1/operator/commerce/sellable-items'
  const media = '/api/v1/operator/commerce/product-media'
  return {
    list: (query: string, signal: AbortSignal) =>
      requestJson<{ items: BusinessProduct[]; has_more: boolean }>(
        `${base}?${new URLSearchParams({ query, limit: '50' })}`, { signal }, onSessionExpired,
      ),
    detail: (id: string, signal: AbortSignal) =>
      requestJson<BusinessProductDetail>(`${base}/${encodeURIComponent(id)}?limit=200`, { signal }, onSessionExpired),
    commercialReview: (id: string, offset: number, signal: AbortSignal) =>
      requestJson<ProductCommercialReview>(`${products}/${encodeURIComponent(id)}/commercial-review?${new URLSearchParams({ offset: String(offset) })}`, { signal }, onSessionExpired),
    setProductActive: (id: string, active: boolean, csrfToken: string, signal: AbortSignal) =>
      requestJson(`${products}/${encodeURIComponent(id)}`, {
        method: 'PATCH', body: { active }, csrfToken, signal,
      }, onSessionExpired),
    setVariantActive: (id: string, active: boolean, csrfToken: string, signal: AbortSignal) =>
      requestJson(`${commerce}/${encodeURIComponent(id)}`, {
        method: 'PATCH', body: { active }, csrfToken, signal,
      }, onSessionExpired),
    createProduct: (body: ProductCreateInput, csrfToken: string) =>
      requestJson<{ product_id: string }>(products, {
        method: 'POST', body, csrfToken,
      }, onSessionExpired),
    createFirstVariant: (productId: string, body: FirstVariantCreateInput, csrfToken: string) =>
      requestJson<{ sellable_item_id: string }>(`${products}/${encodeURIComponent(productId)}/sellable-items`, {
        method: 'POST', body, csrfToken,
      }, onSessionExpired),
    changePrice: (id: string, amount: string, csrfToken: string, signal: AbortSignal) =>
      requestJson(`${commerce}/${encodeURIComponent(id)}/price`, {
        method: 'PUT', body: { amount, currency: 'USD' }, csrfToken, signal,
      }, onSessionExpired),
    listMedia: (scope: 'product' | 'sellable_item', id: string, signal: AbortSignal) =>
      requestJson<{ items: CatalogMedia[] }>(scope === 'product'
        ? `${products}/${encodeURIComponent(id)}/media`
        : `${commerce}/${encodeURIComponent(id)}/media`, { signal }, onSessionExpired),
    addMedia: (scope: 'product' | 'sellable_item', id: string, assetUrl: string, altText: string | null,
      csrfToken: string, signal: AbortSignal) => requestJson<CatalogMedia>(media, {
        method: 'POST', body: { [scope === 'product' ? 'product_id' : 'sellable_item_id']: id,
          asset_url: assetUrl, alt_text: altText, is_primary: true }, csrfToken, signal,
      }, onSessionExpired),
    replaceMedia: (id: string, assetUrl: string, altText: string | null, csrfToken: string, signal: AbortSignal) =>
      requestJson<CatalogMedia>(`${media}/${encodeURIComponent(id)}`, {
        method: 'PATCH', body: { asset_url: assetUrl, alt_text: altText }, csrfToken, signal,
      }, onSessionExpired),
    removeMedia: (id: string, csrfToken: string, signal: AbortSignal) =>
      requestJson<CatalogMedia>(`${media}/${encodeURIComponent(id)}`, {
        method: 'PATCH', body: { active: false }, csrfToken, signal,
      }, onSessionExpired),
  }
}
