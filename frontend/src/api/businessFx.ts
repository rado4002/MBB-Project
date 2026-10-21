import { requestJson } from './client'

export interface FxRate {
  exchange_rate_id: string
  rate: string
  authority_mode: 'MANUAL' | 'AUTOMATIC'
  source: 'MBB_ADMIN' | 'EXCHANGE_RATE_API'
  published_at: string | null
  effective_at: string
  ended_at: string | null
}

export interface FxAuthority {
  mode: 'MANUAL' | 'AUTOMATIC'
  revision: number
  updated_at: string
}

export function createBusinessFxClient(onSessionExpired: () => void) {
  const base = '/api/v1/operator/commerce/exchange-rates/usd-cdf'
  return {
    history: (signal: AbortSignal) => requestJson<{ items: FxRate[] }>(base, { signal }, onSessionExpired),
    authority: (signal: AbortSignal) => requestJson<FxAuthority>(`${base}/authority`, { signal }, onSessionExpired),
    setManualRate: (rate: string, csrfToken: string) => requestJson<FxRate>(base, {
      method: 'PUT', body: { base_currency: 'USD', quote_currency: 'CDF', rate }, csrfToken,
    }, onSessionExpired),
    setManualMode: (csrfToken: string) => requestJson<FxAuthority>(`${base}/authority`, {
      method: 'PUT', body: { mode: 'MANUAL' }, csrfToken,
    }, onSessionExpired),
  }
}
