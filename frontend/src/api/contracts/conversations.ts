export const conversationStatuses = [
  'active',
  'qualifying',
  'nurturing',
  'escalated',
  'converted',
  'dormant',
] as const

export const conversationLanguages = ['lingala', 'french', 'swahili'] as const
export const escalationStates = ['open', 'none'] as const

export type ConversationStatus = (typeof conversationStatuses)[number]
export type ConversationLanguage = (typeof conversationLanguages)[number]
export type EscalationState = (typeof escalationStates)[number]
export type MessageContentType = 'text' | 'voice_note' | 'image'
export type MessageDirection = 'inbound' | 'outbound'
export type MessageDeliveryState = 'accepted' | 'sent' | 'failed' | 'uncertain'

export interface ConversationFilters {
  status?: ConversationStatus
  escalation_state?: EscalationState
  language?: ConversationLanguage
}

export interface OperatorConversationQueueItem {
  conversation_id: string
  customer: {
    display_name: string | null
    phone_masked: string
  }
  language: ConversationLanguage
  status: ConversationStatus
  message_count: number
  latest_message: {
    preview: string
    content_type: MessageContentType
    direction: MessageDirection
    occurred_at: string
  } | null
  awaiting_response_since: string | null
  open_escalation: {
    exists: boolean
  }
  ownership: ConversationOwnership
}

export interface OperatorConversationQueueResponse {
  items: OperatorConversationQueueItem[]
  next_cursor: string | null
}

export interface OperatorLeadSummary {
  score: string | null
  stage: string | null
  intent: string | null
  product_interests: string[]
}

export interface OperatorConversationDetail {
  conversation_id: string
  status: ConversationStatus
  language: ConversationLanguage
  message_count: number
  updated_at: string
  customer: {
    display_name: string | null
    phone_masked: string
  }
  lead: OperatorLeadSummary | null
  open_escalation: {
    exists: boolean
  }
  ownership: ConversationOwnership
}

export interface ConversationOwnership {
  owner_type: 'ai' | 'human'
  human_owner: {
    account_id: string
    display_name: string
  } | null
  ai_execution_state: 'eligible' | 'paused'
  version: number
  updated_at: string
}

export interface OwnershipTransitionRequest {
  target_owner_type: 'ai' | 'human'
  expected_version: number
}

export interface OwnershipTransitionResponse {
  conversation_id: string
  ownership: ConversationOwnership
}

export type MessageSenderType = 'customer' | 'operator' | 'system' | 'unknown'

export interface OperatorMessageItem {
  message_id: string
  occurred_at: string
  direction: MessageDirection
  sender_type: MessageSenderType
  operator_author: {
    account_id: string
    display_name: string
  } | null
  delivery_state: MessageDeliveryState | null
  delivery_state_timestamp: string | null
  content_type: MessageContentType
  text: string | null
  media: {
    kind: 'voice_note' | 'image'
    available: false
  } | null
  language: ConversationLanguage
}

export interface OperatorReplyRequest {
  text: string
  expected_ownership_version: number
}

export interface OperatorInternalNoteRequest {
  text: string
}

export interface OperatorInternalNoteItem {
  kind: 'internal_note'
  note_id: string
  occurred_at: string
  author: {
    account_id: string
    display_name: string
  }
  text: string
}

export interface OperatorTimelineMessageItem extends OperatorMessageItem {
  kind: 'message'
}

export type OperatorTimelineItem = OperatorTimelineMessageItem | OperatorInternalNoteItem

export interface OperatorMessageHistoryResponse {
  items: OperatorMessageItem[]
  next_older_cursor: string | null
}

export interface OperatorTimelineResponse {
  items: OperatorTimelineItem[]
  next_older_cursor: string | null
}
