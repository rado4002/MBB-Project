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

export function createBusinessProductsClient(onSessionExpired: () => void) {
  const base = '/api/v1/business/products'
  const products = '/api/v1/operator/commerce/products'
  const commerce = '/api/v1/operator/commerce/sellable-items'
  return {
    list: (query: string, signal: AbortSignal) =>
      requestJson<{ items: BusinessProduct[]; has_more: boolean }>(
        `${base}?${new URLSearchParams({ query, limit: '50' })}`, { signal }, onSessionExpired,
      ),
    detail: (id: string, signal: AbortSignal) =>
      requestJson<BusinessProductDetail>(`${base}/${encodeURIComponent(id)}?limit=200`, { signal }, onSessionExpired),
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
    setAvailability: (id: string, status: ProductOffer['inventory_status'], csrfToken: string, signal: AbortSignal) =>
      requestJson(`${commerce}/${encodeURIComponent(id)}/inventory`, {
        method: 'PUT', body: { status }, csrfToken, signal,
      }, onSessionExpired),
  }
}
