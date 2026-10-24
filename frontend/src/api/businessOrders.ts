import { requestJson } from './client'

export type OrderStatus = 'pending' | 'confirmed' | 'preparing' | 'delivering' | 'delivered' | 'cancelled'

export interface OrderQueueItem {
  order_id: string
  status: OrderStatus
  created_at: string
  updated_at: string
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
  delivered_at: string | null
  preparation: OrderPreparationEligibility
  dispatch: OrderDispatchEligibility
  delivery: OrderDeliveryEligibility
}

export interface OrderPreparationEligibility {
  eligible: boolean
  order_state: 'confirmed' | 'not_confirmed'
  payment_state: 'verified' | 'missing' | 'not_verified' | 'mismatch' | 'unknown'
  allocation_state: 'full' | 'missing' | 'mismatch' | 'unsupported'
  required_quantity: number | null
  allocated_quantity: number | null
}

export interface OrderDispatchEligibility {
  eligible: boolean
  order_state: 'preparing' | 'not_preparing'
  payment_state: 'verified' | 'missing' | 'not_verified' | 'mismatch'
  allocation_state: 'full_unconsumed' | 'missing' | 'mismatch' | 'consumed' | 'unsupported'
  inventory_state: 'ready' | 'conflict' | 'unsupported'
  delivery_method_state: 'supported' | 'unsupported'
  required_quantity: number | null
}

export interface OrderDeliveryEvidenceMessage {
  message_id: string
  content_excerpt: string
  content_truncated: boolean
  received_at: string
  inbound_receipt_order: number
  provenance: 'inbound_whatsapp_customer_message'
}

export interface OrderDeliveryEligibility {
  eligible: boolean
  order_state: 'delivering' | 'delivered' | 'not_delivering'
  delivery_method_state: 'supported' | 'unsupported'
  line_state: 'single' | 'unsupported'
  dispatch_state: 'matched' | 'missing'
  association_state: 'verified' | 'missing' | 'ambiguous' | 'not_applicable'
  evidence_state: 'available' | 'missing' | 'not_applicable'
  dispatch_occurred_at: string | null
  evidence_messages: OrderDeliveryEvidenceMessage[]
  evidence_has_more: boolean
}

export type OrderCancellationReason =
  | 'customer_requested'
  | 'duplicate_order'
  | 'commercial_terms_unavailable'
  | 'other_operational'

export interface OrderCancellationResponse {
  order_id: string
  status: 'cancelled'
  updated_at: string
  replayed: boolean
}

export interface OrderPreparationResponse {
  order_id: string
  status: 'preparing'
  updated_at: string
  replayed: boolean
}

export interface OrderDispatchResponse {
  order_id: string
  status: 'delivering'
  updated_at: string
  replayed: boolean
}

export interface OrderDeliveryConfirmationResponse {
  order_id: string
  status: 'delivered'
  delivered_at: string
  updated_at: string
  replayed: boolean
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
    cancel: (
      id: string,
      reasonCode: OrderCancellationReason,
      expectedUpdatedAt: string,
      idempotencyKey: string,
      csrfToken: string,
      signal: AbortSignal,
    ) => requestJson<OrderCancellationResponse>(`${base}/${encodeURIComponent(id)}/cancel`, {
      method: 'POST',
      body: { reason_code: reasonCode, expected_updated_at: expectedUpdatedAt },
      idempotencyKey,
      csrfToken,
      signal,
    }, onSessionExpired),
    prepare: (
      id: string,
      expectedUpdatedAt: string,
      idempotencyKey: string,
      csrfToken: string,
      signal: AbortSignal,
    ) => requestJson<OrderPreparationResponse>(`${base}/${encodeURIComponent(id)}/prepare`, {
      method: 'POST',
      body: { expected_updated_at: expectedUpdatedAt },
      idempotencyKey,
      csrfToken,
      signal,
    }, onSessionExpired),
    dispatch: (
      id: string,
      expectedUpdatedAt: string,
      handoffReference: string,
      idempotencyKey: string,
      csrfToken: string,
      signal: AbortSignal,
    ) => requestJson<OrderDispatchResponse>(`${base}/${encodeURIComponent(id)}/dispatch`, {
      method: 'POST',
      body: {
        expected_updated_at: expectedUpdatedAt,
        handoff_reference: handoffReference,
        attested: true,
      },
      idempotencyKey,
      csrfToken,
      signal,
    }, onSessionExpired),
    confirmDelivery: (
      id: string,
      expectedUpdatedAt: string,
      evidenceMessageId: string,
      idempotencyKey: string,
      csrfToken: string,
      signal: AbortSignal,
    ) => requestJson<OrderDeliveryConfirmationResponse>(`${base}/${encodeURIComponent(id)}/confirm-delivery`, {
      method: 'POST',
      body: {
        expected_updated_at: expectedUpdatedAt,
        evidence_message_id: evidenceMessageId,
        attested: true,
      },
      idempotencyKey,
      csrfToken,
      signal,
    }, onSessionExpired),
  }
}
