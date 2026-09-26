import { useCallback, useEffect, useId, useMemo, useRef, useState, type FormEvent } from 'react'
import { Link, useParams } from 'react-router-dom'
import { createBusinessProductsClient, type BusinessProductDetail } from '../../api/businessProducts'
import { asApiError, errorMessage } from '../../api/errors'
import { createProductOfferClient, type ProductOffer } from '../../api/productOffers'
import { useAuth } from '../../auth/AuthProvider'
import { PasswordField } from '../../components/PasswordField'

const availability = { available: 'Available', out_of_stock: 'Unavailable', unknown: 'Availability unconfirmed' }
const statuses = { sellable_now: 'Sellable now', out_of_stock: 'Unavailable', availability_unconfirmed: 'Availability unconfirmed', price_unavailable: 'Price not set', inactive: 'Inactive' }
const reasons = {
  sellable_now: 'Active, priced and marked available', inventory_out_of_stock: 'Marked unavailable',
  availability_unconfirmed: 'Availability has not been confirmed', price_unavailable: 'Current USD price is not set',
  product_inactive: 'Product is inactive', sellable_item_inactive: 'Variant is inactive',
}

function productError(failure: unknown): string {
  const error = asApiError(failure)
  if (error.status === 404) return 'This product is no longer available to inspect.'
  if (error.category === 'unavailable') return 'Products are temporarily unavailable. Please try again.'
  return errorMessage(error)
}

function ProductImage({ media, large = false }: { media: ProductOffer['primary_media']; large?: boolean }) {
  const [failed, setFailed] = useState(false)
  return media && !failed
    ? <img className={`products-image${large ? ' products-image--large' : ''}`} src={media.asset_url}
      alt={media.alt_text ?? ''} loading="lazy" onError={() => setFailed(true)} />
    : <span className={`products-image products-placeholder${large ? ' products-image--large' : ''}`}>No image</span>
}

export function ProductsPage() {
  const { productId } = useParams()
  return <section className="products-page">
    <p className="eyebrow">Business · Products</p>
    {productId ? <ProductDetail key={productId} id={productId} /> : <ProductList />}
  </section>
}

function ProductList() {
  const auth = useAuth()
  const manager = auth.session?.capabilities.includes('commerce.manage') ?? false
  const client = useMemo(() => createBusinessProductsClient(auth.handleSessionExpired), [auth.handleSessionExpired])
  const [query, setQuery] = useState('')
  const [submitted, setSubmitted] = useState('')
  const [result, setResult] = useState<Awaited<ReturnType<typeof client.list>> | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const controller = useRef<AbortController | null>(null)
  const inputId = useId()
  const load = useCallback(async (value: string) => {
    controller.current?.abort()
    const current = new AbortController()
    controller.current = current
    setSubmitted(value); setLoading(true); setError(null); setResult(null)
    try {
      const data = await client.list(value, current.signal)
      if (!current.signal.aborted) setResult(data)
    } catch (failure) {
      if (!current.signal.aborted) setError(productError(failure))
    } finally {
      if (!current.signal.aborted) setLoading(false)
    }
  }, [client])
  useEffect(() => {
    // Initial browsing is cancelled when leaving the workspace.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load('')
    return () => controller.current?.abort()
  }, [load])
  return <>
    <h1>Products</h1>
    <form className="products-search" onSubmit={(event) => { event.preventDefault(); void load(query.trim()) }}>
      <label htmlFor={inputId}>Product name or category</label>
      <input id={inputId} value={query} maxLength={120} onChange={(event) => setQuery(event.target.value)} />
      <button className="button button--secondary" disabled={loading}>Search</button>
    </form>
    {loading && <p role="status">Loading products…</p>}
    {error && <div role="alert"><p>{error}</p><button className="button button--secondary" onClick={() => void load(submitted)}>Retry</button></div>}
    {result && <>
      {!result.items.length && <p role="status">{submitted ? 'No matching products.' : 'No products are available to inspect.'}</p>}
      {result.has_more && <p role="status">Showing the first 50 products. Search for a more specific name or category.</p>}
      <ul className="products-list">
        {result.items.map((product) => <li key={product.product_id}>
          <ProductImage key={product.primary_media?.asset_url ?? 'none'} media={product.primary_media} />
          <div><Link to={`/business/products/${product.product_id}`}>{product.name}</Link>
            <p>{product.category_code.replaceAll('_', ' ')}</p>
            {manager && <p>{product.active ? 'Active' : 'Inactive'}</p>}
          </div>
        </li>)}
      </ul>
    </>}
  </>
}

function ProductDetail({ id }: { id: string }) {
  const auth = useAuth()
  const manager = auth.session?.capabilities.includes('commerce.manage') ?? false
  const client = useMemo(() => createBusinessProductsClient(auth.handleSessionExpired), [auth.handleSessionExpired])
  const [product, setProduct] = useState<BusinessProductDetail | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const controller = useRef<AbortController | null>(null)
  const load = useCallback(async () => {
    controller.current?.abort()
    const current = new AbortController(); controller.current = current
    setLoading(true); setError(null); setProduct(null); setSelected(null)
    try {
      const data = await client.detail(id, current.signal)
      if (!current.signal.aborted) setProduct(data)
    } catch (failure) {
      if (!current.signal.aborted) setError(productError(failure))
    } finally { if (!current.signal.aborted) setLoading(false) }
  }, [client, id])
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load()
    return () => controller.current?.abort()
  }, [load])
  return <>
    <Link to="/business/products">Back to products</Link>
    {loading && <p role="status">Loading product…</p>}
    {error && <div role="alert"><p>{error}</p><button className="button button--secondary" onClick={() => void load()}>Retry</button></div>}
    {product && <>
      <h1>{product.name}</h1>
      <p>{product.category_code.replaceAll('_', ' ')}</p>
      {manager && <p>{product.active ? 'Active product' : 'Inactive product'}</p>}
      <ProductImage key={product.primary_media?.asset_url ?? 'none'} media={product.primary_media} large />
      <p className="products-description">{product.description}</p>
      <h2>Variants</h2>
      {!product.variants.length && <p>No variants have been added.</p>}
      {product.has_more_variants && <p role="status">Showing the first 200 variants. Additional variants are not shown.</p>}
      <ul className="products-variants">
        {product.variants.map((variant) => <li key={variant.sellable_item_id}>
          <button className="button button--secondary" aria-pressed={selected === variant.sellable_item_id}
            onClick={() => setSelected(variant.sellable_item_id)}>
            {variant.model_label || 'Standard variant'}{variant.sku ? ` · ${variant.sku}` : ''}
          </button>
          {manager && <span>{variant.active ? 'Active variant' : 'Inactive variant'}</span>}
        </li>)}
      </ul>
      {selected && <VariantDetail key={selected} id={selected} />}
    </>}
  </>
}

function VariantDetail({ id }: { id: string }) {
  const auth = useAuth()
  const manager = auth.session?.capabilities.includes('commerce.manage') ?? false
  const offers = useMemo(() => createProductOfferClient(auth.handleSessionExpired), [auth.handleSessionExpired])
  const commerce = useMemo(() => createBusinessProductsClient(auth.handleSessionExpired), [auth.handleSessionExpired])
  const [offer, setOffer] = useState<ProductOffer | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)
  const [action, setAction] = useState<'price' | 'availability' | null>(null)
  const [amount, setAmount] = useState('')
  const [status, setStatus] = useState<ProductOffer['inventory_status']>('unknown')
  const [busy, setBusy] = useState(false)
  const [needsRefresh, setNeedsRefresh] = useState(false)
  const [denied, setDenied] = useState(false)
  const [reauth, setReauth] = useState(false)
  const [password, setPassword] = useState('')
  const controller = useRef<AbortController | null>(null)
  const operation = useRef<AbortController | null>(null)
  const heading = useRef<HTMLHeadingElement>(null)
  const formField = useRef<HTMLInputElement | HTMLSelectElement | null>(null)
  const passwordField = useRef<HTMLInputElement>(null)
  const actionTrigger = useRef<HTMLButtonElement | null>(null)
  const fieldId = useId()
  const load = useCallback(async () => {
    controller.current?.abort()
    const current = new AbortController(); controller.current = current
    setLoading(true); setError(null); setSuccess(null); setOffer(null)
    try {
      const data = await offers.detail(id, current.signal)
      if (!current.signal.aborted) { setOffer(data); setNeedsRefresh(false); return true }
    } catch (failure) {
      if (!current.signal.aborted) { setError(productError(failure)); setNeedsRefresh(true) }
    } finally { if (!current.signal.aborted) setLoading(false) }
    return false
  }, [offers, id])
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load()
    return () => { controller.current?.abort(); operation.current?.abort() }
  }, [load])
  useEffect(() => { if (offer) heading.current?.focus() }, [offer])
  useEffect(() => {
    if (reauth) passwordField.current?.focus()
    else if (action) formField.current?.focus()
  }, [action, reauth])

  const save = async (event: FormEvent) => {
    event.preventDefault()
    if (!manager || busy || needsRefresh || denied || !action || !offer) return
    const value = amount.trim()
    if (action === 'price' && (!/^\d{1,10}(?:\.\d{1,2})?$/.test(value) || /^0+(?:\.0+)?$/.test(value))) {
      setError('Enter a positive USD price with at most two decimal places.'); return
    }
    const expiry = auth.session?.recent_reauthentication_expires_at_epoch
    if (expiry == null || expiry < Math.floor(Date.now() / 1000)) { setReauth(true); return }
    const current = new AbortController(); operation.current = current
    setBusy(true); setError(null); setSuccess(null)
    let saved = false
    try {
      const csrf = await auth.getCsrfForMutation()
      if (current.signal.aborted) return
      if (action === 'price') await commerce.changePrice(id, value, csrf, current.signal)
      else await commerce.setAvailability(id, status, csrf, current.signal)
      saved = true
      if (current.signal.aborted) return
      setAction(null)
      const refreshed = await load()
      if (!current.signal.aborted) setSuccess(refreshed
        ? 'Change saved. Showing refreshed current facts.'
        : 'Change saved, but current facts could not be loaded. Refresh before another change.')
    } catch (failure) {
      if (current.signal.aborted) return
      const apiError = asApiError(failure)
      if (apiError.code === 'recent_reauthentication_required') setReauth(true)
      else if (apiError.category === 'forbidden') { setDenied(true); setAction(null); setError('You do not have permission to maintain products.') }
      else if (apiError.status === 0 || apiError.status >= 500 || saved) {
        setNeedsRefresh(true); setOffer(null)
        setError('The change could not be confirmed. Refresh current facts before retrying; the change may already have been saved.')
      } else setError(errorMessage(apiError))
    } finally { if (!current.signal.aborted) setBusy(false) }
  }
  const confirmPassword = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true); setError(null)
    const current = new AbortController(); operation.current = current
    try {
      await auth.reauthenticate(password)
      if (!current.signal.aborted) { setReauth(false); setSuccess('Password confirmed. Review and save your change.') }
    } catch (failure) { if (!current.signal.aborted) setError(errorMessage(asApiError(failure))) }
    finally { if (!current.signal.aborted) { setBusy(false); setPassword('') } }
  }
  const cancelAction = () => {
    setAction(null); setReauth(false); setPassword('')
    const target = actionTrigger.current
    queueMicrotask(() => { if (target?.isConnected) target.focus() })
  }
  return <section className="products-variant-detail" aria-label="Selected variant">
    {loading && <p role="status">Loading current variant facts…</p>}
    {error && <p role="alert">{error}</p>}
    {success && <p role="status">{success}</p>}
    <button className="button button--secondary" disabled={busy || loading} onClick={() => void load()}>Refresh current facts</button>
    {offer && <>
      <h2 ref={heading} tabIndex={-1}>{offer.model_label || 'Standard variant'}</h2>
      {offer.sku && <p>SKU: {offer.sku}</p>}
      <ProductImage key={offer.primary_media?.asset_url ?? 'none'} media={offer.primary_media} large />
      <dl className="context-details">
        {Object.entries(offer.attributes).map(([key, value]) => <div key={key}><dt>{key.replaceAll('_', ' ')}</dt><dd>{String(value)}</dd></div>)}
        <div><dt>Current price</dt><dd>{offer.current_usd_price === null ? 'Price not set' : `USD ${offer.current_usd_price}`}</dd></div>
        <div><dt>CDF quote</dt><dd>{offer.cdf_quote_status === 'available' && offer.derived_cdf_quote ? `CDF ${offer.derived_cdf_quote.cdf_amount}` : 'CDF quote unavailable'}</dd></div>
        <div><dt>Availability</dt><dd>{availability[offer.inventory_status]}</dd></div>
        <div><dt>Commercial status</dt><dd>{statuses[offer.offer_status]}</dd></div>
        <div><dt>Reason</dt><dd>{reasons[offer.reason_code]}</dd></div>
        <div><dt>Checked</dt><dd>{new Date(offer.read_at).toLocaleString()}</dd></div>
      </dl>
      {manager && !denied && <div className="products-actions">
        <button className="button button--secondary" disabled={busy || needsRefresh} onClick={(event) => { actionTrigger.current = event.currentTarget; setAction('price'); setAmount(offer.current_usd_price ?? ''); setReauth(false); setError(null); setSuccess(null) }}>Change price</button>
        <button className="button button--secondary" disabled={busy || needsRefresh} onClick={(event) => { actionTrigger.current = event.currentTarget; setAction('availability'); setStatus(offer.inventory_status); setReauth(false); setError(null); setSuccess(null) }}>Set availability</button>
      </div>}
    </>}
    {manager && !denied && action && <form className="products-write" onSubmit={(event) => void save(event)}>
      {action === 'price' ? <><label htmlFor={fieldId}>New USD price</label>
        <input id={fieldId} ref={(element) => { formField.current = element }} inputMode="decimal" value={amount} maxLength={13} required disabled={busy}
          onChange={(event) => setAmount(event.target.value)} /></>
        : <><label htmlFor={fieldId}>Availability</label><select id={fieldId} ref={(element) => { formField.current = element }} value={status} disabled={busy}
          onChange={(event) => setStatus(event.target.value as ProductOffer['inventory_status'])}>
          <option value="available">Available</option><option value="out_of_stock">Unavailable</option><option value="unknown">Availability unconfirmed</option>
        </select></>}
      <button className="button button--primary" disabled={busy || loading || needsRefresh || reauth}>Save {action === 'price' ? 'price' : 'availability'}</button>
      <button className="button button--secondary" type="button" disabled={busy} onClick={cancelAction}>Cancel</button>
    </form>}
    {manager && !denied && reauth && <form className="products-write" onSubmit={(event) => void confirmPassword(event)}>
      <PasswordField ref={passwordField} id={`${fieldId}-password`} label="Confirm Administrator password" autoComplete="current-password"
        value={password} disabled={busy} required onChange={(event) => setPassword(event.target.value)} />
      <button className="button button--primary" disabled={busy}>Confirm password</button>
    </form>}
  </section>
}
