import type { ReactNode } from 'react'
import { Navigate, useLocation } from 'react-router-dom'
import { PageErrorState } from '../components/PageErrorState'
import { PageLoadingState } from '../components/PageLoadingState'
import { useAuth } from './AuthProvider'

interface ProtectedRouteProps {
  children: ReactNode
  allowPasswordChange?: boolean
}

export function ProtectedRoute({
  children,
  allowPasswordChange = false,
}: ProtectedRouteProps) {
  const auth = useAuth()
  const location = useLocation()

  if (auth.status === 'initializing') return <PageLoadingState />
  if (auth.status === 'unavailable') {
    return <PageErrorState onRetry={() => void auth.initialize()} />
  }
  if (auth.status === 'anonymous' || auth.status === 'logout_unconfirmed') {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />
  }
  if (auth.status === 'password_change_required' && !allowPasswordChange) {
    return <Navigate to="/password-change" replace />
  }
  if (auth.status === 'authenticated' && allowPasswordChange) {
    return <Navigate to="/inbox" replace />
  }
  return children
}
