import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  type PropsWithChildren,
} from 'react'
import { createAuthApiClient, type AuthApiClient } from '../api/client'
import type { BrowserSession } from '../api/contracts/auth'
import { ApiError, asApiError } from '../api/errors'
import { authReducer, initialAuthState, type AuthState } from './authReducer'

interface AuthContextValue extends AuthState {
  initialize(): Promise<void>
  login(username: string, password: string): Promise<BrowserSession>
  changePassword(currentPassword: string, newPassword: string): Promise<BrowserSession>
  reauthenticate(password: string): Promise<BrowserSession>
  logout(): Promise<void>
  retryLogout(): Promise<void>
  handleSessionExpired(): void
}

const AuthContext = createContext<AuthContextValue | null>(null)

function requireRotatedCsrf(session: BrowserSession): string {
  if (!session.csrf_token) {
    throw new ApiError({
      status: 0,
      code: 'missing_csrf_token',
      category: 'unavailable',
    })
  }
  return session.csrf_token
}

export interface AuthProviderProps extends PropsWithChildren {
  clientFactory?: (onSessionExpired: () => void) => AuthApiClient
}

export function AuthProvider({
  children,
  clientFactory = createAuthApiClient,
}: AuthProviderProps) {
  const [state, dispatch] = useReducer(authReducer, initialAuthState)
  const stateRef = useRef(state)
  useEffect(() => {
    stateRef.current = state
  }, [state])

  const expireSession = useCallback(() => dispatch({ type: 'anonymous' }), [])
  const client = useMemo(() => clientFactory(expireSession), [clientFactory, expireSession])

  const initialize = useCallback(async () => {
    dispatch({ type: 'initializing' })
    try {
      const session = await client.getSession()
      dispatch({ type: 'session_resolved', session, csrfToken: null })
    } catch (unknownError) {
      const error = asApiError(unknownError)
      if (error.category === 'session_expired') dispatch({ type: 'anonymous' })
      else dispatch({ type: 'unavailable', error })
    }
  }, [client])

  useEffect(() => {
    const controller = new AbortController()
    client
      .getSession(controller.signal)
      .then((session) => {
        dispatch({ type: 'session_resolved', session, csrfToken: null })
      })
      .catch((unknownError: unknown) => {
        if (controller.signal.aborted) return
        const error = asApiError(unknownError)
        if (error.category === 'session_expired') dispatch({ type: 'anonymous' })
        else dispatch({ type: 'unavailable', error })
      })
    return () => controller.abort()
  }, [client])

  const freshCsrf = useCallback(async () => {
    const response = await client.getCsrf()
    dispatch({ type: 'csrf_received', csrfToken: response.csrf_token })
    return response.csrf_token
  }, [client])

  const login = useCallback(
    async (username: string, password: string) => {
      const csrfToken = await freshCsrf()
      const session = await client.login({ username, password }, csrfToken)
      const rotatedCsrf = requireRotatedCsrf(session)
      dispatch({ type: 'session_resolved', session, csrfToken: rotatedCsrf })
      return session
    },
    [client, freshCsrf],
  )

  const csrfForMutation = useCallback(async () => {
    return stateRef.current.csrfToken ?? freshCsrf()
  }, [freshCsrf])

  const changePassword = useCallback(
    async (currentPassword: string, newPassword: string) => {
      const csrfToken = await csrfForMutation()
      const session = await client.changePassword(
        { current_password: currentPassword, new_password: newPassword },
        csrfToken,
      )
      const rotatedCsrf = requireRotatedCsrf(session)
      dispatch({ type: 'session_resolved', session, csrfToken: rotatedCsrf })
      return session
    },
    [client, csrfForMutation],
  )

  const reauthenticate = useCallback(
    async (password: string) => {
      const csrfToken = await csrfForMutation()
      const session = await client.reauthenticate({ password }, csrfToken)
      const rotatedCsrf = requireRotatedCsrf(session)
      dispatch({ type: 'session_resolved', session, csrfToken: rotatedCsrf })
      return session
    },
    [client, csrfForMutation],
  )

  const performLogout = useCallback(
    async (csrfToken: string | null) => {
      let activeCsrf = csrfToken
      try {
        if (!activeCsrf) {
          const response = await client.getCsrf()
          activeCsrf = response.csrf_token
        }
        await client.logout(activeCsrf)
        dispatch({ type: 'anonymous' })
      } catch (unknownError) {
        const error = asApiError(unknownError)
        if (error.category === 'session_expired') {
          dispatch({ type: 'anonymous' })
          return
        }
        dispatch({ type: 'logout_failed', error, csrfToken: activeCsrf })
        throw error
      }
    },
    [client],
  )

  const logout = useCallback(async () => {
    const csrfToken = stateRef.current.csrfToken
    dispatch({ type: 'logout_started' })
    await performLogout(csrfToken)
  }, [performLogout])

  const retryLogout = useCallback(async () => {
    dispatch({ type: 'logout_started' })
    await performLogout(stateRef.current.csrfToken)
  }, [performLogout])

  const value = useMemo<AuthContextValue>(
    () => ({
      ...state,
      initialize,
      login,
      changePassword,
      reauthenticate,
      logout,
      retryLogout,
      handleSessionExpired: expireSession,
    }),
    [
      state,
      initialize,
      login,
      changePassword,
      reauthenticate,
      logout,
      retryLogout,
      expireSession,
    ],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

// This hook intentionally shares the provider's private context.
// eslint-disable-next-line react-refresh/only-export-components
export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext)
  if (!context) throw new Error('useAuth must be used within AuthProvider')
  return context
}
