import type { BrowserSession } from '../api/contracts/auth'
import type { ApiError } from '../api/errors'

export type AuthStatus =
  | 'initializing'
  | 'anonymous'
  | 'password_change_required'
  | 'authenticated'
  | 'unavailable'
  | 'logout_unconfirmed'

export interface AuthState {
  status: AuthStatus
  session: BrowserSession | null
  csrfToken: string | null
  error: ApiError | null
}

export const initialAuthState: AuthState = {
  status: 'initializing',
  session: null,
  csrfToken: null,
  error: null,
}

export type AuthAction =
  | { type: 'initializing' }
  | { type: 'anonymous' }
  | { type: 'unavailable'; error: ApiError }
  | { type: 'session_resolved'; session: BrowserSession; csrfToken?: string | null }
  | { type: 'csrf_received'; csrfToken: string }
  | { type: 'logout_started' }
  | { type: 'logout_failed'; error: ApiError; csrfToken: string | null }

export function authReducer(state: AuthState, action: AuthAction): AuthState {
  switch (action.type) {
    case 'initializing':
      return initialAuthState
    case 'anonymous':
      return { status: 'anonymous', session: null, csrfToken: null, error: null }
    case 'unavailable':
      return {
        status: 'unavailable',
        session: null,
        csrfToken: null,
        error: action.error,
      }
    case 'session_resolved':
      return {
        status: action.session.must_change_password
          ? 'password_change_required'
          : 'authenticated',
        session: action.session,
        csrfToken: action.csrfToken ?? state.csrfToken,
        error: null,
      }
    case 'csrf_received':
      return { ...state, csrfToken: action.csrfToken }
    case 'logout_started':
      return {
        status: 'logout_unconfirmed',
        session: null,
        csrfToken: state.csrfToken,
        error: null,
      }
    case 'logout_failed':
      return {
        status: 'logout_unconfirmed',
        session: null,
        csrfToken: action.csrfToken,
        error: action.error,
      }
  }
}
