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
  const [reauth, setReauth] = useState(false)
  const [password, setPassword] = useState('')
  const attempt = useRef<{ signature: string; key: string } | null>(null)
  const cancellation = useRef<AbortController | null>(null)
  const canCancel = auth.session?.capabilities.includes('order.cancel') ?? false

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
    setCancelOpen(false); setCancelReason(''); setCancelError(null); setCancelNotice(null)
    setReauth(false); setPassword(''); setCancelBusy(false)
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
        {detail && <>
          <h2>Order {detail.order_id.slice(0, 8)}</h2>
          <dl className="orders-facts">
            <div><dt>Status</dt><dd>{detail.status}</dd></div>
            <div><dt>Accepted total</dt><dd>{formatAmount(detail.total_cdf, detail.currency)}</dd></div>
            <div><dt>Created</dt><dd><time dateTime={detail.created_at}>{formatDate(detail.created_at)}</time></dd></div>
            <div><dt>Recorded zone</dt><dd>{detail.delivery_zone}</dd></div>
          </dl>
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
