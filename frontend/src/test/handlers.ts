import { http, HttpResponse } from 'msw'

export const handlers = [
  http.get('/api/v1/auth/session', () =>
    HttpResponse.json(
      {
        error: {
          code: 'session_required',
          message: 'A browser session is required.',
          request_id: 'session-default',
        },
      },
      { status: 401 },
    ),
  ),
]
