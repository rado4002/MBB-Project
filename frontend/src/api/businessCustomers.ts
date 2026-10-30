import { requestJson } from './client'

export type CustomerLanguage = 'lingala' | 'french' | 'swahili'

export interface BusinessCustomerProfile {
  customer_id: string
  conversation_id: string | null
  display_name: string | null
  phone_number: string
  city: string
  preferred_language: CustomerLanguage
  opted_out: boolean
  opted_out_at: string | null
  consent_given: boolean
  consent_timestamp: string | null
  communication_channels: Array<{
    channel: 'whatsapp'
    provenance: 'verified_provider_message'
  }>
  acquisition_sources: string[]
  opt_out_evidence: Array<{
    message_id: string
    received_at: string
    content_excerpt: string
    content_truncated: boolean
    provenance: 'inbound_whatsapp_customer_message'
  }>
}

export interface BusinessCustomerListItem {
  customer_id: string
  customer_reference: string
  conversation_id: string | null
  display_name: string | null
  phone_number: string
  communication_channel: 'whatsapp' | null
  acquisition_source: string | null
  acquisition_source_provenance: 'earliest_recorded_lead' | null
  last_interaction_at: string
}

export interface BusinessCustomerListResponse {
  items: BusinessCustomerListItem[]
  has_more: boolean
}

export interface CustomerProfileSnapshot {
  display_name: string | null
  city: string
  preferred_language: CustomerLanguage
}

export function createBusinessCustomersClient(onSessionExpired: () => void) {
  const base = '/api/v1/business/customers'
  return {
    list: (query: string, offset: number, signal: AbortSignal) => {
      const params = new URLSearchParams({ offset: String(offset) })
      if (query) params.set('q', query)
      return requestJson<BusinessCustomerListResponse>(
        `${base}?${params.toString()}`,
        { signal },
        onSessionExpired,
      )
    },
    detail: (conversationId: string, signal: AbortSignal) =>
      requestJson<BusinessCustomerProfile>(
        `${base}/${encodeURIComponent(conversationId)}`,
        { signal },
        onSessionExpired,
      ),
    detailByReference: (customerReference: string, conversationId: string, signal: AbortSignal) => {
      const query = new URLSearchParams()
      if (conversationId) query.set('conversation_id', conversationId)
      const suffix = query.toString() ? `?${query.toString()}` : ''
      return requestJson<BusinessCustomerProfile>(
        `${base}/by-reference/${encodeURIComponent(customerReference)}${suffix}`,
        { signal },
        onSessionExpired,
      )
    },
    update: (
      conversationId: string,
      expected: CustomerProfileSnapshot,
      profile: CustomerProfileSnapshot,
      reasonCode: 'customer_provided_update' | 'operator_correction',
      csrfToken: string,
      signal: AbortSignal,
    ) => requestJson<BusinessCustomerProfile>(
      `${base}/${encodeURIComponent(conversationId)}/profile`,
      { method: 'PUT', body: { expected, profile, reason_code: reasonCode }, csrfToken, signal },
      onSessionExpired,
    ),
    optOut: (
      conversationId: string,
      evidenceMessageId: string,
      csrfToken: string,
      signal: AbortSignal,
    ) => requestJson<BusinessCustomerProfile>(
      `${base}/${encodeURIComponent(conversationId)}/opt-out`,
      {
        method: 'POST',
        body: { expected_opted_out: false, evidence_message_id: evidenceMessageId, attested: true },
        csrfToken,
        signal,
      },
      onSessionExpired,
    ),
  }
}
