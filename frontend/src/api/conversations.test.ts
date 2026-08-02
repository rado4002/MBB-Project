import { afterEach, describe, expect, it, vi } from 'vitest'
import { createConversationApiClient } from './conversations'

afterEach(() => vi.unstubAllGlobals())

describe('E1 conversation queue client', () => {
  it('serializes only supported filters on a relative same-origin no-store request', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ items: [], next_cursor: null }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    await createConversationApiClient().listConversations({
      filters: {
        status: 'active',
        escalation_state: 'open',
        language: 'french',
      },
    })

    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe(
      '/api/v1/operator/conversations?status=active&escalation_state=open&language=french',
    )
    expect(init).toMatchObject({ method: 'GET', credentials: 'same-origin', cache: 'no-store' })
    expect(new Headers(init?.headers).has('X-CSRF-Token')).toBe(false)
  })

  it('sends the opaque cursor only to E1 for the requested next page', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ items: [], next_cursor: null }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    await createConversationApiClient().listConversations({
      filters: { language: 'swahili' },
      cursor: 'opaque+/cursor=',
    })

    expect(fetchMock.mock.calls[0][0]).toBe(
      '/api/v1/operator/conversations?language=swahili&cursor=opaque%2B%2Fcursor%3D',
    )
  })

  it('uses centralized session expiration and preserves Retry-After errors', async () => {
    const expired = vi.fn()
    const fetchMock = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ error: { code: 'AUTH_SESSION_EXPIRED', request_id: 'queue-expired' } }),
          { status: 401, headers: { 'Content-Type': 'application/json' } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ error: { code: 'SERVICE_UNAVAILABLE', request_id: 'queue-throttle' } }),
          { status: 429, headers: { 'Content-Type': 'application/json', 'Retry-After': '12' } },
        ),
      )
    vi.stubGlobal('fetch', fetchMock)
    const client = createConversationApiClient(expired)

    await expect(client.listConversations({ filters: {} })).rejects.toMatchObject({
      category: 'session_expired',
    })
    expect(expired).toHaveBeenCalledOnce()
    await expect(client.listConversations({ filters: {} })).rejects.toMatchObject({
      category: 'throttled',
      retryAfterSeconds: 12,
    })
  })

  it('requests conversation detail through the relative E1 route', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ conversation_id: 'conversation-id' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    await createConversationApiClient().getConversation('conversation/id')

    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/v1/operator/conversations/conversation%2Fid')
    expect(init).toMatchObject({ method: 'GET', credentials: 'same-origin', cache: 'no-store' })
    expect(new Headers(init?.headers).has('X-CSRF-Token')).toBe(false)
  })

  it('keeps the stable older-message cursor in the API request only', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(JSON.stringify({ items: [], next_older_cursor: null }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    await createConversationApiClient().getMessages('conversation-id', 'older+/cursor=')

    expect(fetchMock.mock.calls[0][0]).toBe(
      '/api/v1/operator/conversations/conversation-id/messages?before=older%2B%2Fcursor%3D',
    )
  })
})
