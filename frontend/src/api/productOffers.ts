import { requestJson } from './client'

export interface ProductOffer {
  product_id: string
  sellable_item_id: string
  sku: string | null
  product_name: string
  category_code: string
  description: string
  model_label: string | null
  attributes: Record<string, string | number | boolean>
  primary_media: { media_id: string; asset_url: string; alt_text: string | null; source_scope: 'product' | 'sellable_item' } | null
  price_id: string | null
  current_usd_price: string | null
  price_currency: 'USD'
  price_effective_at: string | null
  cdf_quote_status: 'available' | 'cdf_quote_unavailable'
  cdf_quote_unavailable_reason: 'current_usd_price_unavailable' | 'current_fx_unavailable' | null
  derived_cdf_quote: { currency: 'CDF'; cdf_amount: string; exchange_rate_id: string; usd_to_cdf_rate: string; exchange_rate_effective_at: string } | null
  inventory_status: 'available' | 'out_of_stock' | 'unknown'
  inventory_configured: boolean
  inventory_updated_at: string | null
  offer_status: 'sellable_now' | 'availability_unconfirmed' | 'out_of_stock' | 'price_unavailable' | 'inactive'
  is_sellable_now: boolean
  reason_code: 'sellable_now' | 'availability_unconfirmed' | 'inventory_out_of_stock' | 'price_unavailable' | 'product_inactive' | 'sellable_item_inactive'
  read_at: string
}

export interface OperatorOrderDraft {
  draft_id: string
  draft_version: number
  status: 'awaiting_confirmation' | 'confirmed' | 'cancelled' | 'invalidated'
  product_id: string
  sellable_item_id: string
  product_name: string
  model_label: string | null
  quantity: number
  unit_price_usd: string
  unit_price_cdf: string
  total_cdf: string
  confirmation_code: string
  required_customer_reply: string | null
  customer_confirmation_text: string
  presented_to_customer: false
  replayed: boolean
}

export function createProductOfferClient(onSessionExpired: () => void) {
  const path = '/api/v1/operator/product-offers'
  return {
    search: (query: string, signal: AbortSignal) =>
      requestJson<{ items: ProductOffer[] }>(
        `${path}?${new URLSearchParams({ query, limit: '20' })}`,
        { signal }, onSessionExpired,
      ),
    detail: (id: string, signal: AbortSignal) =>
      requestJson<ProductOffer>(`${path}/${encodeURIComponent(id)}`, { signal }, onSessionExpired),
    prepareDraft: (
      conversationId: string,
      offer: ProductOffer,
      quantity: number,
      expectedOwnershipVersion: number,
      idempotencyKey: string,
      csrfToken: string,
      signal?: AbortSignal,
    ) => {
      if (!offer.price_id || !offer.derived_cdf_quote || !offer.inventory_updated_at) {
        throw new Error('Offer is not eligible for an Order draft')
      }
      return requestJson<OperatorOrderDraft>(
        `/api/v1/operator/conversations/${encodeURIComponent(conversationId)}/order-drafts`,
        {
          method: 'POST',
          body: {
            expected_ownership_version: expectedOwnershipVersion,
            product_id: offer.product_id,
            sellable_item_id: offer.sellable_item_id,
            price_id: offer.price_id,
            exchange_rate_id: offer.derived_cdf_quote.exchange_rate_id,
            inventory_updated_at: offer.inventory_updated_at,
            quantity,
          },
          idempotencyKey,
          csrfToken,
          signal,
        },
        onSessionExpired,
      )
    },
  }
}
