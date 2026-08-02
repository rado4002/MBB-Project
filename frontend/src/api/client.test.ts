import { afterEach, describe, expect, it, vi } from 'vitest'
import { createAuthApiClient } from './client'
import { normalizeApiError } from './errors'
import { sessionFixture } from '../test/fixtures'

function jsonResponse(body: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
}

afterEach(() => vi.unstubAllGlobals())

describe('browser auth API client', () => {
  it('uses a relative session URL with same-origin credentials, no-store, and no CSRF', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(sessionFixture()))
    vi.stubGlobal('fetch', fetchMock)

    await createAuthApiClient().getSession()

    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/v1/auth/session')
    expect(init).toMatchObject({ method: 'GET', credentials: 'same-origin', cache: 'no-store' })
    expect(new Headers(init?.headers).has('X-CSRF-Token')).toBe(false)
  })

  it('sends JSON and the memory CSRF value on every mutation', async () => {
    const response = { ...sessionFixture(), csrf_token: 'rotated' }
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(response))
    vi.stubGlobal('fetch', fetchMock)
    const client = createAuthApiClient()

    await client.changePassword(
      { current_password: 'old-secret', new_password: 'new-secret' },
      'csrf-memory',
    )

    const [url, init] = fetchMock.mock.calls[0]
    const headers = new Headers(init?.headers)
    expect(url).toBe('/api/v1/auth/password/change')
    expect(headers.get('Content-Type')).toBe('application/json')
    expect(headers.get('X-CSRF-Token')).toBe('csrf-memory')
    expect(init?.body).toBe(JSON.stringify({ current_password: 'old-secret', new_password: 'new-secret' }))
  })

  it('supports the reauthentication contract without exposing a workflow', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      jsonResponse({ ...sessionFixture(), csrf_token: 'reauth-rotated' }),
    )
    vi.stubGlobal('fetch', fetchMock)

    const credentials = { password: 'one-time-input' }
    await createAuthApiClient().reauthenticate(credentials, 'csrf')

    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/auth/reauthenticate')
    expect(fetchMock.mock.calls[0][1]?.body).toBe(JSON.stringify({ password: 'one-time-input' }))
    expect(credentials.password).toBe('')
  })

  it('passes AbortSignal cancellation to fetch', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(sessionFixture()))
    vi.stubGlobal('fetch', fetchMock)
    const controller = new AbortController()

    await createAuthApiClient().getSession(controller.signal)

    expect(fetchMock.mock.calls[0][1]?.signal).toBe(controller.signal)
  })

  it('centralizes protected-request session expiration', async () => {
    const expired = vi.fn()
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof fetch>().mockResolvedValue(
        jsonResponse({ error: { code: 'session_invalid', request_id: 'expired' } }, { status: 401 }),
      ),
    )

    await expect(createAuthApiClient(expired).logout('csrf')).rejects.toMatchObject({
      category: 'session_expired',
    })
    expect(expired).toHaveBeenCalledOnce()
  })

  it('does not treat invalid login credentials as a global session expiration', async () => {
    const expired = vi.fn()
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse({ csrf_token: 'preauth', expires_at_epoch: 100 }))
      .mockResolvedValueOnce(
        jsonResponse({ error: { code: 'invalid_credentials', request_id: 'login' } }, { status: 401 }),
      )
    vi.stubGlobal('fetch', fetchMock)

    const client = createAuthApiClient(expired)
    const csrf = await client.getCsrf()
    await expect(client.login({ username: 'user', password: 'wrong' }, csrf.csrf_token)).rejects.toMatchObject({
      category: 'credentials',
    })
    expect(expired).not.toHaveBeenCalled()
  })
})

describe('safe API error normalization', () => {
  it('normalizes E1 envelopes and gives the header request ID precedence', async () => {
    const error = await normalizeApiError(
      jsonResponse(
        { error: { code: 'SERVICE_UNAVAILABLE', message: 'internal detail', request_id: 'body-id' } },
        { status: 503, headers: { 'X-Request-ID': 'header-id' } },
      ),
    )
    expect(error).toMatchObject({ status: 503, code: 'SERVICE_UNAVAILABLE', category: 'unavailable', requestId: 'header-id' })
    expect(error).not.toHaveProperty('raw')
  })

  it('normalizes lowercase middleware detail forms', async () => {
    const error = await normalizeApiError(
      jsonResponse(
        { detail: { code: 'csrf_invalid', request_id: 'lowercase-id' } },
        { status: 403 },
      ),
    )
    expect(error).toMatchObject({ code: 'csrf_invalid', category: 'csrf', requestId: 'lowercase-id' })
  })

  it('handles malformed and empty error bodies without retaining response details', async () => {
    const malformed = await normalizeApiError(new Response('{not-json', { status: 502 }))
    const empty = await normalizeApiError(new Response(null, { status: 500 }))
    expect(malformed).toMatchObject({ code: 'http_502', category: 'unavailable' })
    expect(empty).toMatchObject({ code: 'http_500', category: 'unavailable' })
  })

  it('uses Retry-After headers before body retry values', async () => {
    const error = await normalizeApiError(
      jsonResponse(
        { error: { code: 'authentication_throttled', retry_after_seconds: 90 } },
        { status: 429, headers: { 'Retry-After': '45' } },
      ),
    )
    expect(error).toMatchObject({ category: 'throttled', retryAfterSeconds: 45 })
  })
})
