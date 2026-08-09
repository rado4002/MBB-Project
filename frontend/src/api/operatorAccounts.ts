import { requestJson } from './client'
import type {
  OperatorAccountCreateRequest,
  OperatorAccountListResponse,
  OperatorAccountSummary,
  OperatorPasswordSetRequest,
} from './contracts/operatorAccounts'

const BASE = '/api/v1/operator/accounts'

export interface OperatorAccountsApiClient {
  list(signal?: AbortSignal): Promise<OperatorAccountListResponse>
  create(
    body: OperatorAccountCreateRequest,
    csrfToken: string,
    signal?: AbortSignal,
  ): Promise<OperatorAccountSummary>
  setPassword(
    accountId: string,
    body: OperatorPasswordSetRequest,
    csrfToken: string,
    signal?: AbortSignal,
  ): Promise<OperatorAccountSummary>
  disable(
    accountId: string,
    csrfToken: string,
    signal?: AbortSignal,
  ): Promise<OperatorAccountSummary>
  enable(
    accountId: string,
    body: OperatorPasswordSetRequest,
    csrfToken: string,
    signal?: AbortSignal,
  ): Promise<OperatorAccountSummary>
}

export function createOperatorAccountsApiClient(
  onSessionExpired: () => void = () => undefined,
): OperatorAccountsApiClient {
  const request = <T,>(path: string, options = {}) =>
    requestJson<T>(path, options, onSessionExpired)
  return {
    list: (signal) => request<OperatorAccountListResponse>(BASE, { signal }),
    create: async (body, csrfToken, signal) => {
      const payload = { ...body }
      try {
        return await request<OperatorAccountSummary>(BASE, {
          method: 'POST',
          body: payload,
          csrfToken,
          signal,
        })
      } finally {
        body.password = ''
        payload.password = ''
      }
    },
    setPassword: async (accountId, body, csrfToken, signal) => {
      const payload = { ...body }
      try {
        return await request<OperatorAccountSummary>(`${BASE}/${accountId}/password`, {
          method: 'POST',
          body: payload,
          csrfToken,
          signal,
        })
      } finally {
        body.new_password = ''
        payload.new_password = ''
      }
    },
    disable: (accountId, csrfToken, signal) =>
      request<OperatorAccountSummary>(`${BASE}/${accountId}/disable`, {
        method: 'POST',
        body: {},
        csrfToken,
        signal,
      }),
    enable: async (accountId, body, csrfToken, signal) => {
      const payload = { ...body }
      try {
        return await request<OperatorAccountSummary>(`${BASE}/${accountId}/enable`, {
          method: 'POST',
          body: payload,
          csrfToken,
          signal,
        })
      } finally {
        body.new_password = ''
        payload.new_password = ''
      }
    },
  }
}
