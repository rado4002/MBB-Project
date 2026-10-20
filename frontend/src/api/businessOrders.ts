import { requestJson } from './client'

export type OrderStatus = 'pending' | 'confirmed' | 'preparing' | 'delivering' | 'delivered' | 'cancelled'

export interface OrderQueueItem {
  order_id: string
  status: OrderStatus
  created_at: string
  total_cdf: string
  currency: string
  item_count: number
  first_product_name: string | null
  conversation_id: string | null
}

export interface OrderLineSnapshot {
  product_id: string
  sellable_item_id: string | null
  product_name: string | null
  model_label: string | null
  quantity: number
  price_id: string | null
  unit_price_usd: string | null
  exchange_rate_id: string | null
  usd_to_cdf_rate: string | null
  unit_price_cdf: string
}

export interface OrderDetail extends OrderQueueItem {
  items: OrderLineSnapshot[]
  delivery_zone: string
}

export function createBusinessOrdersClient(onSessionExpired: () => void) {
  const base = '/api/v1/business/orders'
  return {
    list: (status: OrderStatus | null, offset: number, signal: AbortSignal) => {
      const params = new URLSearchParams({ limit: '20', offset: String(offset) })
      if (status) params.set('status', status)
      return requestJson<{ items: OrderQueueItem[]; has_more: boolean }>(
        `${base}?${params}`, { signal }, onSessionExpired,
      )
    },
    detail: (id: string, signal: AbortSignal) =>
      requestJson<OrderDetail>(`${base}/${encodeURIComponent(id)}`, { signal }, onSessionExpired),
  }
}
