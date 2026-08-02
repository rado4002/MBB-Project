import {
  conversationLanguages,
  conversationStatuses,
  escalationStates,
  type ConversationFilters,
  type ConversationLanguage,
  type ConversationStatus,
  type EscalationState,
} from '../../api/contracts/conversations'

function included<T extends string>(values: readonly T[], value: string | null): value is T {
  return value !== null && values.includes(value as T)
}

export function readConversationFilters(params: URLSearchParams): ConversationFilters {
  const status = params.get('status')
  const escalationState = params.get('escalation_state')
  const language = params.get('language')
  return {
    ...(included(conversationStatuses, status)
      ? { status: status as ConversationStatus }
      : {}),
    ...(included(escalationStates, escalationState)
      ? { escalation_state: escalationState as EscalationState }
      : {}),
    ...(included(conversationLanguages, language)
      ? { language: language as ConversationLanguage }
      : {}),
  }
}

export function writeConversationFilters(filters: ConversationFilters): URLSearchParams {
  const params = new URLSearchParams()
  if (filters.status) params.set('status', filters.status)
  if (filters.escalation_state) {
    params.set('escalation_state', filters.escalation_state)
  }
  if (filters.language) params.set('language', filters.language)
  return params
}
