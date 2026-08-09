export type OperatorAccountStatus = 'active' | 'disabled'

export interface OperatorAccountSummary {
  account_id: string
  username: string
  display_name: string
  email: string | null
  status: OperatorAccountStatus
  last_login_at: string | null
  created_at: string
}

export interface OperatorAccountListResponse {
  items: OperatorAccountSummary[]
}

export interface OperatorAccountCreateRequest {
  username: string
  display_name: string
  email: string | null
  password: string
}

export interface OperatorPasswordSetRequest {
  new_password: string
}
