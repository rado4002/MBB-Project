import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import {
  createBusinessOrdersClient,
  type OrderCancellationReason,
  type OrderDeliveryIssueReason,
  type OrderDeliveryIssueResolution,
  type OrderDeliveryIssueSource,
  type OrderDeliveryIssueType,
  type OrderDetail,
  type OrderQueueItem,
  type OrderStatus,
} from '../../api/businessOrders'
import { asApiError, errorMessage } from '../../api/errors'
import { useAuth } from '../../auth/AuthProvider'
import { PasswordField } from '../../components/PasswordField'

const statuses: OrderStatus[] = ['pending', 'confirmed', 'preparing', 'delivering', 'delivered', 'cancelled']
const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
const cancellationReasons: { value: OrderCancellationReason; label: string }[] = [
  { value: 'customer_requested', label: 'Customer requested cancellation' },
  { value: 'duplicate_order', label: 'Duplicate Order' },
  { value: 'commercial_terms_unavailable', label: 'Accepted commercial terms unavailable' },
  { value: 'other_operational', label: 'Other operational reason' },
]
const issueReasons: Record<OrderDeliveryIssueType, { value: OrderDeliveryIssueReason; label: string }[]> = {
  failed: [
    { value: 'recipient_unavailable', label: 'Recipient unavailable' },
    { value: 'recipient_refused', label: 'Recipient refused delivery' },
    { value: 'address_problem', label: 'Address problem' },
    { value: 'courier_problem', label: 'Courier problem' },
    { value: 'other', label: 'Other documented failure' },
  ],
  disputed: [
    { value: 'receipt_disputed', label: 'Receipt disputed' },
    { value: 'condition_disputed', label: 'Delivered condition disputed' },
    { value: 'item_disputed', label: 'Delivered item disputed' },
    { value: 'other', label: 'Other documented dispute' },
  ],
}
const issueSources: { value: OrderDeliveryIssueSource; label: string }[] = [
  { value: 'customer_message', label: 'Customer message' },
  { value: 'customer_call', label: 'Customer call' },
  { value: 'courier_report', label: 'Courier report' },
  { value: 'staff_observation', label: 'Staff observation' },
  { value: 'other', label: 'Other identified source' },
]

function formatAmount(value: string, currency: string) {
  const number = Number(value)
  return Number.isFinite(number)
    ? `${currency} ${new Intl.NumberFormat(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(number)}`
    : `${currency} ${value}`
}

function formatDate(value: string) {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? 'Time unavailable' : date.toLocaleString()
}

function humanize(value: string) {
  const words = value.replaceAll('_', ' ')
  return words.charAt(0).toUpperCase() + words.slice(1)
}

function queuePayment(state: OrderQueueItem['readiness']['payment_state'] | OrderDetail['preparation']['payment_state']) {
  if (state === 'verified') return 'Verified'
  if (state === 'missing') return 'Not recorded'
  if (state === 'not_verified') return 'Not verified'
  if (state === 'mismatch') return 'Mismatch'
  return 'Unavailable'
}

function queueStock(item: OrderQueueItem) {
  if (item.readiness.stock_state === 'consumed') return 'Dispatched'
  if (item.readiness.stock_state === 'full_unconsumed') {
    return item.readiness.inventory_state === 'ready' ? 'Allocated · ready' : 'Allocated · conflict'
  }
  if (item.readiness.stock_state === 'missing') return 'Not allocated'
  if (item.readiness.stock_state === 'mismatch') return 'Allocation mismatch'
  return 'Unsupported'
}

function queueDelivery(item: OrderQueueItem) {
  if (item.readiness.delivery_state === 'delivered') return 'Delivered'
  if (item.readiness.delivery_state === 'delivering') {
    return item.readiness.dispatch_state === 'matched' ? 'In delivery' : 'Dispatch evidence missing'
  }
  return 'Not started'
}

function detailStock(detail: OrderDetail) {
  if (detail.dispatch.allocation_state === 'consumed') return 'Dispatched'
  if (detail.dispatch.allocation_state === 'full_unconsumed') {
    return detail.dispatch.inventory_state === 'ready' ? 'Allocated · ready' : 'Allocated · conflict'
  }
  if (detail.dispatch.allocation_state === 'missing') return 'Not allocated'
  if (detail.dispatch.allocation_state === 'mismatch') return 'Allocation mismatch'
  return 'Unsupported'
}

function detailDelivery(detail: OrderDetail) {
  if (detail.delivery.order_state === 'delivered') return 'Delivered'
  if (detail.delivery.order_state === 'delivering') {
    return detail.delivery.dispatch_state === 'matched' ? 'In delivery' : 'Dispatch evidence missing'
  }
  return 'Not started'
}

function readError(failure: unknown) {
  const error = asApiError(failure)
  if (error.category === 'unavailable') return 'Orders are temporarily unavailable. Please try again.'
  return errorMessage(error)
}

function paymentReadiness(state: OrderDetail['preparation']['payment_state']) {
  if (state === 'verified') return 'Verified completed Payment matches the accepted Order'
  if (state === 'missing') return 'No Payment recorded'
  if (state === 'not_verified') return 'Payment is not verified as completed'
  if (state === 'mismatch') return 'Completed Payment does not match the accepted amount or currency'
  return 'Unavailable for this Order shape'
}

function allocationReadiness(preparation: OrderDetail['preparation']) {
  if (preparation.allocation_state === 'full') {
    return `Full allocation · ${preparation.allocated_quantity} of ${preparation.required_quantity}`
  }
  if (preparation.allocation_state === 'missing') return 'No durable Stock allocation recorded'
  if (preparation.allocation_state === 'mismatch') {
    return `Allocation does not match · ${preparation.allocated_quantity ?? 0} of ${preparation.required_quantity}`
  }
  return 'Unsupported Order line structure'
}

function dispatchStockReadiness(dispatch: OrderDetail['dispatch']) {
  if (dispatch.allocation_state === 'consumed') return 'Allocation consumed at dispatch'
  if (dispatch.allocation_state === 'full_unconsumed' && dispatch.inventory_state === 'ready') {
    return `Full allocation ready · quantity ${dispatch.required_quantity}`
  }
  if (dispatch.allocation_state === 'missing') return 'No durable Stock allocation recorded'
  if (dispatch.allocation_state === 'unsupported') return 'Unsupported Order line structure'
  return 'Allocated or physical Stock does not match this Order'
}

function deliveryReadiness(delivery: OrderDetail['delivery']) {
  if (delivery.order_state === 'delivered') {
    return 'Customer-reported receipt has already been recorded'
  }
  if (delivery.order_state !== 'delivering') return 'Dispatch the Order before confirming customer receipt'
  if (delivery.delivery_method_state !== 'supported') return 'Only moto taxi Orders use this confirmation workflow'
  if (delivery.line_state !== 'single') return 'Only single-line Orders are supported'
  if (delivery.dispatch_state !== 'matched') return 'Matching immutable dispatch evidence is missing'
  if (delivery.issue_state === 'open') return 'Resolve the open delivery issue before confirming customer receipt'
  if (delivery.association_state === 'missing') return 'The verified customer conversation is unavailable'
  if (delivery.association_state === 'ambiguous') return 'More than one delivering Order is associated with this conversation'
  if (delivery.evidence_state === 'missing') return 'No eligible post-dispatch inbound customer text has been received'
  return 'Customer receipt evidence is ready for Administrator review'
}

function currentOperationalState(detail: OrderDetail) {
  if (detail.delivery.issue_state === 'open') return 'Open delivery issue requires review'
  if (detail.status === 'cancelled') return 'Order cancelled'
  if (detail.status === 'delivered') return 'Customer-reported receipt recorded'
  if (detail.status === 'delivering') return deliveryReadiness(detail.delivery)
  if (detail.status === 'preparing') {
    if (detail.dispatch.payment_state !== 'verified') return paymentReadiness(detail.dispatch.payment_state)
    if (detail.dispatch.allocation_state !== 'full_unconsumed' || detail.dispatch.inventory_state !== 'ready') {
      return dispatchStockReadiness(detail.dispatch)
    }
    if (detail.dispatch.delivery_method_state !== 'supported') {
      return 'Only moto taxi Orders can use this dispatch workflow'
    }
    return detail.dispatch.eligible ? 'Ready for authorized dispatch' : 'Dispatch is not eligible'
  }
  if (detail.status === 'confirmed') {
    if (detail.preparation.payment_state !== 'verified') return paymentReadiness(detail.preparation.payment_state)
    return detail.preparation.eligible ? 'Ready for authorized preparation' : allocationReadiness(detail.preparation)
  }
  return paymentReadiness(detail.preparation.payment_state)
}

export function OrdersPage() {
  const auth = useAuth()
  const { orderId } = useParams()
  const [params, setParams] = useSearchParams()
  const requestedStatus = params.get('status')
  const status: OrderStatus | null = requestedStatus === 'all' ? null
    : statuses.includes(requestedStatus as OrderStatus) ? requestedStatus as OrderStatus : 'pending'
  const rawOffset = Number(params.get('offset') ?? '0')
  const offset = Number.isInteger(rawOffset) && rawOffset >= 0 && rawOffset <= 10000 ? rawOffset : 0
  const client = useMemo(() => createBusinessOrdersClient(auth.handleSessionExpired), [auth.handleSessionExpired])
  const [list, setList] = useState<{ items: OrderQueueItem[]; has_more: boolean } | null>(null)
  const [listError, setListError] = useState<string | null>(null)
  const [detail, setDetail] = useState<OrderDetail | null>(null)
  const [detailError, setDetailError] = useState<string | null>(null)
  const [cancelOpen, setCancelOpen] = useState(false)
  const [cancelReason, setCancelReason] = useState<OrderCancellationReason | ''>('')
  const [cancelBusy, setCancelBusy] = useState(false)
  const [cancelError, setCancelError] = useState<string | null>(null)
  const [cancelNotice, setCancelNotice] = useState<string | null>(null)
  const [prepareOpen, setPrepareOpen] = useState(false)
  const [prepareBusy, setPrepareBusy] = useState(false)
  const [prepareError, setPrepareError] = useState<string | null>(null)
  const [prepareNotice, setPrepareNotice] = useState<string | null>(null)
  const [dispatchOpen, setDispatchOpen] = useState(false)
  const [dispatchBusy, setDispatchBusy] = useState(false)
  const [dispatchError, setDispatchError] = useState<string | null>(null)
  const [dispatchNotice, setDispatchNotice] = useState<string | null>(null)
  const [handoffReference, setHandoffReference] = useState('')
  const [dispatchAttested, setDispatchAttested] = useState(false)
  const [dispatchReauth, setDispatchReauth] = useState(false)
  const [dispatchPassword, setDispatchPassword] = useState('')
  const [deliveryOpen, setDeliveryOpen] = useState(false)
  const [deliveryBusy, setDeliveryBusy] = useState(false)
  const [deliveryError, setDeliveryError] = useState<string | null>(null)
  const [deliveryNotice, setDeliveryNotice] = useState<string | null>(null)
  const [selectedEvidence, setSelectedEvidence] = useState('')
  const [deliveryAttested, setDeliveryAttested] = useState(false)
  const [deliveryReauth, setDeliveryReauth] = useState(false)
  const [deliveryPassword, setDeliveryPassword] = useState('')
  const [issueOpen, setIssueOpen] = useState(false)
  const [issueType, setIssueType] = useState<OrderDeliveryIssueType>('failed')
  const [issueReason, setIssueReason] = useState<OrderDeliveryIssueReason>('recipient_unavailable')
  const [issueSource, setIssueSource] = useState<OrderDeliveryIssueSource>('courier_report')
  const [issueSummary, setIssueSummary] = useState('')
  const [issueEvidence, setIssueEvidence] = useState('')
  const [issueBusy, setIssueBusy] = useState(false)
  const [issueError, setIssueError] = useState<string | null>(null)
  const [issueNotice, setIssueNotice] = useState<string | null>(null)
  const [closingIssueId, setClosingIssueId] = useState<string | null>(null)
  const [resolutionCode, setResolutionCode] = useState<OrderDeliveryIssueResolution>('false_alarm')
  const [resolutionReason, setResolutionReason] = useState('')
  const [issueCloseReauth, setIssueCloseReauth] = useState(false)
  const [issueClosePassword, setIssueClosePassword] = useState('')
  const [reauth, setReauth] = useState(false)
  const [password, setPassword] = useState('')
  const attempt = useRef<{ signature: string; key: string } | null>(null)
  const cancellation = useRef<AbortController | null>(null)
  const prepareAttempt = useRef<{ signature: string; key: string } | null>(null)
  const preparation = useRef<AbortController | null>(null)
  const dispatchAttempt = useRef<{ signature: string; key: string } | null>(null)
  const dispatchRequest = useRef<AbortController | null>(null)
  const deliveryAttempt = useRef<{ signature: string; key: string } | null>(null)
  const deliveryRequest = useRef<AbortController | null>(null)
  const issueAttempt = useRef<{ signature: string; key: string } | null>(null)
  const issueCloseAttempt = useRef<{ signature: string; key: string } | null>(null)
  const issueRequest = useRef<AbortController | null>(null)
  const detailHeading = useRef<HTMLHeadingElement | null>(null)
  const lastSelectedOrder = useRef<string | null>(null)
  const canCancel = auth.session?.capabilities.includes('order.cancel') ?? false
  const canPrepare = auth.session?.capabilities.includes('order.prepare') ?? false
  const canDispatch = auth.session?.capabilities.includes('order.dispatch') ?? false
  const canConfirmDelivery = auth.session?.capabilities.includes('order.delivery.confirm') ?? false
  const canReportIssue = auth.session?.capabilities.includes('order.delivery_issue.create') ?? false
  const canCloseIssue = auth.session?.capabilities.includes('order.delivery_issue.close') ?? false

  const loadList = useCallback(async (signal: AbortSignal) => {
    setList(null); setListError(null)
    try {
      const result = await client.list(status, offset, signal)
      if (!signal.aborted) setList(result)
    } catch (failure) {
      if (!signal.aborted) setListError(readError(failure))
    }
  }, [client, status, offset])

  const loadDetail = useCallback(async (signal: AbortSignal) => {
    setDetail(null); setDetailError(null)
    cancellation.current?.abort(); attempt.current = null
    preparation.current?.abort(); prepareAttempt.current = null
    dispatchRequest.current?.abort(); dispatchAttempt.current = null
    deliveryRequest.current?.abort(); deliveryAttempt.current = null
    issueRequest.current?.abort(); issueAttempt.current = null; issueCloseAttempt.current = null
    setCancelOpen(false); setCancelReason(''); setCancelError(null); setCancelNotice(null)
    setReauth(false); setPassword(''); setCancelBusy(false)
    setPrepareOpen(false); setPrepareBusy(false); setPrepareError(null); setPrepareNotice(null)
    setDispatchOpen(false); setDispatchBusy(false); setDispatchError(null); setDispatchNotice(null)
    setHandoffReference(''); setDispatchAttested(false); setDispatchReauth(false); setDispatchPassword('')
    setDeliveryOpen(false); setDeliveryBusy(false); setDeliveryError(null); setDeliveryNotice(null)
    setSelectedEvidence(''); setDeliveryAttested(false); setDeliveryReauth(false); setDeliveryPassword('')
    setIssueOpen(false); setIssueType('failed'); setIssueReason('recipient_unavailable')
    setIssueSource('courier_report'); setIssueSummary(''); setIssueEvidence('')
    setIssueBusy(false); setIssueError(null); setIssueNotice(null); setClosingIssueId(null)
    setResolutionCode('false_alarm'); setResolutionReason(''); setIssueCloseReauth(false); setIssueClosePassword('')
    if (!orderId) return
    try {
      const result = await client.detail(orderId, signal)
      if (!signal.aborted) setDetail(result)
    } catch (failure) {
      if (!signal.aborted) setDetailError(readError(failure))
    }
  }, [client, orderId])

  useEffect(() => {
    const controller = new AbortController()
    // The request transition clears the prior page before loading a new one.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void loadList(controller.signal)
    return () => controller.abort()
  }, [loadList])
  useEffect(() => {
    const controller = new AbortController()
    // The selected Order is route-scoped; never show a previous Order's snapshot.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void loadDetail(controller.signal)
    return () => controller.abort()
  }, [loadDetail])
  useEffect(() => {
    if (orderId && detail) detailHeading.current?.focus()
    if (!orderId && list && lastSelectedOrder.current) {
      document.querySelector<HTMLElement>(`[data-order-id="${lastSelectedOrder.current}"]`)?.focus()
    }
  }, [detail, list, orderId])
  const submitCancellation = async (event: FormEvent) => {
    event.preventDefault()
    if (!detail || !canCancel || detail.status !== 'pending' || !cancelReason || cancelBusy) return
    const expiry = auth.session?.recent_reauthentication_expires_at_epoch
    if (expiry == null || expiry < Math.floor(Date.now() / 1000)) {
      setReauth(true); return
    }
    const signature = `${detail.order_id}:${detail.updated_at}:${cancelReason}`
    if (!attempt.current || attempt.current.signature !== signature) {
      attempt.current = { signature, key: crypto.randomUUID() }
    }
    const controller = new AbortController()
    cancellation.current?.abort(); cancellation.current = controller
    setCancelBusy(true); setCancelError(null); setCancelNotice(null)
    try {
      const csrf = await auth.getCsrfForMutation()
      if (controller.signal.aborted) return
      const result = await client.cancel(
        detail.order_id, cancelReason, detail.updated_at,
        attempt.current.key, csrf, controller.signal,
      )
      if (controller.signal.aborted) return
      setDetail({ ...detail, status: result.status, updated_at: result.updated_at })
      setList((current) => current ? {
        ...current,
        items: current.items.map((item) => item.order_id === result.order_id
          ? { ...item, status: result.status, updated_at: result.updated_at }
          : item),
      } : current)
      setCancelOpen(false); setCancelReason(''); setReauth(false); setPassword('')
      setCancelNotice(result.replayed
        ? 'Cancellation was already recorded. Showing the current Order.'
        : 'Order cancelled.')
      attempt.current = null
    } catch (failure) {
      if (controller.signal.aborted) return
      const apiError = asApiError(failure)
      if (apiError.code === 'recent_reauthentication_required') {
        setReauth(true)
      } else if (apiError.code === 'ORDER_STALE' || apiError.code === 'ORDER_STATE_CONFLICT') {
        attempt.current = null
        setCancelOpen(false)
        setCancelError('The Order changed since review. Refresh it before another action.')
        void loadList(new AbortController().signal)
        void loadDetail(new AbortController().signal)
      } else if (apiError.code === 'ORDER_PAYMENT_CONFLICT') {
        attempt.current = null
        setCancelOpen(false)
        setCancelError('This Order has Payment state and cannot be cancelled here.')
      } else if (apiError.status === 0 || apiError.status >= 500) {
        setCancelError('The result could not be confirmed. Retry the same cancellation request.')
      } else {
        attempt.current = null
        setCancelError(errorMessage(apiError))
      }
    } finally {
      if (!controller.signal.aborted) setCancelBusy(false)
    }
  }

  const confirmPassword = async (event: FormEvent) => {
    event.preventDefault(); setCancelBusy(true); setCancelError(null)
    try {
      await auth.reauthenticate(password)
      setReauth(false)
      setCancelNotice('Password confirmed. Review the reason and confirm cancellation.')
    } catch (failure) {
      setCancelError(errorMessage(asApiError(failure)))
    } finally {
      setPassword(''); setCancelBusy(false)
    }
  }

  const submitPreparation = async (event: FormEvent) => {
    event.preventDefault()
    if (!detail || !canPrepare || !detail.preparation.eligible || prepareBusy) return
    const signature = `${detail.order_id}:${detail.updated_at}`
    if (!prepareAttempt.current || prepareAttempt.current.signature !== signature) {
      prepareAttempt.current = { signature, key: crypto.randomUUID() }
    }
    const controller = new AbortController()
    preparation.current?.abort(); preparation.current = controller
    setPrepareBusy(true); setPrepareError(null); setPrepareNotice(null)
    try {
      const csrf = await auth.getCsrfForMutation()
      if (controller.signal.aborted) return
      const result = await client.prepare(
        detail.order_id, detail.updated_at, prepareAttempt.current.key, csrf, controller.signal,
      )
      if (controller.signal.aborted) return
      setDetail({
        ...detail,
        status: result.status,
        updated_at: result.updated_at,
        readiness: {
          ...detail.readiness,
          stock_state: 'consumed',
          inventory_state: 'conflict',
          dispatch_state: 'matched',
          delivery_state: 'delivering',
        },
        preparation: { ...detail.preparation, eligible: false, order_state: 'not_confirmed' },
        dispatch: {
          ...detail.dispatch,
          eligible: detail.dispatch.payment_state === 'verified'
            && detail.dispatch.allocation_state === 'full_unconsumed'
            && detail.dispatch.inventory_state === 'ready'
            && detail.dispatch.delivery_method_state === 'supported',
          order_state: 'preparing',
        },
      })
      setList((current) => current ? {
        ...current,
        items: current.items.map((item) => item.order_id === result.order_id
          ? { ...item, status: result.status, updated_at: result.updated_at, readiness: {
            ...item.readiness,
            stock_state: 'consumed', inventory_state: 'conflict',
            dispatch_state: 'matched', delivery_state: 'delivering',
          } }
          : item),
      } : current)
      setPrepareOpen(false)
      setPrepareNotice(result.replayed
        ? 'Preparation was already recorded. Showing the current Order.'
        : 'Order preparation started.')
      prepareAttempt.current = null
    } catch (failure) {
      if (controller.signal.aborted) return
      const apiError = asApiError(failure)
      if (
        apiError.code === 'ORDER_STALE' || apiError.code === 'ORDER_STATE_CONFLICT'
        || apiError.code === 'ORDER_PAYMENT_REQUIRED' || apiError.code === 'ORDER_ALLOCATION_REQUIRED'
        || apiError.code === 'ORDER_PREPARATION_UNSUPPORTED'
      ) {
        prepareAttempt.current = null
        setPrepareOpen(false)
        try {
          const fresh = await client.detail(detail.order_id, controller.signal)
          if (!controller.signal.aborted) setDetail(fresh)
        } catch {
          // Keep the reviewed snapshot and the actionable conflict message.
        }
        if (!controller.signal.aborted) {
          setPrepareError(apiError.code === 'ORDER_STALE' || apiError.code === 'ORDER_STATE_CONFLICT'
            ? 'The Order changed since review. Its current preparation state is shown.'
            : apiError.code === 'ORDER_PAYMENT_REQUIRED'
              ? 'Preparation is blocked because matching verified Payment evidence is no longer available.'
              : apiError.code === 'ORDER_ALLOCATION_REQUIRED'
                ? 'Preparation is blocked because the full Stock allocation is no longer available.'
                : 'This Order is not supported by the preparation workflow.')
          void loadList(new AbortController().signal)
        }
      } else if (apiError.status === 0 || apiError.status >= 500) {
        setPrepareError('The result could not be confirmed. Retry the same preparation request.')
      } else {
        prepareAttempt.current = null
        setPrepareError(errorMessage(apiError))
      }
    } finally {
      if (!controller.signal.aborted) setPrepareBusy(false)
    }
  }

  const submitDispatch = async (event: FormEvent) => {
    event.preventDefault()
    const reference = handoffReference.trim()
    if (!detail || !canDispatch || !detail.dispatch.eligible || !reference || !dispatchAttested || dispatchBusy) return
    const expiry = auth.session?.recent_reauthentication_expires_at_epoch
    if (expiry == null || expiry < Math.floor(Date.now() / 1000)) {
      setDispatchReauth(true); return
    }
    const signature = `${detail.order_id}:${detail.updated_at}:${reference}`
    if (!dispatchAttempt.current || dispatchAttempt.current.signature !== signature) {
      dispatchAttempt.current = { signature, key: crypto.randomUUID() }
    }
    const controller = new AbortController()
    dispatchRequest.current?.abort(); dispatchRequest.current = controller
    setDispatchBusy(true); setDispatchError(null); setDispatchNotice(null)
    try {
      const csrf = await auth.getCsrfForMutation()
      if (controller.signal.aborted) return
      const result = await client.dispatch(
        detail.order_id, detail.updated_at, reference,
        dispatchAttempt.current.key, csrf, controller.signal,
      )
      if (controller.signal.aborted) return
      setDetail({
        ...detail,
        status: result.status,
        updated_at: result.updated_at,
        dispatch: {
          ...detail.dispatch,
          eligible: false,
          order_state: 'not_preparing',
          allocation_state: 'consumed',
          inventory_state: 'conflict',
        },
        delivery: {
          ...detail.delivery,
          eligible: false,
          order_state: 'delivering',
          dispatch_state: 'matched',
          association_state: detail.conversation_id ? 'verified' : 'missing',
          evidence_state: detail.conversation_id ? 'missing' : 'not_applicable',
          dispatch_occurred_at: result.updated_at,
          evidence_messages: [],
          evidence_has_more: false,
        },
      })
      setList((current) => current ? {
        ...current,
        items: current.items.map((item) => item.order_id === result.order_id
          ? { ...item, status: result.status, updated_at: result.updated_at }
          : item),
      } : current)
      setDispatchOpen(false); setHandoffReference(''); setDispatchAttested(false)
      setDispatchReauth(false); setDispatchPassword('')
      setDispatchNotice(result.replayed
        ? 'Dispatch was already recorded. Showing the current Order.'
        : 'Order dispatched. Customer delivery is not confirmed.')
      dispatchAttempt.current = null
    } catch (failure) {
      if (controller.signal.aborted) return
      const apiError = asApiError(failure)
      if (apiError.code === 'recent_reauthentication_required') {
        setDispatchReauth(true)
      } else if (
        apiError.code === 'ORDER_STALE' || apiError.code === 'ORDER_STATE_CONFLICT'
        || apiError.code === 'ORDER_PAYMENT_REQUIRED' || apiError.code === 'ORDER_ALLOCATION_REQUIRED'
        || apiError.code === 'ORDER_DISPATCH_METHOD_UNSUPPORTED' || apiError.code === 'ORDER_DISPATCH_UNSUPPORTED'
      ) {
        dispatchAttempt.current = null
        setDispatchOpen(false)
        try {
          const fresh = await client.detail(detail.order_id, controller.signal)
          if (!controller.signal.aborted) setDetail(fresh)
        } catch {
          // Preserve the reviewed snapshot and show the authoritative conflict.
        }
        if (!controller.signal.aborted) {
          setDispatchError(apiError.code === 'ORDER_STALE' || apiError.code === 'ORDER_STATE_CONFLICT'
            ? 'The Order changed since review. Its current dispatch state is shown.'
            : apiError.code === 'ORDER_PAYMENT_REQUIRED'
              ? 'Dispatch is blocked because matching verified Payment evidence is unavailable.'
              : apiError.code === 'ORDER_ALLOCATION_REQUIRED'
                ? 'Dispatch is blocked because full unconsumed allocated Stock is unavailable.'
                : apiError.code === 'ORDER_DISPATCH_METHOD_UNSUPPORTED'
                  ? 'Only moto taxi Orders can use this dispatch workflow.'
                  : 'This Order is not supported by the dispatch workflow.')
          void loadList(new AbortController().signal)
        }
      } else if (apiError.status === 0 || apiError.status >= 500) {
        setDispatchError('The result could not be confirmed. Retry the same dispatch request.')
      } else {
        dispatchAttempt.current = null
        setDispatchError(errorMessage(apiError))
      }
    } finally {
      if (!controller.signal.aborted) setDispatchBusy(false)
    }
  }

  const confirmDispatchPassword = async (event: FormEvent) => {
    event.preventDefault(); setDispatchBusy(true); setDispatchError(null)
    try {
      await auth.reauthenticate(dispatchPassword)
      setDispatchReauth(false)
      setDispatchNotice('Password confirmed. Review the handoff evidence and confirm dispatch.')
    } catch (failure) {
      setDispatchError(errorMessage(asApiError(failure)))
    } finally {
      setDispatchPassword(''); setDispatchBusy(false)
    }
  }

  const submitDelivery = async (event: FormEvent) => {
    event.preventDefault()
    if (
      !detail || !canConfirmDelivery || !detail.delivery.eligible || !selectedEvidence
      || !deliveryAttested || deliveryBusy
    ) return
    const expiry = auth.session?.recent_reauthentication_expires_at_epoch
    if (expiry == null || expiry < Math.floor(Date.now() / 1000)) {
      setDeliveryReauth(true); return
    }
    const signature = `${detail.order_id}:${detail.updated_at}:${selectedEvidence}`
    if (!deliveryAttempt.current || deliveryAttempt.current.signature !== signature) {
      deliveryAttempt.current = { signature, key: crypto.randomUUID() }
    }
    const controller = new AbortController()
    deliveryRequest.current?.abort(); deliveryRequest.current = controller
    setDeliveryBusy(true); setDeliveryError(null); setDeliveryNotice(null)
    try {
      const csrf = await auth.getCsrfForMutation()
      if (controller.signal.aborted) return
      const result = await client.confirmDelivery(
        detail.order_id, detail.updated_at, selectedEvidence,
        deliveryAttempt.current.key, csrf, controller.signal,
      )
      if (controller.signal.aborted) return
      setDetail({
        ...detail,
        status: result.status,
        delivered_at: result.delivered_at,
        updated_at: result.updated_at,
        readiness: { ...detail.readiness, delivery_state: 'delivered' },
        delivery: {
          ...detail.delivery,
          eligible: false,
          order_state: 'delivered',
          evidence_state: 'not_applicable',
          evidence_messages: [],
          evidence_has_more: false,
        },
      })
      setList((current) => current ? {
        ...current,
        items: current.items.map((item) => item.order_id === result.order_id
          ? { ...item, status: result.status, updated_at: result.updated_at, readiness: {
            ...item.readiness, delivery_state: 'delivered',
          } }
          : item),
      } : current)
      setDeliveryOpen(false); setSelectedEvidence(''); setDeliveryAttested(false)
      setDeliveryReauth(false); setDeliveryPassword('')
      setDeliveryNotice(result.replayed
        ? 'Delivery confirmation was already recorded. Showing the current Order.'
        : 'Customer-reported receipt recorded. This timestamp is not the exact physical handoff time.')
      deliveryAttempt.current = null
    } catch (failure) {
      if (controller.signal.aborted) return
      const apiError = asApiError(failure)
      if (apiError.code === 'recent_reauthentication_required') {
        setDeliveryReauth(true)
      } else if (
        apiError.code === 'ORDER_STALE' || apiError.code === 'ORDER_STATE_CONFLICT'
        || apiError.code === 'ORDER_DELIVERY_METHOD_UNSUPPORTED'
        || apiError.code === 'ORDER_DISPATCH_EVIDENCE_REQUIRED'
        || apiError.code === 'ORDER_DELIVERY_EVIDENCE_MISSING'
        || apiError.code === 'ORDER_DELIVERY_EVIDENCE_INVALID'
        || apiError.code === 'ORDER_DELIVERY_EVIDENCE_AMBIGUOUS'
        || apiError.code === 'ORDER_DELIVERY_EVIDENCE_REUSED'
        || apiError.code === 'ORDER_DELIVERY_UNSUPPORTED'
        || apiError.code === 'IDEMPOTENCY_CONFLICT'
      ) {
        deliveryAttempt.current = null
        setDeliveryOpen(false); setSelectedEvidence(''); setDeliveryAttested(false)
        try {
          const fresh = await client.detail(detail.order_id, controller.signal)
          if (!controller.signal.aborted) setDetail(fresh)
        } catch {
          // Preserve the reviewed snapshot and show the authoritative conflict.
        }
        if (!controller.signal.aborted) {
          setDeliveryError(apiError.code === 'ORDER_STALE' || apiError.code === 'ORDER_STATE_CONFLICT'
            ? 'The Order changed since review. Its current delivery state is shown.'
            : apiError.code === 'ORDER_DELIVERY_EVIDENCE_MISSING'
              ? 'The selected customer message is no longer available. Current evidence is shown.'
              : apiError.code === 'ORDER_DELIVERY_EVIDENCE_AMBIGUOUS'
                ? 'Delivery evidence is ambiguous between delivering Orders. No confirmation was recorded.'
                : apiError.code === 'ORDER_DELIVERY_EVIDENCE_REUSED'
                  ? 'That customer message was already used. Current evidence is shown.'
                  : 'Delivery confirmation is blocked. The current eligibility is shown.')
          void loadList(new AbortController().signal)
        }
      } else if (apiError.status === 0 || apiError.status >= 500) {
        setDeliveryError('The result could not be confirmed. Retry the same delivery request.')
      } else {
        deliveryAttempt.current = null
        setDeliveryError(errorMessage(apiError))
      }
    } finally {
      if (!controller.signal.aborted) setDeliveryBusy(false)
    }
  }

  const confirmDeliveryPassword = async (event: FormEvent) => {
    event.preventDefault(); setDeliveryBusy(true); setDeliveryError(null)
    try {
      await auth.reauthenticate(deliveryPassword)
      setDeliveryReauth(false)
      setDeliveryNotice('Password confirmed. Review the customer message and confirm receipt.')
    } catch (failure) {
      setDeliveryError(errorMessage(asApiError(failure)))
    } finally {
      setDeliveryPassword(''); setDeliveryBusy(false)
    }
  }

  const submitDeliveryIssue = async (event: FormEvent) => {
    event.preventDefault()
    if (!detail || !canReportIssue || issueBusy || issueSummary.trim().length < 10) return
    const signature = [detail.order_id, detail.updated_at, issueType, issueReason, issueSource,
      issueSummary.trim(), issueEvidence].join(':')
    if (!issueAttempt.current || issueAttempt.current.signature !== signature) {
      issueAttempt.current = { signature, key: crypto.randomUUID() }
    }
    const controller = new AbortController()
    issueRequest.current?.abort(); issueRequest.current = controller
    setIssueBusy(true); setIssueError(null); setIssueNotice(null)
    try {
      const csrf = await auth.getCsrfForMutation()
      if (controller.signal.aborted) return
      const result = await client.reportDeliveryIssue(
        detail.order_id, detail.updated_at, issueType, issueReason, issueSource,
        issueSummary.trim(), issueEvidence || null, issueAttempt.current.key, csrf, controller.signal,
      )
      if (controller.signal.aborted) return
      const refreshed = await client.detail(detail.order_id, controller.signal)
      if (controller.signal.aborted) return
      setDetail(refreshed)
      setIssueOpen(false); setIssueSummary(''); setIssueEvidence('')
      setIssueNotice(result.replayed
        ? 'This delivery issue was already recorded. Showing current history.'
        : 'Delivery issue recorded. Order, Payment and Stock facts were not changed.')
      issueAttempt.current = null
    } catch (failure) {
      if (controller.signal.aborted) return
      const apiError = asApiError(failure)
      if (apiError.code === 'ORDER_STALE' || apiError.code === 'DELIVERY_ISSUE_ALREADY_OPEN') {
        issueAttempt.current = null; setIssueOpen(false)
        setIssueError('The Order or its delivery issues changed. Review the refreshed history.')
        void loadDetail(new AbortController().signal)
      } else if (apiError.status === 0 || apiError.status >= 500) {
        setIssueError('The result could not be confirmed. Retry the same report request.')
      } else {
        issueAttempt.current = null; setIssueError(errorMessage(apiError))
      }
    } finally {
      if (!controller.signal.aborted) setIssueBusy(false)
    }
  }

  const submitIssueClosure = async (event: FormEvent) => {
    event.preventDefault()
    if (!detail || !closingIssueId || !canCloseIssue || issueBusy || resolutionReason.trim().length < 10) return
    const expiry = auth.session?.recent_reauthentication_expires_at_epoch
    if (expiry == null || expiry < Math.floor(Date.now() / 1000)) {
      setIssueCloseReauth(true); return
    }
    const signature = [detail.order_id, closingIssueId, resolutionCode, resolutionReason.trim()].join(':')
    if (!issueCloseAttempt.current || issueCloseAttempt.current.signature !== signature) {
      issueCloseAttempt.current = { signature, key: crypto.randomUUID() }
    }
    const controller = new AbortController()
    issueRequest.current?.abort(); issueRequest.current = controller
    setIssueBusy(true); setIssueError(null); setIssueNotice(null)
    try {
      const csrf = await auth.getCsrfForMutation()
      if (controller.signal.aborted) return
      const result = await client.closeDeliveryIssue(
        detail.order_id, closingIssueId, resolutionCode, resolutionReason.trim(),
        issueCloseAttempt.current.key, csrf, controller.signal,
      )
      if (controller.signal.aborted) return
      const refreshed = await client.detail(detail.order_id, controller.signal)
      if (controller.signal.aborted) return
      setDetail(refreshed)
      setClosingIssueId(null); setResolutionReason(''); setIssueCloseReauth(false); setIssueClosePassword('')
      setIssueNotice(result.replayed
        ? 'This issue closure was already recorded. Showing current history.'
        : 'Delivery issue closed with documented Administrator review.')
      issueCloseAttempt.current = null
    } catch (failure) {
      if (controller.signal.aborted) return
      const apiError = asApiError(failure)
      if (apiError.code === 'recent_reauthentication_required') {
        setIssueCloseReauth(true)
      } else if (apiError.code === 'DELIVERY_ISSUE_STATE_CONFLICT') {
        issueCloseAttempt.current = null; setClosingIssueId(null)
        setIssueError('The issue changed since review. Review the refreshed history.')
        void loadDetail(new AbortController().signal)
      } else if (apiError.status === 0 || apiError.status >= 500) {
        setIssueError('The result could not be confirmed. Retry the same closure request.')
      } else {
        issueCloseAttempt.current = null; setIssueError(errorMessage(apiError))
      }
    } finally {
      if (!controller.signal.aborted) setIssueBusy(false)
    }
  }

  const confirmIssueClosePassword = async (event: FormEvent) => {
    event.preventDefault(); setIssueBusy(true); setIssueError(null)
    try {
      await auth.reauthenticate(issueClosePassword)
      setIssueCloseReauth(false)
      setIssueNotice('Password confirmed. Review the resolution and close the issue.')
    } catch (failure) {
      setIssueError(errorMessage(asApiError(failure)))
    } finally {
      setIssueClosePassword(''); setIssueBusy(false)
    }
  }

  const listPath = `/business/orders?${new URLSearchParams({ status: status ?? 'all', offset: String(offset) })}`
  const chooseStatus = (value: string) => setParams({ status: value, offset: '0' })
  const chooseOffset = (value: number) => setParams({ status: status ?? 'all', offset: String(value) })

  return <section className="orders-page">
    <header className="business-page-heading"><div><p className="eyebrow">Business · Orders</p><h1>Orders</h1></div></header>
    <p className="orders-caution">Accepted Orders are not proof of payment, reserved stock, or delivery.</p>
    <div className={`orders-workspace${orderId ? ' orders-workspace--selected' : ''}`}>
      <section className="orders-list" aria-label="Orders queue">
        <label htmlFor="orders-status">Order status</label>
        <select id="orders-status" value={status ?? 'all'} onChange={(event) => chooseStatus(event.target.value)}>
          {statuses.map((value) => <option key={value} value={value}>{humanize(value)}</option>)}
          <option value="all">All statuses</option>
        </select>
        {!list && !listError && <p role="status">Loading Orders…</p>}
        {listError && <div role="alert"><p>{listError}</p><button className="button button--secondary" onClick={() => void loadList(new AbortController().signal)}>Retry</button></div>}
        {list && list.items.length === 0 && <p>No Orders found.</p>}
        {list && <ul>{list.items.map((order) => <li key={order.order_id}>
          <Link data-order-id={order.order_id} onClick={() => { lastSelectedOrder.current = order.order_id }}
            to={`/business/orders/${encodeURIComponent(order.order_id)}?${new URLSearchParams({ status: status ?? 'all', offset: String(offset) })}`} aria-current={order.order_id === orderId ? 'page' : undefined}>
            <span className="orders-queue-heading"><strong>Order {order.order_id.slice(0, 8)}</strong><time dateTime={order.created_at}>{formatDate(order.created_at)}</time></span>
            <span className="orders-queue-product">{order.first_product_name ?? 'Product unavailable'}</span>
            <span className="orders-queue-meta">
              <span><small>Items</small><strong>{order.item_count}</strong></span>
              <span><small>Accepted total</small><strong>{formatAmount(order.total_cdf, order.currency)}</strong></span>
            </span>
            <dl className="orders-queue-states">
              <div><dt>Order</dt><dd data-tone={order.status === 'delivered' ? 'success' : order.status === 'cancelled' ? 'muted' : order.status === 'pending' ? 'neutral' : 'progress'}>{humanize(order.status)}</dd></div>
              <div><dt>Payment</dt><dd data-tone={order.readiness.payment_state === 'verified' ? 'success' : order.readiness.payment_state === 'mismatch' ? 'warning' : 'muted'}>{queuePayment(order.readiness.payment_state)}</dd></div>
              <div><dt>Stock</dt><dd data-tone={order.readiness.stock_state === 'full_unconsumed' && order.readiness.inventory_state === 'ready' ? 'success' : order.readiness.stock_state === 'mismatch' || order.readiness.inventory_state === 'conflict' && order.readiness.stock_state === 'full_unconsumed' ? 'warning' : 'muted'}>{queueStock(order)}</dd></div>
              <div><dt>Delivery</dt><dd data-tone={order.readiness.delivery_state === 'delivered' ? 'success' : order.readiness.delivery_state === 'delivering' ? 'progress' : 'muted'}>{queueDelivery(order)}</dd></div>
            </dl>
            {order.readiness.issue_state === 'open' && <span className="orders-queue-alert">Open delivery issue</span>}
          </Link>
        </li>)}</ul>}
        {list && (offset > 0 || list.has_more && offset + 20 <= 10000) && <div className="orders-pagination">
          {offset > 0 && <button className="button button--secondary" onClick={() => chooseOffset(Math.max(0, offset - 20))}>Previous</button>}
          {list.has_more && offset + 20 <= 10000 && <button className="button button--secondary" onClick={() => chooseOffset(offset + 20)}>Next</button>}
        </div>}
      </section>
      {orderId && <section className="orders-detail" aria-label="Order detail">
        <Link className="orders-back" to={listPath}>Back to Orders</Link>
        {!detail && !detailError && <p role="status">Loading Order…</p>}
        {detailError && <div role="alert"><p>{detailError}</p><button className="button button--secondary" onClick={() => void loadDetail(new AbortController().signal)}>Retry</button></div>}
        {detail && <>
          <h2 ref={detailHeading} tabIndex={-1}>Order {detail.order_id.slice(0, 8)}</h2>
          <section className="orders-current-state" aria-labelledby="order-current-state-heading">
            <h3 id="order-current-state-heading">Current operational state</h3>
            <p className="orders-current-focus">Current focus — {currentOperationalState(detail)}</p>
            <dl className="orders-detail-summary">
              <div><dt>Status</dt><dd>{humanize(detail.status)}</dd></div>
              <div><dt>Accepted total</dt><dd>{formatAmount(detail.total_cdf, detail.currency)}</dd></div>
              <div><dt>Items</dt><dd>{detail.item_count}</dd></div>
              <div><dt>Recorded zone</dt><dd>{detail.delivery_zone}</dd></div>
              <div><dt>Payment</dt><dd>{queuePayment(detail.preparation.payment_state)}</dd></div>
              <div><dt>Stock</dt><dd>{detailStock(detail)}</dd></div>
              <div><dt>Delivery</dt><dd>{detailDelivery(detail)}</dd></div>
              <div><dt>Issue</dt><dd>{detail.delivery.issue_state === 'open' ? 'Open' : 'None open'}</dd></div>
              <div><dt>Created</dt><dd><time dateTime={detail.created_at}>{formatDate(detail.created_at)}</time></dd></div>
              {detail.delivered_at && <div><dt>Customer message received</dt><dd><time dateTime={detail.delivered_at}>{formatDate(detail.delivered_at)}</time></dd></div>}
            </dl>
            {prepareNotice && detail.status !== 'confirmed' && <p role="status">{prepareNotice}</p>}
            {dispatchNotice && detail.status !== 'preparing' && <p role="status">{dispatchNotice}</p>}
            {cancelError && <p role="alert">{cancelError}</p>}
            {cancelNotice && <p role="status">{cancelNotice}</p>}
            {canCancel && detail.status === 'pending' && !cancelOpen && <div className="orders-actions">
              <button className="button button--secondary" onClick={() => {
                setCancelOpen(true); setCancelError(null); setCancelNotice(null)
              }}>Cancel Order</button>
            </div>}
            {canPrepare && detail.preparation.eligible && !prepareOpen && <div className="orders-actions">
              <button className="button button--primary" onClick={() => {
                setPrepareOpen(true); setPrepareError(null); setPrepareNotice(null)
              }}>Prepare Order</button>
            </div>}
            {canDispatch && detail.dispatch.eligible && !dispatchOpen && <div className="orders-actions">
              <button className="button button--primary" onClick={() => {
                setDispatchOpen(true); setDispatchError(null); setDispatchNotice(null)
              }}>Dispatch Order</button>
            </div>}
            {canConfirmDelivery && detail.delivery.eligible && !deliveryOpen && <div className="orders-actions">
              <button className="button button--primary" onClick={() => {
                setDeliveryOpen(true); setDeliveryError(null); setDeliveryNotice(null)
              }}>Confirm customer receipt</button>
            </div>}
            {canCancel && detail.status === 'pending' && cancelOpen && <form className="orders-cancellation" onSubmit={(event) => void submitCancellation(event)}>
              <h3>Cancel pending Order</h3>
              <p>This records an authoritative cancellation. It does not change Payment or Stock.</p>
              <label htmlFor="order-cancellation-reason">Cancellation reason</label>
              <select id="order-cancellation-reason" required value={cancelReason} disabled={cancelBusy}
                onChange={(event) => {
                  setCancelReason(event.target.value as OrderCancellationReason | '')
                  attempt.current = null
                }}>
                <option value="">Select a reason</option>
                {cancellationReasons.map((reason) => <option key={reason.value} value={reason.value}>{reason.label}</option>)}
              </select>
              <div className="orders-actions">
                <button className="button button--primary" disabled={cancelBusy || !cancelReason || reauth}>Confirm cancellation</button>
                <button type="button" className="button button--secondary" disabled={cancelBusy} onClick={() => {
                  setCancelOpen(false); setCancelReason(''); setCancelError(null); setReauth(false); setPassword(''); attempt.current = null
                }}>Keep pending</button>
              </div>
            </form>}
            {canCancel && cancelOpen && reauth && <form className="orders-cancellation" onSubmit={(event) => void confirmPassword(event)}>
              <PasswordField id="order-cancellation-password" label="Confirm Administrator password" autoComplete="current-password"
                value={password} disabled={cancelBusy} required onChange={(event) => setPassword(event.target.value)} />
              <button className="button button--primary" disabled={cancelBusy}>Confirm password</button>
            </form>}
          </section>
          <section className="orders-commercial" aria-labelledby="order-commercial-heading">
            <h3 id="order-commercial-heading">Accepted commercial snapshot</h3>
            <ul className="orders-lines">{detail.items.map((line, index) => <li key={`${line.product_id}-${index}`}>
              <strong>{line.product_name ?? `Product ${line.product_id}`}{line.model_label ? ` · ${line.model_label}` : ''}</strong>
              <span>Quantity {line.quantity} · {formatAmount(line.unit_price_cdf, 'CDF')} per unit</span>
              {line.unit_price_usd && <span>Accepted USD price {formatAmount(line.unit_price_usd, 'USD')}</span>}
              {line.usd_to_cdf_rate && <span>Accepted USD/CDF rate {line.usd_to_cdf_rate}</span>}
              <div className="orders-links">
                {uuidPattern.test(line.product_id) && <Link to={`/business/products/${line.product_id}`}>Current Product</Link>}
                {line.sellable_item_id && <Link to={`/business/stock?item=${line.sellable_item_id}`}>Current Stock</Link>}
              </div>
            </li>)}</ul>
            <p className="orders-current-note">Product and Stock links show current facts, which may differ from this accepted snapshot.</p>
            {detail.conversation_id && <nav className="orders-related-links" aria-label="Related customer records">
              <Link to={`/business/customers/${detail.conversation_id}`}>Open Customer profile</Link>
              <Link to={`/inbox/${detail.conversation_id}`}>Open associated Inbox conversation</Link>
            </nav>}
          </section>
          {(detail.status === 'confirmed' || detail.status === 'preparing' || detail.status === 'delivering' || detail.status === 'delivered' || detail.delivery_issues.length > 0)
            && <h3 className="orders-lifecycle-heading">Lifecycle details</h3>}
          {detail.status === 'confirmed' && <section className="orders-preparation" aria-labelledby="order-preparation-heading">
            <h3 id="order-preparation-heading">Preparation readiness</h3>
            {prepareError && <p role="alert">{prepareError}</p>}
            {prepareNotice && <p role="status">{prepareNotice}</p>}
            <details className="orders-evidence-disclosure">
              <summary tabIndex={0}>Payment and Stock evidence</summary>
              <dl className="orders-facts">
                <div><dt>Payment verification</dt><dd>{paymentReadiness(detail.preparation.payment_state)}</dd></div>
                <div><dt>Stock allocation</dt><dd>{allocationReadiness(detail.preparation)}</dd></div>
                <div><dt>Eligibility</dt><dd>{detail.preparation.eligible ? 'Ready to prepare' : 'Not eligible'}</dd></div>
              </dl>
            </details>
            {canPrepare && detail.preparation.eligible && prepareOpen && <form className="orders-preparation-confirm" onSubmit={(event) => void submitPreparation(event)}>
              <h3>Begin Order preparation?</h3>
              <p>This changes the Order from confirmed to preparing. It does not dispatch, deliver, or settle Payment.</p>
              <div className="orders-actions">
                <button className="button button--primary" disabled={prepareBusy}>Confirm preparation</button>
                <button type="button" className="button button--secondary" disabled={prepareBusy} onClick={() => {
                  setPrepareOpen(false); setPrepareError(null); prepareAttempt.current = null
                }}>Keep confirmed</button>
              </div>
            </form>}
          </section>}
          {detail.status === 'preparing' && <section className="orders-dispatch" aria-labelledby="order-dispatch-heading">
            <h3 id="order-dispatch-heading">Dispatch readiness</h3>
            {dispatchError && <p role="alert">{dispatchError}</p>}
            {dispatchNotice && <p role="status">{dispatchNotice}</p>}
            <details className="orders-evidence-disclosure">
              <summary tabIndex={0}>Payment, Stock and dispatch evidence</summary>
              <dl className="orders-facts">
                <div><dt>Payment verification</dt><dd>{paymentReadiness(detail.dispatch.payment_state)}</dd></div>
                <div><dt>Allocated Stock</dt><dd>{dispatchStockReadiness(detail.dispatch)}</dd></div>
                <div><dt>Delivery method</dt><dd>{detail.dispatch.delivery_method_state === 'supported' ? 'Moto taxi supported' : 'Not supported for dispatch'}</dd></div>
                <div><dt>Eligibility</dt><dd>{detail.dispatch.eligible ? 'Ready to dispatch' : 'Not eligible'}</dd></div>
              </dl>
            </details>
            {canDispatch && detail.dispatch.eligible && dispatchOpen && <form className="orders-dispatch-confirm" onSubmit={(event) => void submitDispatch(event)}>
              <h3>Confirm physical dispatch</h3>
              <p>This deducts the allocated items from physical Stock and changes the Order to delivering. It does not confirm customer receipt.</p>
              <label htmlFor="order-handoff-reference">Internal handoff reference</label>
              <input id="order-handoff-reference" required maxLength={100} value={handoffReference} disabled={dispatchBusy}
                onChange={(event) => { setHandoffReference(event.target.value); dispatchAttempt.current = null }} />
              <label className="orders-attestation">
                <input type="checkbox" checked={dispatchAttested} disabled={dispatchBusy}
                  onChange={(event) => { setDispatchAttested(event.target.checked); dispatchAttempt.current = null }} />
                I confirm the full allocated quantity physically left MBB custody.
              </label>
              <div className="orders-actions">
                <button className="button button--primary" disabled={dispatchBusy || !handoffReference.trim() || !dispatchAttested || dispatchReauth}>Confirm dispatch</button>
                <button type="button" className="button button--secondary" disabled={dispatchBusy} onClick={() => {
                  setDispatchOpen(false); setHandoffReference(''); setDispatchAttested(false)
                  setDispatchError(null); setDispatchReauth(false); setDispatchPassword(''); dispatchAttempt.current = null
                }}>Keep preparing</button>
              </div>
            </form>}
            {canDispatch && dispatchOpen && dispatchReauth && <form className="orders-dispatch-confirm" onSubmit={(event) => void confirmDispatchPassword(event)}>
              <PasswordField id="order-dispatch-password" label="Confirm Administrator password" autoComplete="current-password"
                value={dispatchPassword} disabled={dispatchBusy} required onChange={(event) => setDispatchPassword(event.target.value)} />
              <button className="button button--primary" disabled={dispatchBusy}>Confirm password</button>
            </form>}
          </section>}
          {(detail.status === 'delivering' || detail.status === 'delivered') && <section className="orders-delivery" aria-labelledby="order-delivery-heading">
            <h3 id="order-delivery-heading">Delivery confirmation</h3>
            {deliveryError && <p role="alert">{deliveryError}</p>}
            {deliveryNotice && <p role="status">{deliveryNotice}</p>}
            <p className="orders-current-note">Delivery confirmation records when MBB received the customer&apos;s message. It is not the exact physical handoff time.</p>
            <dl className="orders-facts">
              <div><dt>Dispatch evidence</dt><dd>{detail.delivery.dispatch_state === 'matched' ? 'Matched immutable dispatch record' : 'Not available'}</dd></div>
              <div><dt>Customer association</dt><dd>{detail.delivery.association_state === 'verified' ? 'Verified Order conversation' : detail.delivery.association_state === 'ambiguous' ? 'Ambiguous between delivering Orders' : detail.delivery.association_state === 'missing' ? 'Verified conversation missing' : 'Not applicable'}</dd></div>
              <div><dt>Eligibility</dt><dd>{deliveryReadiness(detail.delivery)}</dd></div>
            </dl>
            {detail.delivery.dispatch_occurred_at && <p>Dispatched <time dateTime={detail.delivery.dispatch_occurred_at}>{formatDate(detail.delivery.dispatch_occurred_at)}</time></p>}
            {detail.delivery.evidence_messages.length > 0 && <div className="orders-evidence-list" aria-label="Eligible customer receipt messages">
              <h4>Eligible customer messages</h4>
              <p>These are authentic inbound WhatsApp text messages stored after dispatch. An Administrator must decide whether a message unambiguously confirms customer receipt.</p>
              {detail.delivery.evidence_messages.map((message) => <article key={message.message_id} className="orders-evidence-message">
                {canConfirmDelivery && deliveryOpen && <input
                  type="radio"
                  id={`delivery-evidence-${message.message_id}`}
                  name="delivery-evidence"
                  value={message.message_id}
                  checked={selectedEvidence === message.message_id}
                  disabled={deliveryBusy}
                  onChange={() => {
                    setSelectedEvidence(message.message_id)
                    setDeliveryAttested(false)
                    deliveryAttempt.current = null
                  }}
                />}
                <div>
                  {canConfirmDelivery && deliveryOpen
                    ? <label htmlFor={`delivery-evidence-${message.message_id}`}><strong>Inbound WhatsApp customer message</strong></label>
                    : <strong>Inbound WhatsApp customer message</strong>}
                  <blockquote>{message.content_excerpt}{message.content_truncated ? '…' : ''}</blockquote>
                  <p>Received by MBB <time dateTime={message.received_at}>{formatDate(message.received_at)}</time> · receipt sequence {message.inbound_receipt_order}</p>
                </div>
              </article>)}
              {detail.delivery.evidence_has_more && <p>Only the 20 most recent eligible messages are shown. Open the associated Inbox conversation for full context.</p>}
            </div>}
            {canConfirmDelivery && detail.delivery.eligible && deliveryOpen && <form className="orders-delivery-confirm" onSubmit={(event) => void submitDelivery(event)}>
              <h3>Confirm delivery from customer evidence</h3>
              <p>Select one customer-originated message above. Do not infer receipt from dispatch or courier handoff.</p>
              <label className="orders-attestation">
                <input type="checkbox" checked={deliveryAttested} disabled={deliveryBusy || !selectedEvidence}
                  onChange={(event) => { setDeliveryAttested(event.target.checked); deliveryAttempt.current = null }} />
                I reviewed the selected message and attest that the customer unambiguously reported receiving this Order.
              </label>
              <div className="orders-actions">
                <button className="button button--primary" disabled={deliveryBusy || !selectedEvidence || !deliveryAttested || deliveryReauth}>Confirm delivery</button>
                <button type="button" className="button button--secondary" disabled={deliveryBusy} onClick={() => {
                  setDeliveryOpen(false); setSelectedEvidence(''); setDeliveryAttested(false)
                  setDeliveryError(null); setDeliveryReauth(false); setDeliveryPassword(''); deliveryAttempt.current = null
                }}>Keep delivering</button>
              </div>
            </form>}
            {canConfirmDelivery && deliveryOpen && deliveryReauth && <form className="orders-delivery-confirm" onSubmit={(event) => void confirmDeliveryPassword(event)}>
              <PasswordField id="order-delivery-password" label="Confirm Administrator password" autoComplete="current-password"
                value={deliveryPassword} disabled={deliveryBusy} required onChange={(event) => setDeliveryPassword(event.target.value)} />
              <button className="button button--primary" disabled={deliveryBusy}>Confirm password</button>
            </form>}
          </section>}
          {(detail.status === 'delivering' || detail.status === 'delivered' || detail.delivery_issues.length > 0) && <section className="orders-delivery-issues" aria-labelledby="order-delivery-issues-heading">
            <h3 id="order-delivery-issues-heading">Delivery issues</h3>
            {issueError && <p role="alert">{issueError}</p>}
            {issueNotice && <p role="status">{issueNotice}</p>}
            <p className="orders-current-note">Issue reports and triage do not change Order, Payment, dispatch, delivery evidence or Stock facts.</p>
            {detail.delivery_issues.length === 0 && <p>No delivery issues recorded.</p>}
            {detail.delivery_issues.map((issue) => <article key={issue.issue_id} className="orders-evidence-message">
              <div>
                <strong>{issue.issue_type === 'failed' ? 'Failed delivery report' : 'Delivery dispute'} · {issue.status}</strong>
                <p>{issue.summary}</p>
                <p>{humanize(issue.reason_code)} · {humanize(issue.source)} · reported by {issue.reported_by_display_name} on <time dateTime={issue.reported_at}>{formatDate(issue.reported_at)}</time></p>
                {issue.evidence_message_id && <p>Validated customer-message evidence: {issue.evidence_message_id}</p>}
                {issue.status === 'closed' && <>
                  <p><strong>Resolution:</strong> {humanize(issue.resolution_code ?? '')} — {issue.resolution_reason}</p>
                  {issue.closed_at && <p>Closed by {issue.closed_by_display_name} on <time dateTime={issue.closed_at}>{formatDate(issue.closed_at)}</time></p>}
                </>}
                {issue.status === 'open' && canCloseIssue && closingIssueId !== issue.issue_id && <div className="orders-actions">
                  <button className="button button--secondary" onClick={() => {
                    setClosingIssueId(issue.issue_id); setResolutionCode('false_alarm'); setResolutionReason('')
                    setIssueError(null); setIssueNotice(null); setIssueCloseReauth(false); issueCloseAttempt.current = null
                  }}>Review and close</button>
                </div>}
                {issue.status === 'open' && canCloseIssue && closingIssueId === issue.issue_id && <form className="orders-delivery-confirm" onSubmit={(event) => void submitIssueClosure(event)}>
                  <label htmlFor={`issue-resolution-${issue.issue_id}`}>Resolution</label>
                  <select id={`issue-resolution-${issue.issue_id}`} value={resolutionCode} disabled={issueBusy}
                    onChange={(event) => { setResolutionCode(event.target.value as OrderDeliveryIssueResolution); issueCloseAttempt.current = null }}>
                    <option value="false_alarm">Documented false alarm</option>
                    {detail.status === 'delivering' && <option value="delivery_continuing">Delivery is continuing</option>}
                  </select>
                  <label htmlFor={`issue-resolution-reason-${issue.issue_id}`}>Administrator reason</label>
                  <textarea id={`issue-resolution-reason-${issue.issue_id}`} required minLength={10} maxLength={500}
                    value={resolutionReason} disabled={issueBusy}
                    onChange={(event) => { setResolutionReason(event.target.value); issueCloseAttempt.current = null }} />
                  <p>Closing records triage only. It does not confirm delivery or change financial or Stock facts.</p>
                  <div className="orders-actions">
                    <button className="button button--primary" disabled={issueBusy || resolutionReason.trim().length < 10 || issueCloseReauth}>Close issue</button>
                    <button type="button" className="button button--secondary" disabled={issueBusy} onClick={() => {
                      setClosingIssueId(null); setResolutionReason(''); setIssueCloseReauth(false); setIssueClosePassword('')
                      setIssueError(null); issueCloseAttempt.current = null
                    }}>Keep open</button>
                  </div>
                </form>}
                {issue.status === 'open' && canCloseIssue && closingIssueId === issue.issue_id && issueCloseReauth && <form className="orders-delivery-confirm" onSubmit={(event) => void confirmIssueClosePassword(event)}>
                  <PasswordField id={`issue-close-password-${issue.issue_id}`} label="Confirm Administrator password" autoComplete="current-password"
                    value={issueClosePassword} disabled={issueBusy} required onChange={(event) => setIssueClosePassword(event.target.value)} />
                  <button className="button button--primary" disabled={issueBusy}>Confirm password</button>
                </form>}
              </div>
            </article>)}
            {canReportIssue && (detail.status === 'delivering' || detail.status === 'delivered')
              && !detail.delivery_issues.some((issue) => issue.status === 'open') && !issueOpen && <div className="orders-actions">
              <button className="button button--secondary" onClick={() => {
                const type: OrderDeliveryIssueType = detail.status === 'delivered' ? 'disputed' : 'failed'
                setIssueType(type); setIssueReason(issueReasons[type][0].value); setIssueOpen(true)
                setIssueError(null); setIssueNotice(null)
              }}>Report delivery problem</button>
            </div>}
            {canReportIssue && issueOpen && <form className="orders-delivery-confirm" onSubmit={(event) => void submitDeliveryIssue(event)}>
              <label htmlFor="delivery-issue-type">Problem type</label>
              <select id="delivery-issue-type" value={issueType} disabled={issueBusy || detail.status === 'delivered'}
                onChange={(event) => {
                  const type = event.target.value as OrderDeliveryIssueType
                  setIssueType(type); setIssueReason(issueReasons[type][0].value); issueAttempt.current = null
                }}>
                {detail.status === 'delivering' && <option value="failed">Failed delivery</option>}
                <option value="disputed">Disputed delivery</option>
              </select>
              <label htmlFor="delivery-issue-reason">Reason</label>
              <select id="delivery-issue-reason" value={issueReason} disabled={issueBusy}
                onChange={(event) => { setIssueReason(event.target.value as OrderDeliveryIssueReason); issueAttempt.current = null }}>
                {issueReasons[issueType].map((reason) => <option key={reason.value} value={reason.value}>{reason.label}</option>)}
              </select>
              <label htmlFor="delivery-issue-source">Source</label>
              <select id="delivery-issue-source" value={issueSource} disabled={issueBusy}
                onChange={(event) => { setIssueSource(event.target.value as OrderDeliveryIssueSource); issueAttempt.current = null }}>
                {issueSources.map((source) => <option key={source.value} value={source.value}>{source.label}</option>)}
              </select>
              <label htmlFor="delivery-issue-summary">Factual summary</label>
              <textarea id="delivery-issue-summary" required minLength={10} maxLength={500} value={issueSummary} disabled={issueBusy}
                onChange={(event) => { setIssueSummary(event.target.value); issueAttempt.current = null }} />
              {detail.delivery.evidence_messages.length > 0 && <>
                <label htmlFor="delivery-issue-evidence">Optional validated customer message</label>
                <select id="delivery-issue-evidence" value={issueEvidence} disabled={issueBusy}
                  onChange={(event) => { setIssueEvidence(event.target.value); issueAttempt.current = null }}>
                  <option value="">No message evidence</option>
                  {detail.delivery.evidence_messages.map((message) => <option key={message.message_id} value={message.message_id}>
                    {message.content_excerpt.slice(0, 80)}
                  </option>)}
                </select>
              </>}
              <div className="orders-actions">
                <button className="button button--primary" disabled={issueBusy || issueSummary.trim().length < 10}>Record issue</button>
                <button type="button" className="button button--secondary" disabled={issueBusy} onClick={() => {
                  setIssueOpen(false); setIssueSummary(''); setIssueEvidence(''); setIssueError(null); issueAttempt.current = null
                }}>Cancel report</button>
              </div>
            </form>}
          </section>}
        </>}
      </section>}
    </div>
  </section>
}
