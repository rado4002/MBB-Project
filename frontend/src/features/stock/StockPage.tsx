import { useCallback, useEffect, useId, useMemo, useRef, useState, type FormEvent } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { createBusinessStockClient, type StockItem, type StockMovement } from '../../api/businessStock'
import { asApiError, errorMessage } from '../../api/errors'
import { useAuth } from '../../auth/AuthProvider'
import { PasswordField } from '../../components/PasswordField'

const availability = { available: 'Available', out_of_stock: 'Out of stock', unknown: 'Availability unconfirmed' }
const maxQuantity = 2147483647

interface Review {
  kind: 'receive' | 'adjust'
  value: number
  reason: string
  expected_quantity: number | null
  expected_updated_at: string | null
  operation_key: string
}

function pendingKey(id: string) { return `mbb.stock.pending.${id}` }
function readPending(id: string): Review | null {
  try {
    const saved = sessionStorage.getItem(pendingKey(id))
    if (!saved) return null
    const value: unknown = JSON.parse(saved)
    if (typeof value !== 'object' || value === null) return null
    const review = value as Partial<Review>
    return (review.kind === 'receive' || review.kind === 'adjust') &&
      typeof review.value === 'number' && Number.isInteger(review.value) &&
      typeof review.operation_key === 'string' && typeof review.reason === 'string' &&
      (review.expected_quantity === null || typeof review.expected_quantity === 'number') &&
      (review.expected_updated_at === null || typeof review.expected_updated_at === 'string')
      ? review as Review : null
  } catch { return null }
}
function savePending(id: string, review: Review) {
  try { sessionStorage.setItem(pendingKey(id), JSON.stringify(review)) } catch { /* Retry remains available in this page. */ }
}
function clearPending(id: string) {
  try { sessionStorage.removeItem(pendingKey(id)) } catch { /* No durable browser copy was available. */ }
}

function stockError(failure: unknown): string {
  const error = asApiError(failure)
  if (error.category === 'unavailable') return 'Stock is temporarily unavailable. Please try again.'
  return errorMessage(error)
}

export function StockPage() {
  const [params, setParams] = useSearchParams()
  const itemId = params.get('item')
  const query = params.get('q') ?? ''
  return <section className="stock-page">
    <p className="eyebrow">Business · Stock</p>
    {itemId
      ? <StockDetail key={itemId} id={itemId} query={query} />
      : <StockList key={query} query={query} onSearch={(value) => setParams(value ? { q: value } : {})} />}
  </section>
}

function StockList({ query, onSearch }: { query: string; onSearch: (value: string) => void }) {
  const auth = useAuth()
  const manager = auth.session?.capabilities.includes('commerce.manage') ?? false
  const client = useMemo(() => createBusinessStockClient(auth.handleSessionExpired), [auth.handleSessionExpired])
  const [input, setInput] = useState(query)
  const [items, setItems] = useState<StockItem[] | null>(null)
  const [hasMore, setHasMore] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const controller = useRef<AbortController | null>(null)
  const searchId = useId()
  const load = useCallback(async () => {
    controller.current?.abort()
    const current = new AbortController(); controller.current = current
    setLoading(true); setError(null)
    try {
      const result = await client.search(query, null, current.signal)
      if (!current.signal.aborted) { setItems(result.items); setHasMore(result.has_more) }
    } catch (failure) {
      if (!current.signal.aborted) { setItems(null); setError(stockError(failure)) }
    } finally { if (!current.signal.aborted) setLoading(false) }
  }, [client, query])
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load()
    return () => controller.current?.abort()
  }, [load])
  return <>
    <h1>Stock</h1>
    <form className="stock-search" onSubmit={(event) => { event.preventDefault(); onSearch(input.trim()) }}>
      <label htmlFor={searchId}>Product, variant, or SKU</label>
      <input id={searchId} value={input} maxLength={120} onChange={(event) => setInput(event.target.value)} />
      <button className="button button--primary">Search</button>
    </form>
    {loading && <p role="status">Loading stock…</p>}
    {error && <div role="alert"><p>{error}</p><button className="button button--secondary" onClick={() => void load()}>Retry</button></div>}
    {items && <>
      {!items.length && <p role="status">No matching variants.</p>}
      {hasMore && <p role="status">Showing the first 50 variants. Refine your search to find more.</p>}
      <ul className="stock-results">
        {items.map((item) => <li key={item.sellable_item_id}>
          <div>
            <Link to={`/business/stock?${new URLSearchParams({ ...(query ? { q: query } : {}), item: item.sellable_item_id })}`}>
              {item.product_name} · {item.model_label || 'Standard variant'}
            </Link>
            {item.sku && <p>SKU: {item.sku}</p>}
            {manager && <p>{item.product_active && item.variant_active ? 'Active' : 'Inactive'}</p>}
          </div>
          <div className="stock-result-facts">
            <span>{availability[item.availability]}</span>
            {manager && item.quantity !== undefined && <span>Quantity: {item.quantity === null ? 'Unknown' : item.quantity}</span>}
            {manager && item.inventory_updated_at !== undefined && <span>Updated: {item.inventory_updated_at ? new Date(item.inventory_updated_at).toLocaleString() : 'Never'}</span>}
          </div>
        </li>)}
      </ul>
    </>}
  </>
}

function StockDetail({ id, query }: { id: string; query: string }) {
  const auth = useAuth()
  const manager = auth.session?.capabilities.includes('commerce.manage') ?? false
  const client = useMemo(() => createBusinessStockClient(auth.handleSessionExpired), [auth.handleSessionExpired])
  const [item, setItem] = useState<StockItem | null>(null)
  const [activity, setActivity] = useState<StockMovement[]>([])
  const [activityError, setActivityError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [action, setAction] = useState<'receive' | 'adjust' | null>(() => readPending(id)?.kind ?? null)
  const [value, setValue] = useState('')
  const [reason, setReason] = useState('')
  const [review, setReview] = useState<Review | null>(() => readPending(id))
  const [uncertain, setUncertain] = useState(() => readPending(id) !== null)
  const [reauth, setReauth] = useState(false)
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [denied, setDenied] = useState(false)
  const controller = useRef<AbortController | null>(null)
  const operation = useRef<AbortController | null>(null)
  const fieldId = useId()
  const reviewHeading = useRef<HTMLHeadingElement>(null)
  const passwordField = useRef<HTMLInputElement>(null)
  const load = useCallback(async () => {
    controller.current?.abort()
    const current = new AbortController(); controller.current = current
    setLoading(true); setError(null); setActivityError(null)
    try {
      const result = await client.search('', id, current.signal)
      if (current.signal.aborted) return false
      if (result.items.length !== 1) { setItem(null); setError('This variant is not available to inspect.'); return false }
      setItem(result.items[0])
      if (manager) {
        try {
          const history = await client.activity(id, current.signal)
          if (!current.signal.aborted) {
            setActivity(history.items)
            const pending = readPending(id)
            if (pending && history.items.some((entry) => entry.operation_key === pending.operation_key)) {
              clearPending(id); setReview(null); setAction(null); setUncertain(false)
              setNotice('Stock change confirmed. Showing current stock.')
            }
          }
        } catch (failure) {
          if (!current.signal.aborted) setActivityError(stockError(failure))
        }
      }
      return !current.signal.aborted
    } catch (failure) {
      if (!current.signal.aborted) { setItem(null); setError(stockError(failure)) }
      return false
    } finally { if (!current.signal.aborted) setLoading(false) }
  }, [client, id, manager])
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load()
    return () => { controller.current?.abort(); operation.current?.abort() }
  }, [load])
  useEffect(() => { if (review && !uncertain) reviewHeading.current?.focus() }, [review, uncertain])
  useEffect(() => { if (reauth) passwordField.current?.focus() }, [reauth])

  const prepare = (event: FormEvent) => {
    event.preventDefault(); setError(null)
    if (!item || !action || item.quantity === undefined || item.inventory_updated_at === undefined) return
    const entered = value.trim()
    if (!/^\d+$/.test(entered) || Number(entered) > maxQuantity || (action === 'receive' && Number(entered) === 0)) {
      setError(action === 'receive' ? 'Enter a positive whole amount to receive.' : 'Enter a whole corrected quantity from 0 to 2147483647.'); return
    }
    const number = Number(entered)
    if (action === 'receive' && item.quantity === null) { setError('Stock is unconfirmed. Adjust to a verified count first.'); return }
    if (action === 'receive' && item.quantity !== null && item.quantity + number > maxQuantity) {
      setError('The resulting quantity exceeds the allowed limit.'); return
    }
    const normalizedReason = reason.trim()
    if (action === 'adjust' && (!normalizedReason || normalizedReason.length > 500)) {
      setError('Enter a correction reason of at most 500 characters.'); return
    }
    setReview({ kind: action, value: number, reason: normalizedReason,
      expected_quantity: item.quantity, expected_updated_at: item.inventory_updated_at,
      operation_key: crypto.randomUUID() })
  }
  const confirm = async () => {
    if (!review || busy || denied) return
    const expiry = auth.session?.recent_reauthentication_expires_at_epoch
    if (expiry == null || expiry < Math.floor(Date.now() / 1000)) { setReauth(true); return }
    const current = new AbortController(); operation.current = current
    setBusy(true); setError(null); setNotice(null)
    savePending(id, review)
    try {
      const csrf = await auth.getCsrfForMutation()
      if (current.signal.aborted) return
      const reviewed = { expected_quantity: review.expected_quantity,
        expected_updated_at: review.expected_updated_at, operation_key: review.operation_key }
      if (review.kind === 'receive') await client.receive(id, review.value, reviewed, csrf, current.signal)
      else await client.adjust(id, review.value, review.reason, reviewed, csrf, current.signal)
      if (current.signal.aborted) return
      clearPending(id); setReview(null); setAction(null); setUncertain(false)
      const refreshed = await load()
      if (!current.signal.aborted) setNotice(refreshed
        ? 'Stock change saved. Showing current stock.'
        : 'Stock change saved, but current stock could not be loaded. Refresh before another change.')
    } catch (failure) {
      if (current.signal.aborted) return
      const apiError = asApiError(failure)
      if (apiError.code === 'recent_reauthentication_required') setReauth(true)
      else if (apiError.code === 'STOCK_CONFLICT' || apiError.code === 'STOCK_QUANTITY_UNKNOWN') {
        clearPending(id); setReview(null); setAction(null); setUncertain(false)
        await load()
        if (!current.signal.aborted) setError(apiError.code === 'STOCK_QUANTITY_UNKNOWN'
          ? 'Stock is unconfirmed. Adjust to a verified count first.'
          : 'Stock changed since your review. Check the current quantity and start again.')
      } else if (apiError.category === 'forbidden') {
        clearPending(id); setReview(null); setAction(null); setUncertain(false); setDenied(true)
        setError('You do not have permission to manage stock.')
      } else if (apiError.status === 0 || apiError.status >= 500) {
        setUncertain(true)
        setError('The result could not be confirmed. Retry the same operation or refresh stock and activity before another change.')
      } else { clearPending(id); setError(errorMessage(apiError)) }
    } finally { if (!current.signal.aborted) setBusy(false) }
  }
  const confirmPassword = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true); setError(null)
    try {
      await auth.reauthenticate(password)
      setReauth(false); setNotice('Password confirmed. Review and confirm your stock change.')
    } catch (failure) { setError(errorMessage(asApiError(failure))) }
    finally { setPassword(''); setBusy(false) }
  }
  const cancel = () => { setAction(null); setReview(null); setReauth(false); setReason(''); setValue(''); setError(null) }
  const adminItem = manager && item && item.quantity !== undefined && item.inventory_updated_at !== undefined ? item : null
  const back = `/business/stock${query ? `?${new URLSearchParams({ q: query })}` : ''}`
  return <>
    <Link to={back}>Back to stock results</Link>
    {loading && <p role="status">Loading current stock…</p>}
    {error && <p role="alert">{error}</p>}
    {notice && <p role="status">{notice}</p>}
    {!item && !loading && <button className="button button--secondary" onClick={() => void load()}>Retry</button>}
    {item && <>
      <h1>{item.product_name} · {item.model_label || 'Standard variant'}</h1>
      {item.sku && <p>SKU: {item.sku}</p>}
      {manager && <p>{item.product_active && item.variant_active ? 'Active' : 'Inactive'}</p>}
      <dl className="stock-facts">
        <div><dt>Availability</dt><dd>{availability[item.availability]}</dd></div>
        {adminItem && <div><dt>Current quantity</dt><dd>{adminItem.quantity === null ? 'Unknown' : adminItem.quantity}</dd></div>}
        {adminItem && <div><dt>Last stock update</dt><dd>{adminItem.inventory_updated_at ? new Date(adminItem.inventory_updated_at).toLocaleString() : 'Never'}</dd></div>}
      </dl>
      <div className="stock-links">
        <Link to={`/business/products/${encodeURIComponent(item.product_id)}?variant=${encodeURIComponent(item.sellable_item_id)}`}>View product</Link>
        <button className="button button--secondary" disabled={busy || loading} onClick={() => void load()}>Refresh stock</button>
      </div>
      {adminItem && !denied && !action && <div className="stock-actions">
        <button className="button button--primary" onClick={() => { setAction('receive'); setValue(''); setError(null) }} disabled={adminItem.quantity === null}>Receive stock</button>
        <button className="button button--secondary" onClick={() => { setAction('adjust'); setValue(''); setReason(''); setError(null) }}>Adjust stock</button>
        {adminItem.quantity === null && <p>Set a verified count with Adjust before receiving stock.</p>}
      </div>}
      {adminItem && !denied && action && !review && <form className="stock-operation" onSubmit={prepare}>
        <h2>{action === 'receive' ? 'Receive stock' : 'Adjust stock'}</h2>
        <label htmlFor={`${fieldId}-quantity`}>{action === 'receive' ? 'Amount received' : 'Corrected quantity'}</label>
        <input id={`${fieldId}-quantity`} inputMode="numeric" value={value} maxLength={10} required disabled={busy}
          onChange={(event) => setValue(event.target.value)} />
        {action === 'adjust' && <><label htmlFor={`${fieldId}-reason`}>Reason for correction</label>
          <textarea id={`${fieldId}-reason`} value={reason} maxLength={500} required disabled={busy}
            onChange={(event) => setReason(event.target.value)} /></>}
        <div className="stock-actions"><button className="button button--primary" disabled={busy}>Review change</button>
          <button className="button button--secondary" type="button" disabled={busy} onClick={cancel}>Cancel</button></div>
      </form>}
      {adminItem && !denied && review && <section className="stock-operation" aria-label="Confirm stock change">
        <h2 ref={reviewHeading} tabIndex={-1}>Confirm {review.kind === 'receive' ? 'receipt' : 'adjustment'}</h2>
        <p>{review.expected_quantity === null ? 'Unknown' : review.expected_quantity}
          {' → '}{review.kind === 'receive' ? `+${review.value} → ${review.expected_quantity! + review.value}` : review.value}</p>
        {review.kind === 'adjust' && <p>Reason: {review.reason}</p>}
        {uncertain && <p>The previous attempt may have been saved. This retry uses the same operation key.</p>}
        <div className="stock-actions">
          <button className="button button--primary" disabled={busy || reauth} onClick={() => void confirm()}>{uncertain ? 'Retry same operation' : 'Confirm stock change'}</button>
          {!uncertain && <button className="button button--secondary" disabled={busy} onClick={() => setReview(null)}>Edit</button>}
          {!uncertain && <button className="button button--secondary" disabled={busy} onClick={cancel}>Cancel</button>}
        </div>
      </section>}
      {adminItem && reauth && <form className="stock-operation" onSubmit={(event) => void confirmPassword(event)}>
        <PasswordField ref={passwordField} id={`${fieldId}-password`} label="Confirm Administrator password" autoComplete="current-password"
          value={password} disabled={busy} required onChange={(event) => setPassword(event.target.value)} />
        <button className="button button--primary" disabled={busy}>Confirm password</button>
      </form>}
      {manager && <section className="stock-activity" aria-label="Recent stock activity">
        <h2>Recent stock activity</h2>
        {activityError && <p role="alert">{activityError}</p>}
        {!activityError && !activity.length && <p>No stock movements recorded yet.</p>}
        <ol>{activity.map((movement) => <li key={movement.movement_id}>
          <strong>{movement.operation_kind === 'receive' ? 'Receive' : 'Adjust'}</strong>
          <span>{movement.before_quantity === null ? 'Unknown' : movement.before_quantity} → {movement.after_quantity === null ? 'Unknown' : movement.after_quantity}</span>
          {movement.received_amount !== null && <span>+{movement.received_amount}</span>}
          {movement.reason && <span>{movement.reason}</span>}
          <span>Actor: {movement.actor_account_id}</span>
          <time dateTime={movement.occurred_at}>{new Date(movement.occurred_at).toLocaleString()}</time>
        </li>)}</ol>
      </section>}
    </>}
  </>
}
