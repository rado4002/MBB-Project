export type HumanRole = 'administrator' | 'operator' | 'analyst'

export interface HumanSummary {
  account_id: string
  username: string
  display_name: string
  role: HumanRole
}

export interface BrowserSession {
  human: HumanSummary
  capabilities: string[]
  must_change_password: boolean
  idle_expires_at_epoch: number
  absolute_expires_at_epoch: number
  recent_reauthentication_expires_at_epoch: number | null
  csrf_token?: string | null
}

export interface CsrfResponse {
  csrf_token: string
  expires_at_epoch: number
}

export interface LoginRequest {
  username: string
  password: string
}

export interface PasswordChangeRequest {
  current_password: string
  new_password: string
}

export interface ReauthenticateRequest {
  password: string
}

export interface LogoutResponse {
  logged_out: boolean
}
