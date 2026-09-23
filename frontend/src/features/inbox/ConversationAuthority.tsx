import type { RefObject } from 'react'
import type { OperatorConversationDetail } from '../../api/contracts/conversations'

function authorityLabel(detail: OperatorConversationDetail, accountId: string | undefined) {
  const ownership = detail.ownership
  if (ownership.owner_type === 'ai') {
    return 'MBB AI Assistant'
  }
  if (!ownership.human_owner) return 'Human ownership unavailable'
  return ownership.human_owner.account_id === accountId
    ? `You — ${ownership.human_owner.display_name}`
    : ownership.human_owner.display_name
}

function ownershipUnavailableReason(
  detail: OperatorConversationDetail,
  hasOwnershipCapability: boolean,
  mayReturnOwnedConversation: boolean,
) {
  if (!hasOwnershipCapability) {
    return 'Ownership action unavailable — your account does not have permission to change authority.'
  }
  if (detail.ownership.owner_type === 'human' && !mayReturnOwnedConversation) {
    return 'Ownership action unavailable — only the current owner or an administrator can return this conversation to AI.'
  }
  return null
}

export function ConversationAuthority({
  detail,
  accountId,
  hasOwnershipCapability,
  mayReturnOwnedConversation,
  canChangeOwnership,
  ownershipOpen,
  notice,
  authorityRef,
  actionRef,
  onAction,
}: {
  detail: OperatorConversationDetail
  accountId: string | undefined
  hasOwnershipCapability: boolean
  mayReturnOwnedConversation: boolean
  canChangeOwnership: boolean
  ownershipOpen: boolean
  notice: string
  authorityRef: RefObject<HTMLDivElement | null>
  actionRef: RefObject<HTMLButtonElement | null>
  onAction: () => void
}) {
  const ownership = detail.ownership
  const unavailableAction = ownershipUnavailableReason(
    detail,
    hasOwnershipCapability,
    mayReturnOwnedConversation,
  )
  const actionLabel = ownership.owner_type === 'ai'
    ? 'Take over conversation'
    : 'Return to AI'

  return (
    <div
      className="conversation-authority"
      ref={authorityRef}
      tabIndex={-1}
      role="group"
      aria-label="Conversation authority"
    >
      <span className={`conversation-authority__status conversation-authority__status--${ownership.owner_type}`}>
        <span className="conversation-authority__dot" aria-hidden="true" />
        <strong>{authorityLabel(detail, accountId)}</strong>
        <span aria-hidden="true">·</span>
        <span>{ownership.owner_type === 'ai'
          ? ownership.ai_execution_state === 'paused' ? 'Paused' : 'Handling'
          : 'Human control'}</span>
      </span>
      {canChangeOwnership ? (
        <button
          className="button button--primary conversation-authority__action"
          type="button"
          aria-haspopup="dialog"
          aria-expanded={ownershipOpen}
          onClick={onAction}
          ref={actionRef}
        >
          {actionLabel}
        </button>
      ) : unavailableAction ? (
        <p className="conversation-authority__restriction">{unavailableAction}</p>
      ) : null}
      {notice ? (
        <p className="conversation-authority__notice" role="status" aria-live="polite">
          {notice}
        </p>
      ) : null}
    </div>
  )
}
