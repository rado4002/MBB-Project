import { requestJson } from './client'

export interface StockItem {
  product_id: string
  sellable_item_id: string
  product_name: string
  model_label: string | null
  sku: string | null
  product_active: boolean
  variant_active: boolean
  availability: 'available' | 'out_of_stock' | 'unknown'
  quantity?: number | null
  inventory_updated_at?: string | null
  current_usd_price?: string | null
}

export interface StockMovement {
  movement_id: string
  sellable_item_id: string
  operation_kind: 'receive' | 'adjust'
  before_quantity: number | null
  before_updated_at: string | null
  received_amount: number | null
  corrected_quantity: number | null
  after_quantity: number | null
  reason: string | null
  actor_account_id: string
  operation_key: string | null
  occurred_at: string
}

interface ReviewedState {
  expected_quantity: number | null
  expected_updated_at: string | null
  operation_key: string
}

export function createBusinessStockClient(onSessionExpired: () => void) {
  const base = '/api/v1/business/stock'
  const inventory = (id: string) => `/api/v1/operator/commerce/sellable-items/${encodeURIComponent(id)}/inventory`
  return {
    search: (query: string, itemId: string | null, signal: AbortSignal) => {
      const params = new URLSearchParams({ limit: '50' })
      if (query) params.set('query', query)
      if (itemId) params.set('item_id', itemId)
      return requestJson<{ items: StockItem[]; has_more: boolean }>(`${base}?${params}`, { signal }, onSessionExpired)
    },
    activity: (id: string, signal: AbortSignal) =>
      requestJson<{ items: StockMovement[] }>(`${inventory(id)}/activity?limit=20`, { signal }, onSessionExpired),
    receive: (id: string, receivedAmount: number, reviewed: ReviewedState, csrfToken: string, signal: AbortSignal) =>
      requestJson<StockMovement>(`${inventory(id)}/receive`, {
        method: 'POST', body: { received_amount: receivedAmount, ...reviewed }, csrfToken, signal,
      }, onSessionExpired),
    adjust: (id: string, correctedQuantity: number, reason: string, reviewed: ReviewedState, csrfToken: string, signal: AbortSignal) =>
      requestJson<StockMovement>(`${inventory(id)}/adjust`, {
        method: 'POST', body: { corrected_quantity: correctedQuantity, reason, ...reviewed }, csrfToken, signal,
      }, onSessionExpired),
  }
}
