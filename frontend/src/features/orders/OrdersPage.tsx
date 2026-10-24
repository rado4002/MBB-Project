import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import {
  createBusinessOrdersClient,
  type OrderCancellationReason,
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
  if (delivery.association_state === 'missing') return 'The verified customer conversation is unavailable'
  if (delivery.association_state === 'ambiguous') return 'More than one delivering Order is associated with this conversation'
  if (delivery.evidence_state === 'missing') return 'No eligible post-dispatch inbound customer text has been received'
  return 'Customer receipt evidence is ready for Administrator review'
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
  const canCancel = auth.session?.capabilities.includes('order.cancel') ?? false
  const canPrepare = auth.session?.capabilities.includes('order.prepare') ?? false
  const canDispatch = auth.session?.capabilities.includes('order.dispatch') ?? false
  const canConfirmDelivery = auth.session?.capabilities.includes('order.delivery.confirm') ?? false

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
    setCancelOpen(false); setCancelReason(''); setCancelError(null); setCancelNotice(null)
    setReauth(false); setPassword(''); setCancelBusy(false)
    setPrepareOpen(false); setPrepareBusy(false); setPrepareError(null); setPrepareNotice(null)
    setDispatchOpen(false); setDispatchBusy(false); setDispatchError(null); setDispatchNotice(null)
    setHandoffReference(''); setDispatchAttested(false); setDispatchReauth(false); setDispatchPassword('')
    setDeliveryOpen(false); setDeliveryBusy(false); setDeliveryError(null); setDeliveryNotice(null)
    setSelectedEvidence(''); setDeliveryAttested(false); setDeliveryReauth(false); setDeliveryPassword('')
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
          ? { ...item, status: result.status, updated_at: result.updated_at }
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
          ? { ...item, status: result.status, updated_at: result.updated_at }
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
          {statuses.map((value) => <option key={value} value={value}>{value}</option>)}
          <option value="all">All statuses</option>
        </select>
        {!list && !listError && <p role="status">Loading Orders…</p>}
        {listError && <div role="alert"><p>{listError}</p><button className="button button--secondary" onClick={() => void loadList(new AbortController().signal)}>Retry</button></div>}
        {list && list.items.length === 0 && <p>No Orders found.</p>}
        {list && <ul>{list.items.map((order) => <li key={order.order_id}>
          <Link to={`/business/orders/${encodeURIComponent(order.order_id)}?${new URLSearchParams({ status: status ?? 'all', offset: String(offset) })}`} aria-current={order.order_id === orderId ? 'page' : undefined}>
            <strong>{order.first_product_name ?? `Order ${order.order_id.slice(0, 8)}`}</strong>
            <span>{order.status} · {formatAmount(order.total_cdf, order.currency)}</span>
            <time dateTime={order.created_at}>{formatDate(order.created_at)}</time>
          </Link>
        </li>)}</ul>}
        {list && <div className="orders-pagination">
          <button className="button button--secondary" disabled={offset === 0} onClick={() => chooseOffset(Math.max(0, offset - 20))}>Previous</button>
          <button className="button button--secondary" disabled={!list.has_more || offset + 20 > 10000} onClick={() => chooseOffset(offset + 20)}>Next</button>
        </div>}
      </section>
      {orderId && <section className="orders-detail" aria-label="Order detail">
        <Link className="orders-back" to={listPath}>Back to Orders</Link>
        {!detail && !detailError && <p role="status">Loading Order…</p>}
        {detailError && <div role="alert"><p>{detailError}</p><button className="button button--secondary" onClick={() => void loadDetail(new AbortController().signal)}>Retry</button></div>}
        {cancelError && <p role="alert">{cancelError}</p>}
        {cancelNotice && <p role="status">{cancelNotice}</p>}
        {prepareError && <p role="alert">{prepareError}</p>}
        {prepareNotice && <p role="status">{prepareNotice}</p>}
        {dispatchError && <p role="alert">{dispatchError}</p>}
        {dispatchNotice && <p role="status">{dispatchNotice}</p>}
        {deliveryError && <p role="alert">{deliveryError}</p>}
        {deliveryNotice && <p role="status">{deliveryNotice}</p>}
        {detail && <>
          <h2>Order {detail.order_id.slice(0, 8)}</h2>
          <dl className="orders-facts">
            <div><dt>Status</dt><dd>{detail.status}</dd></div>
            <div><dt>Accepted total</dt><dd>{formatAmount(detail.total_cdf, detail.currency)}</dd></div>
            <div><dt>Created</dt><dd><time dateTime={detail.created_at}>{formatDate(detail.created_at)}</time></dd></div>
            <div><dt>Recorded zone</dt><dd>{detail.delivery_zone}</dd></div>
            {detail.delivered_at && <div><dt>Customer message received</dt><dd><time dateTime={detail.delivered_at}>{formatDate(detail.delivered_at)}</time></dd></div>}
          </dl>
          <section className="orders-preparation" aria-labelledby="order-preparation-heading">
            <h3 id="order-preparation-heading">Preparation readiness</h3>
            <dl className="orders-facts">
              <div><dt>Payment verification</dt><dd>{paymentReadiness(detail.preparation.payment_state)}</dd></div>
              <div><dt>Stock allocation</dt><dd>{allocationReadiness(detail.preparation)}</dd></div>
              <div><dt>Eligibility</dt><dd>{detail.preparation.eligible ? 'Ready to prepare' : 'Not eligible'}</dd></div>
            </dl>
            {canPrepare && detail.preparation.eligible && !prepareOpen && <div className="orders-actions">
              <button className="button button--primary" onClick={() => {
                setPrepareOpen(true); setPrepareError(null); setPrepareNotice(null)
              }}>Prepare Order</button>
            </div>}
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
          </section>
          <section className="orders-dispatch" aria-labelledby="order-dispatch-heading">
            <h3 id="order-dispatch-heading">Dispatch readiness</h3>
            <dl className="orders-facts">
              <div><dt>Payment verification</dt><dd>{paymentReadiness(detail.dispatch.payment_state)}</dd></div>
              <div><dt>Allocated Stock</dt><dd>{dispatchStockReadiness(detail.dispatch)}</dd></div>
              <div><dt>Delivery method</dt><dd>{detail.dispatch.delivery_method_state === 'supported' ? 'Moto taxi supported' : 'Not supported for dispatch'}</dd></div>
              <div><dt>Eligibility</dt><dd>{detail.dispatch.eligible ? 'Ready to dispatch' : 'Not eligible'}</dd></div>
            </dl>
            {canDispatch && detail.dispatch.eligible && !dispatchOpen && <div className="orders-actions">
              <button className="button button--primary" onClick={() => {
                setDispatchOpen(true); setDispatchError(null); setDispatchNotice(null)
              }}>Dispatch Order</button>
            </div>}
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
          </section>
          <section className="orders-delivery" aria-labelledby="order-delivery-heading">
            <h3 id="order-delivery-heading">Delivery confirmation</h3>
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
            {canConfirmDelivery && detail.delivery.eligible && !deliveryOpen && <div className="orders-actions">
              <button className="button button--primary" onClick={() => {
                setDeliveryOpen(true); setDeliveryError(null); setDeliveryNotice(null)
              }}>Confirm customer receipt</button>
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
          </section>
          <h3>Accepted commercial snapshot</h3>
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
          {detail.conversation_id && <Link to={`/inbox/${detail.conversation_id}`}>Open associated Inbox conversation</Link>}
          {canCancel && detail.status === 'pending' && !cancelOpen && <div className="orders-actions">
            <button className="button button--secondary" onClick={() => {
              setCancelOpen(true); setCancelError(null); setCancelNotice(null)
            }}>Cancel Order</button>
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
        </>}
      </section>}
    </div>
  </section>
}
