import { requestJson } from './client'
import type {
  ConversationFilters,
  OperatorConversationQueueResponse,
} from './contracts/conversations'

export interface ConversationQueueRequest {
  filters: ConversationFilters
  cursor?: string
  signal?: AbortSignal
}

export interface ConversationApiClient {
  listConversations(request: ConversationQueueRequest): Promise<OperatorConversationQueueResponse>
}

export function createConversationApiClient(
  onSessionExpired: () => void = () => undefined,
): ConversationApiClient {
  return {
    listConversations: ({ filters, cursor, signal }) => {
      const query = new URLSearchParams()
      if (filters.status) query.set('status', filters.status)
      if (filters.escalation_state) {
        query.set('escalation_state', filters.escalation_state)
      }
      if (filters.language) query.set('language', filters.language)
      if (cursor) query.set('cursor', cursor)
      const suffix = query.size ? `?${query.toString()}` : ''
      return requestJson<OperatorConversationQueueResponse>(
        `/api/v1/operator/conversations${suffix}`,
        { signal },
        onSessionExpired,
      )
    },
  }
}
