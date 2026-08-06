import { requestJson } from './client'
import type {
  ConversationFilters,
  OperatorConversationDetail,
  OperatorConversationQueueResponse,
  OperatorMessageHistoryResponse,
  OperatorMessageItem,
  OperatorInternalNoteItem,
  OperatorInternalNoteRequest,
  OperatorReplyRequest,
  OperatorTimelineResponse,
  OwnershipTransitionRequest,
  OwnershipTransitionResponse,
} from './contracts/conversations'

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
  getTimeline(
    conversationId: string,
    before?: string,
    signal?: AbortSignal,
  ): Promise<OperatorTimelineResponse>
  changeOwnership(
    conversationId: string,
    body: OwnershipTransitionRequest,
    idempotencyKey: string,
    csrfToken: string,
    signal?: AbortSignal,
  ): Promise<OwnershipTransitionResponse>
  createReply(
    conversationId: string,
    body: OperatorReplyRequest,
    idempotencyKey: string,
    csrfToken: string,
    signal?: AbortSignal,
  ): Promise<OperatorMessageItem>
  createInternalNote(
    conversationId: string,
    body: OperatorInternalNoteRequest,
    idempotencyKey: string,
    csrfToken: string,
    signal?: AbortSignal,
  ): Promise<OperatorInternalNoteItem>
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
    getTimeline: (conversationId, before, signal) => {
      const query = new URLSearchParams()
      if (before) query.set('before', before)
      const suffix = query.size ? `?${query.toString()}` : ''
      return requestJson<OperatorTimelineResponse>(
        `${conversationPath(conversationId)}/timeline${suffix}`,
        { signal },
        onSessionExpired,
      )
    },
    changeOwnership: (
      conversationId,
      body,
      idempotencyKey,
      csrfToken,
      signal,
    ) =>
      requestJson<OwnershipTransitionResponse>(
        `${conversationPath(conversationId)}/ownership`,
        {
          method: 'POST',
          body,
          csrfToken,
          idempotencyKey,
          signal,
        },
        onSessionExpired,
      ),
    createReply: (
      conversationId,
      body,
      idempotencyKey,
      csrfToken,
      signal,
    ) =>
      requestJson<OperatorMessageItem>(
        `${conversationPath(conversationId)}/replies`,
        {
          method: 'POST',
          body,
          csrfToken,
          idempotencyKey,
          signal,
        },
        onSessionExpired,
      ),
    createInternalNote: (
      conversationId,
      body,
      idempotencyKey,
      csrfToken,
      signal,
    ) =>
      requestJson<OperatorInternalNoteItem>(
        `${conversationPath(conversationId)}/notes`,
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
