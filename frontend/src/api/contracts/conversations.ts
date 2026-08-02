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
}

export interface OperatorConversationQueueResponse {
  items: OperatorConversationQueueItem[]
  next_cursor: string | null
}
