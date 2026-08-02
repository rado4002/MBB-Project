import { Navigate, Route, Routes } from 'react-router-dom'
import { useAuth } from '../auth/AuthProvider'
import { ProtectedRoute } from '../auth/ProtectedRoute'
import { ApplicationShell } from '../components/ApplicationShell'
import { PageErrorState } from '../components/PageErrorState'
import { PageLoadingState } from '../components/PageLoadingState'
import { AccountPage } from '../features/account/AccountPage'
import { LoginPage } from '../features/auth/LoginPage'
import { PasswordChangePage } from '../features/auth/PasswordChangePage'
import { InboxFoundationPage } from '../features/inbox/InboxFoundationPage'
import { NotFoundPage } from '../features/NotFoundPage'
import { SessionPage } from '../features/session/SessionPage'

function EntryRoute() {
  const auth = useAuth()
  if (auth.status === 'initializing') return <PageLoadingState />
  if (auth.status === 'unavailable') return <PageErrorState onRetry={() => void auth.initialize()} />
  if (auth.status === 'password_change_required') return <Navigate to="/password-change" replace />
  if (auth.status === 'authenticated') return <Navigate to="/inbox" replace />
  return <Navigate to="/login" replace />
}

function LoginRoute() {
  const auth = useAuth()
  if (auth.status === 'initializing') return <PageLoadingState />
  if (auth.status === 'unavailable') return <PageErrorState onRetry={() => void auth.initialize()} />
  if (auth.status === 'password_change_required') return <Navigate to="/password-change" replace />
  if (auth.status === 'authenticated') return <Navigate to="/inbox" replace />
  return <LoginPage />
}

export function AppRouter() {
  return (
    <Routes>
      <Route path="/" element={<EntryRoute />} />
      <Route path="/login" element={<LoginRoute />} />
      <Route
        path="/password-change"
        element={
          <ProtectedRoute allowPasswordChange>
            <PasswordChangePage />
          </ProtectedRoute>
        }
      />
      <Route
        element={
          <ProtectedRoute>
            <ApplicationShell />
          </ProtectedRoute>
        }
      >
        <Route path="/inbox" element={<InboxFoundationPage />} />
        <Route path="/account" element={<AccountPage />} />
        <Route path="/session" element={<SessionPage />} />
      </Route>
      <Route path="*" element={<NotFoundPage />} />
    </Routes>
  )
}
