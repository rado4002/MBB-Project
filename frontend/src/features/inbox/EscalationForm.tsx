import { useId, useRef, useState, type FormEvent } from 'react'
import type { ConversationApiClient } from '../../api/conversations'
import {
  escalationPriorities,
  escalationTypes,
  type OperatorEscalationRequest,
} from '../../api/contracts/conversations'
import { asApiError, errorMessage } from '../../api/errors'
import { useAuth } from '../../auth/AuthProvider'

export function EscalationForm({ client, conversationId, available, hasOpenEscalation, onRefresh }: {
  client: ConversationApiClient
  conversationId: string
  available: boolean
  hasOpenEscalation: boolean
  onRefresh: () => Promise<void>
}) {
  const auth = useAuth()
  const id = useId()
  const [open, setOpen] = useState(false)
  const [reason, setReason] = useState('')
  const [type, setType] = useState<OperatorEscalationRequest['type']>('complex_issue')
  const [priority, setPriority] = useState<OperatorEscalationRequest['priority']>('medium')
  const [busy, setBusy] = useState(false)
  const busyRef = useRef(false)
  const [attempt, setAttempt] = useState<{ key: string; body: OperatorEscalationRequest } | null>(null)
  const [message, setMessage] = useState('')
  const [finished, setFinished] = useState(false)
  const canCreate = Boolean(auth.session?.capabilities.includes('escalation.create'))

  async function refresh() {
    try {
      await onRefresh()
    } catch {
      setMessage((current) => `${current} Conversation refresh failed. Refresh details before continuing.`)
    }
  }

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (busyRef.current || finished || !canCreate || (!attempt && (!available || hasOpenEscalation))) return
    const body = { reason: reason.trim(), type, priority }
    if (!attempt && (Array.from(body.reason).length < 10 || Array.from(body.reason).length > 500)) {
      setMessage('Enter a reason containing 10–500 trimmed characters.')
      return
    }
    busyRef.current = true
    setBusy(true)
    setMessage('')
    try {
      const submission = attempt ?? { key: crypto.randomUUID(), body }
      setAttempt(submission)
      const csrfToken = await auth.getCsrfForMutation()
      await client.createEscalation(conversationId, submission.body, submission.key, csrfToken)
      setFinished(true)
      setMessage('Escalation created.')
      await refresh()
    } catch (unknownError) {
      const error = asApiError(unknownError)
      if (error.code === 'ESCALATION_ALREADY_OPEN') {
        setFinished(true)
        setMessage('An escalation is already open for this conversation.')
        await refresh()
      } else if (error.code === 'IDEMPOTENCY_CONFLICT') {
        setFinished(true)
        setMessage('This submission key conflicts with another request. Refresh and review the conversation before starting again.')
        await refresh()
      } else if (error.code === 'IDEMPOTENCY_IN_PROGRESS') {
        setMessage('This submission is still processing. Wait, then retry the same submission.')
      } else if (error.category === 'validation') {
        setAttempt(null)
        setMessage('Check the reason, type and priority, then submit again.')
      } else if (error.category === 'unavailable') {
        setMessage('The submission outcome is uncertain. Retry the same submission to check or complete it.')
      } else {
        setMessage(errorMessage(error))
      }
    } finally {
      busyRef.current = false
      setBusy(false)
    }
  }

  if (!canCreate) return null
  return (
    <section className="escalation-form" aria-label="Escalation creation">
      <button className="button button--secondary" type="button" aria-expanded={open}
        aria-controls={id} disabled={!open && !attempt && (!available || hasOpenEscalation)}
        onClick={() => setOpen(!open)}>
        {open ? 'Hide escalation form' : 'Create escalation'}
      </button>
      {open ? (
        <form id={id} onSubmit={(event) => void submit(event)}>
          <p>Create a ticket for follow-up. Conversation control stays unchanged.</p>
          <fieldset disabled={busy || Boolean(attempt) || finished}>
            <legend>Escalation details</legend>
            <label htmlFor={`${id}-reason`}>Escalation reason</label>
            <textarea id={`${id}-reason`} value={reason} rows={3}
              aria-describedby={`${id}-help`} onChange={(event) => setReason(event.target.value)} />
            <p id={`${id}-help`}>10–500 characters after trimming spaces.</p>
            <label htmlFor={`${id}-type`}>Escalation type</label>
            <select id={`${id}-type`} value={type}
              onChange={(event) => setType(event.target.value as OperatorEscalationRequest['type'])}>
              {escalationTypes.map((value) => <option key={value} value={value}>{value.replaceAll('_', ' ')}</option>)}
            </select>
            <label htmlFor={`${id}-priority`}>Escalation priority</label>
            <select id={`${id}-priority`} value={priority}
              onChange={(event) => setPriority(event.target.value as OperatorEscalationRequest['priority'])}>
              {escalationPriorities.map((value) => <option key={value} value={value}>{value}</option>)}
            </select>
          </fieldset>
          <p role="status">{message}</p>
          <button className="button button--primary" type="submit"
            disabled={busy || finished || (!attempt && (!available || hasOpenEscalation))}>
            {busy ? 'Submitting escalation…' : attempt ? 'Retry same submission' : 'Submit escalation'}
          </button>
        </form>
      ) : null}
    </section>
  )
}
