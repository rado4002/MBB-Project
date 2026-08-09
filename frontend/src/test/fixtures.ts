import type { BrowserSession, HumanRole } from '../api/contracts/auth'
import type {
  OperatorConversationDetail,
  OperatorConversationQueueItem,
  OperatorInternalNoteItem,
  OperatorTimelineMessageItem,
} from '../api/contracts/conversations'

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
    capabilities: [
      'auth.reauthenticate',
      'conversation.read',
      'message.read',
      ...(role === 'analyst'
        ? []
        : [
            'message.reply',
            'internal_note.read',
            'internal_note.create',
            'escalation.create',
            'conversation.ownership.change',
          ]),
      ...(role === 'administrator' ? ['operator_account.manage'] : []),
    ],
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
    ownership: {
      owner_type: 'ai',
      human_owner: null,
      ai_execution_state: 'eligible',
      version: 1,
      updated_at: '2026-08-02T12:30:00Z',
    },
  }
}

export function conversationDetailFixture(
  id = '11111111-1111-4111-8111-111111111111',
): OperatorConversationDetail {
  return {
    conversation_id: id,
    status: 'active',
    language: 'french',
    message_count: 4,
    updated_at: '2026-08-02T12:30:00Z',
    customer: {
      display_name: 'Marie Client',
      phone_masked: '***5678',
    },
    lead: {
      score: 'warm',
      stage: 'qualifying',
      intent: 'product_information',
      product_interests: ['Solar starter kit'],
    },
    open_escalation: { exists: false },
    ownership: {
      owner_type: 'ai',
      human_owner: null,
      ai_execution_state: 'eligible',
      version: 1,
      updated_at: '2026-08-02T12:30:00Z',
    },
  }
}

export function messageFixture(
  id = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
  overrides: Partial<OperatorTimelineMessageItem> = {},
): OperatorTimelineMessageItem {
  return {
    kind: 'message',
    message_id: id,
    occurred_at: '2026-08-02T12:30:00Z',
    direction: 'inbound',
    sender_type: 'customer',
    operator_author: null,
    delivery_state: null,
    delivery_state_timestamp: null,
    content_type: 'text',
    text: 'Bonjour, je souhaite des informations.',
    media: null,
    language: 'french',
    ...overrides,
  }
}

export function internalNoteFixture(
  id = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd',
  overrides: Partial<OperatorInternalNoteItem> = {},
): OperatorInternalNoteItem {
  return {
    kind: 'internal_note',
    note_id: id,
    occurred_at: '2026-08-02T12:45:00Z',
    author: {
      account_id: 'account-test-only',
      display_name: 'Omar Operator',
    },
    text: 'Internal follow-up context.',
    ...overrides,
  }
}
