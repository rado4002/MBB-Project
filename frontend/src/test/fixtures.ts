import type { BrowserSession, HumanRole } from '../api/contracts/auth'
import type { OperatorConversationQueueItem } from '../api/contracts/conversations'

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

export function conversationFixture(
  id = '11111111-1111-4111-8111-111111111111',
): OperatorConversationQueueItem {
  return {
    conversation_id: id,
    customer: {
      display_name: 'Marie Client',
      phone_masked: '***5678',
    },
    language: 'french',
    status: 'active',
    message_count: 4,
    latest_message: {
      preview: 'Bonjour, je souhaite des informations.',
      content_type: 'text',
      direction: 'inbound',
      occurred_at: '2026-08-02T12:30:00Z',
    },
    awaiting_response_since: '2026-08-02T12:30:00Z',
    open_escalation: { exists: false },
  }
}
