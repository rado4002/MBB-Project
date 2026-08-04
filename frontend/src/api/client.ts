import type {
  BrowserSession,
  CsrfResponse,
  LoginRequest,
  LogoutResponse,
  PasswordChangeRequest,
  ReauthenticateRequest,
} from './contracts/auth'
import { ApiError, normalizeApiError } from './errors'

const AUTH_BASE = '/api/v1/auth'

export interface RequestOptions {
  method?: 'GET' | 'POST'
  body?: object
  csrfToken?: string
  idempotencyKey?: string
  signal?: AbortSignal
  notifySessionExpiration?: boolean
}

export async function requestJson<T>(
  path: string,
  options: RequestOptions = {},
  onSessionExpired: () => void = () => undefined,
): Promise<T> {
  if (!path.startsWith('/')) throw new Error('API paths must be relative')
  const method = options.method ?? 'GET'
  const headers = new Headers()
  if (options.body !== undefined) {
    headers.set('Content-Type', 'application/json')
  }
  if (options.csrfToken) headers.set('X-CSRF-Token', options.csrfToken)
  if (options.idempotencyKey) {
    headers.set('Idempotency-Key', options.idempotencyKey)
  }

  const response = await fetch(path, {
    method,
    credentials: 'same-origin',
    cache: 'no-store',
    headers,
    ...(options.body !== undefined ? { body: JSON.stringify(options.body) } : {}),
    ...(options.signal ? { signal: options.signal } : {}),
  })

  if (!response.ok) {
    const error = await normalizeApiError(response)
    if (
      options.notifySessionExpiration !== false &&
      error.category === 'session_expired'
    ) {
      onSessionExpired()
    }
    throw error
  }

  try {
    return (await response.json()) as T
  } catch {
    throw new ApiError({
      status: response.status,
      code: 'malformed_success_response',
      category: 'unavailable',
    })
  }
}

export interface AuthApiClient {
  getCsrf(signal?: AbortSignal): Promise<CsrfResponse>
  login(body: LoginRequest, csrfToken: string, signal?: AbortSignal): Promise<BrowserSession>
  getSession(signal?: AbortSignal): Promise<BrowserSession>
  changePassword(
    body: PasswordChangeRequest,
    csrfToken: string,
    signal?: AbortSignal,
  ): Promise<BrowserSession>
  reauthenticate(
    body: ReauthenticateRequest,
    csrfToken: string,
    signal?: AbortSignal,
  ): Promise<BrowserSession>
  logout(csrfToken: string, signal?: AbortSignal): Promise<LogoutResponse>
}

export function createAuthApiClient(
  onSessionExpired: () => void = () => undefined,
): AuthApiClient {
  const request = <T,>(path: string, options: RequestOptions = {}) =>
    requestJson<T>(path, options, onSessionExpired)

  return {
    getCsrf: (signal) =>
      request<CsrfResponse>(`${AUTH_BASE}/csrf`, {
        signal,
        notifySessionExpiration: false,
      }),
    login: (body, csrfToken, signal) =>
      request<BrowserSession>(`${AUTH_BASE}/login`, {
        method: 'POST',
        body,
        csrfToken,
        signal,
        notifySessionExpiration: false,
      }),
    getSession: (signal) =>
      request<BrowserSession>(`${AUTH_BASE}/session`, {
        signal,
        notifySessionExpiration: false,
      }),
    changePassword: (body, csrfToken, signal) =>
      request<BrowserSession>(`${AUTH_BASE}/password/change`, {
        method: 'POST',
        body,
        csrfToken,
        signal,
      }),
    reauthenticate: async (body, csrfToken, signal) => {
      const payload = { password: body.password }
      try {
        return await request<BrowserSession>(`${AUTH_BASE}/reauthenticate`, {
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
    logout: (csrfToken, signal) =>
      request<LogoutResponse>(`${AUTH_BASE}/logout`, {
        method: 'POST',
        body: {},
        csrfToken,
        signal,
      }),
  }
}
