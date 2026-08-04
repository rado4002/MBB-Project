export type ApiErrorCategory =
  | 'credentials'
  | 'session_expired'
  | 'throttled'
  | 'validation'
  | 'csrf'
  | 'origin'
  | 'json_required'
  | 'unavailable'
  | 'forbidden'
  | 'unknown'

export interface NormalizedApiError {
  status: number
  code: string
  category: ApiErrorCategory
  requestId?: string
  retryAfterSeconds?: number
  operatorMessage?: string
}

export class ApiError extends Error implements NormalizedApiError {
  readonly status: number
  readonly code: string
  readonly category: ApiErrorCategory
  readonly requestId?: string
  readonly retryAfterSeconds?: number
  readonly operatorMessage?: string

  constructor(error: NormalizedApiError) {
    super(error.category)
    this.name = 'ApiError'
    this.status = error.status
    this.code = error.code
    this.category = error.category
    this.requestId = error.requestId
    this.retryAfterSeconds = error.retryAfterSeconds
    this.operatorMessage = error.operatorMessage
  }
}

type UnknownRecord = Record<string, unknown>

function isRecord(value: unknown): value is UnknownRecord {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function safeString(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() ? value.trim() : undefined
}

function safeSeconds(value: unknown): number | undefined {
  if (typeof value === 'number' && Number.isFinite(value) && value >= 0) {
    return Math.ceil(value)
  }
  if (typeof value === 'string' && /^\d+$/.test(value.trim())) {
    return Number.parseInt(value, 10)
  }
  return undefined
}

function retryAfterFromHeader(value: string | null): number | undefined {
  if (!value) return undefined
  const seconds = safeSeconds(value)
  if (seconds !== undefined) return seconds
  const date = Date.parse(value)
  if (Number.isNaN(date)) return undefined
  return Math.max(0, Math.ceil((date - Date.now()) / 1000))
}

function detailFromBody(body: unknown): UnknownRecord {
  if (!isRecord(body)) return {}
  if (isRecord(body.error)) return body.error
  if (isRecord(body.detail)) return body.detail
  return body
}

function categoryFor(status: number, code: string): ApiErrorCategory {
  const normalized = code.toLowerCase()
  if (
    normalized === 'invalid_credentials' ||
    normalized === 'authentication_failed'
  ) {
    return 'credentials'
  }
  if (
    status === 401 ||
    normalized === 'session_invalid' ||
    normalized === 'session_required' ||
    normalized === 'auth_session_expired'
  ) {
    return 'session_expired'
  }
  if (status === 429 || normalized.includes('throttl')) return 'throttled'
  if (normalized.includes('csrf')) return 'csrf'
  if (normalized.includes('origin')) return 'origin'
  if (normalized.includes('json')) return 'json_required'
  if (status === 422 || normalized.includes('validation') || normalized === 'request_invalid') {
    return 'validation'
  }
  if (status === 403 || normalized === 'forbidden' || normalized === 'capability_required') {
    return 'forbidden'
  }
  if (status >= 500 || normalized.includes('unavailable') || normalized === 'browser_auth_disabled') {
    return 'unavailable'
  }
  return 'unknown'
}

export async function normalizeApiError(response: Response): Promise<ApiError> {
  let body: unknown
  try {
    body = await response.json()
  } catch {
    body = undefined
  }
  const detail = detailFromBody(body)
  const code = safeString(detail.code) ?? `http_${response.status}`
  // Middleware owns the response header, so it deterministically takes
  // precedence over a body-provided request ID when both are present.
  const requestId =
    safeString(response.headers.get('X-Request-ID')) ??
    safeString(detail.request_id) ??
    safeString(detail.requestId)
  const retryAfterSeconds =
    retryAfterFromHeader(response.headers.get('Retry-After')) ??
    safeSeconds(detail.retry_after_seconds) ??
    safeSeconds(detail.retryAfterSeconds)
  const operatorMessage = safeString(detail.message)

  return new ApiError({
    status: response.status,
    code,
    category: categoryFor(response.status, code),
    ...(requestId ? { requestId } : {}),
    ...(retryAfterSeconds !== undefined ? { retryAfterSeconds } : {}),
    ...(operatorMessage ? { operatorMessage } : {}),
  })
}

export function errorMessage(error: ApiError): string {
  switch (error.category) {
    case 'credentials':
      return 'The username or password is incorrect.'
    case 'session_expired':
      return 'Your session has ended. Sign in again to continue.'
    case 'throttled':
      return error.retryAfterSeconds !== undefined
        ? `Too many attempts. Try again in ${error.retryAfterSeconds} seconds.`
        : 'Too many attempts. Please wait before trying again.'
    case 'validation':
      return 'Check the information entered and try again.'
    case 'csrf':
      return 'The security check expired. Please try again.'
    case 'origin':
    case 'json_required':
      return 'The sign-in request could not be accepted. Please try again.'
    case 'forbidden':
      return 'You do not have permission to perform this action.'
    case 'unavailable':
      return 'Authentication is temporarily unavailable. Please try again.'
    default:
      return 'The request could not be completed. Please try again.'
  }
}

export function asApiError(error: unknown): ApiError {
  if (error instanceof ApiError) return error
  return new ApiError({ status: 0, code: 'network_error', category: 'unavailable' })
}
