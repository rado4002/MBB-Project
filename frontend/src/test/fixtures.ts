import type { BrowserSession, HumanRole } from '../api/contracts/auth'

export function sessionFixture(
  role: HumanRole = 'operator',
  mustChangePassword = false,
): BrowserSession {
  return {
    human: {
      account_id: 'account-test-only',
      username: `${role}.user`,
      display_name: role === 'administrator' ? 'Ada Admin' : role === 'operator' ? 'Omar Operator' : 'Ana Analyst',
      role,
    },
    capabilities: ['auth.reauthenticate'],
    must_change_password: mustChangePassword,
    idle_expires_at_epoch: 1_900_000_000,
    absolute_expires_at_epoch: 1_900_003_600,
    recent_reauthentication_expires_at_epoch: null,
  }
}
