import { requestJson } from './client'
import type {
  ConversationFilters,
  OperatorConversationDetail,
  OperatorConversationQueueResponse,
  OperatorMessageHistoryResponse,
} from './contracts/conversations'
import type {
  OperatorEscalationCreate,
  OperatorEscalationResponse,
} from './contracts/escalations'

export interface ConversationQueueRequest {
  filters: ConversationFilters
  cursor?: string
  signal?: AbortSignal
}

export interface ConversationApiClient {
  listConversations(request: ConversationQueueRequest): Promise<OperatorConversationQueueResponse>
  getConversation(conversationId: string, signal?: AbortSignal): Promise<OperatorConversationDetail>
  getMessages(
    conversationId: string,
    before?: string,
    signal?: AbortSignal,
  ): Promise<OperatorMessageHistoryResponse>
  createEscalation(
    conversationId: string,
    body: OperatorEscalationCreate,
    idempotencyKey: string,
    csrfToken: string,
    signal?: AbortSignal,
  ): Promise<OperatorEscalationResponse>
}

export function createConversationApiClient(
  onSessionExpired: () => void = () => undefined,
): ConversationApiClient {
  const conversationPath = (conversationId: string) =>
    `/api/v1/operator/conversations/${encodeURIComponent(conversationId)}`

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
    getConversation: (conversationId, signal) =>
      requestJson<OperatorConversationDetail>(
        conversationPath(conversationId),
        { signal },
        onSessionExpired,
      ),
    getMessages: (conversationId, before, signal) => {
      const query = new URLSearchParams()
      if (before) query.set('before', before)
      const suffix = query.size ? `?${query.toString()}` : ''
      return requestJson<OperatorMessageHistoryResponse>(
        `${conversationPath(conversationId)}/messages${suffix}`,
        { signal },
        onSessionExpired,
      )
    },
    createEscalation: (
      conversationId,
      body,
      idempotencyKey,
      csrfToken,
      signal,
    ) =>
      requestJson<OperatorEscalationResponse>(
        `${conversationPath(conversationId)}/escalations`,
        {
          method: 'POST',
          body,
          csrfToken,
          idempotencyKey,
          signal,
        },
        onSessionExpired,
      ),
  }
}
