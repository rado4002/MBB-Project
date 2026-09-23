import type { OperatorConversationDetail } from '../../api/contracts/conversations'

const REPLY_ELIGIBLE_STATUSES = new Set([
  'active',
  'qualifying',
  'nurturing',
  'escalated',
])

export function replyUnavailableReason(
  detail: OperatorConversationDetail | null,
  accountId: string | undefined,
  hasReplyCapability: boolean,
) {
  if (!hasReplyCapability) {
    return 'Reply unavailable — your account does not have permission to reply.'
  }
  if (!detail) {
    return 'Reply unavailable — conversation ownership is unavailable.'
  }
  if (detail.ownership.owner_type === 'ai') {
    if (detail.ownership.ai_execution_state === 'paused') {
      return 'Reply unavailable — waiting for a Human Operator to take over.'
    }
    return 'Reply unavailable — this conversation is controlled by MBB AI Assistant.'
  }
  if (
    detail.ownership.ai_execution_state !== 'paused' ||
    !detail.ownership.human_owner
  ) {
    return 'Reply unavailable — conversation ownership is unavailable.'
  }
  if (detail.ownership.human_owner.account_id !== accountId) {
    return `Reply unavailable — only ${detail.ownership.human_owner.display_name} may reply.`
  }
  if (!REPLY_ELIGIBLE_STATUSES.has(detail.status)) {
    return 'Reply unavailable — this conversation is not currently eligible for replies.'
  }
  return null
}
