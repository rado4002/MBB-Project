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

  it('sends an ownership transition with CSRF, idempotency, and no extra fields', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({
          conversation_id: 'conversation-id',
          ownership: {
            owner_type: 'human',
            human_owner: { account_id: 'operator-id', display_name: 'Operator' },
            ai_execution_state: 'paused',
            version: 2,
            updated_at: '2026-08-04T00:00:00Z',
          },
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      ),
    )
    vi.stubGlobal('fetch', fetchMock)

    await createConversationApiClient().changeOwnership(
      'conversation-id',
      { target_owner_type: 'human', expected_version: 1 },
      '11111111-1111-4111-8111-111111111111',
      'csrf-token',
    )

    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/v1/operator/conversations/conversation-id/ownership')
    expect(init).toMatchObject({
      method: 'POST',
      credentials: 'same-origin',
      cache: 'no-store',
      body: JSON.stringify({ target_owner_type: 'human', expected_version: 1 }),
    })
    const headers = new Headers(init?.headers)
    expect(headers.get('X-CSRF-Token')).toBe('csrf-token')
    expect(headers.get('Idempotency-Key')).toBe(
      '11111111-1111-4111-8111-111111111111',
    )
  })

  it('sends a plain-text operator reply with CSRF and the browser UUID', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      new Response(
        JSON.stringify({ message_id: '11111111-1111-4111-8111-111111111111' }),
        { status: 202, headers: { 'Content-Type': 'application/json' } },
      ),
    )
    vi.stubGlobal('fetch', fetchMock)

    await createConversationApiClient().createReply(
      'conversation-id',
      { text: 'Bonjour Marie', expected_ownership_version: 2 },
      '11111111-1111-4111-8111-111111111111',
      'csrf-token',
    )

    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/v1/operator/conversations/conversation-id/replies')
    expect(init).toMatchObject({
      method: 'POST',
      credentials: 'same-origin',
      cache: 'no-store',
      body: JSON.stringify({ text: 'Bonjour Marie', expected_ownership_version: 2 }),
    })
    const headers = new Headers(init?.headers)
    expect(headers.get('Content-Type')).toBe('application/json')
    expect(headers.get('X-CSRF-Token')).toBe('csrf-token')
    expect(headers.get('Idempotency-Key')).toBe(
      '11111111-1111-4111-8111-111111111111',
    )
  })
})
