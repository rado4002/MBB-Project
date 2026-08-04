export const operatorEscalationTypes = [
  'voice_note',
  'complex_issue',
  'high_value_lead',
  'payment_issue',
] as const

export type OperatorEscalationType = (typeof operatorEscalationTypes)[number]

export const operatorEscalationPriorities = ['low', 'medium', 'high'] as const

export type OperatorEscalationPriority =
  (typeof operatorEscalationPriorities)[number]

export interface OperatorEscalationCreate {
  reason: string
  type: OperatorEscalationType
  priority: OperatorEscalationPriority
}

export interface OperatorEscalationResponse extends OperatorEscalationCreate {
  escalation_id: string
  conversation_id: string
  status: 'open'
  source: 'operator_browser'
  created_at: string
  created_by: {
    account_id: string
    display_name: string
  }
}
