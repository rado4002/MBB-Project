import { requestJson } from './client'

export type CustomerLanguage = 'lingala' | 'french' | 'swahili'

export interface BusinessCustomerProfile {
  conversation_id: string
  display_name: string | null
  phone_masked: string
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

export interface CustomerProfileSnapshot {
  display_name: string | null
  city: string
  preferred_language: CustomerLanguage
}

export function createBusinessCustomersClient(onSessionExpired: () => void) {
  const base = '/api/v1/business/customers'
  return {
    detail: (conversationId: string, signal: AbortSignal) =>
      requestJson<BusinessCustomerProfile>(
        `${base}/${encodeURIComponent(conversationId)}`,
        { signal },
        onSessionExpired,
      ),
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
