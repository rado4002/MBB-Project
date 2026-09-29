import { useCallback, useEffect, useId, useMemo, useRef, useState, type FormEvent } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { createBusinessProductsClient, type BusinessProductDetail, type ProductCommercialReview } from '../../api/businessProducts'
import { asApiError, errorMessage } from '../../api/errors'
import { createProductOfferClient, type ProductOffer } from '../../api/productOffers'
import { useAuth } from '../../auth/AuthProvider'
import { PasswordField } from '../../components/PasswordField'

const availability = { available: 'Available', out_of_stock: 'Out of stock', unknown: 'Availability unconfirmed' }
const statuses = { sellable_now: 'Sellable now', out_of_stock: 'Out of stock', availability_unconfirmed: 'Availability unconfirmed', price_unavailable: 'Price not set', inactive: 'Inactive' }
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

function uncertainCreate(error: ReturnType<typeof asApiError>): boolean {
  return error.status === 0 || error.status >= 500 || error.code === 'malformed_success_response'
}

function returnedId(value: unknown): value is string {
  return typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value)
}

function CreationPassword({ onConfirmed, onCancel }: { onConfirmed: () => void; onCancel: () => void }) {
  const auth = useAuth()
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const id = useId()
  const field = useRef<HTMLInputElement>(null)
  useEffect(() => { field.current?.focus() }, [])
  const confirm = async (event: FormEvent) => {
    event.preventDefault()
    if (busy) return
    setBusy(true); setError(null)
    try {
      await auth.reauthenticate(password)
      onConfirmed()
    } catch (failure) { setError(errorMessage(asApiError(failure))) }
    finally { setPassword(''); setBusy(false) }
  }
  return <form className="products-setup" onSubmit={(event) => void confirm(event)}>
    <PasswordField ref={field} id={id} label="Confirm Administrator password" autoComplete="current-password"
      value={password} disabled={busy} required onChange={(event) => setPassword(event.target.value)} />
    {error && <p role="alert">{error}</p>}
    <div className="products-setup-actions">
      <button className="button button--primary" disabled={busy}>Confirm password</button>
      <button className="button button--secondary" type="button" disabled={busy} onClick={onCancel}>Back to form</button>
    </div>
  </form>
}

function ProductCreateForm({ onCreated, onCancel, onReview }: {
  onCreated: (id: string, name: string) => void; onCancel: () => void; onReview: () => void
}) {
  const auth = useAuth()
  const client = useMemo(() => createBusinessProductsClient(auth.handleSessionExpired), [auth.handleSessionExpired])
  const [name, setName] = useState('')
  const [category, setCategory] = useState('')
  const [description, setDescription] = useState('')
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({})
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [reauth, setReauth] = useState(false)
  const [uncertain, setUncertain] = useState(false)
  const id = useId()
  const nameField = useRef<HTMLInputElement>(null)
  useEffect(() => { nameField.current?.focus() }, [])
  const save = async (event: FormEvent) => {
    event.preventDefault()
    if (busy || uncertain || reauth) return
    const normalizedName = name.trim()
    const normalizedCategory = category.trim().toLowerCase()
    const normalizedDescription = description.trim()
    const errors: Record<string, string> = {}
    if (!normalizedName || normalizedName.length > 200) errors.name = 'Enter a product name of at most 200 characters.'
    if (!/^[a-z][a-z0-9_]{0,49}$/.test(normalizedCategory)) errors.category = 'Use up to 50 lowercase letters, numbers, or underscores, starting with a letter.'
    if (!normalizedDescription || normalizedDescription.length > 4000) errors.description = 'Enter a description of at most 4000 characters.'
    setFieldErrors(errors); setError(null)
    if (Object.keys(errors).length) return
    const expiry = auth.session?.recent_reauthentication_expires_at_epoch
    if (expiry == null || expiry < Math.floor(Date.now() / 1000)) { setReauth(true); return }
    setBusy(true)
    let sent = false
    try {
      const csrf = await auth.getCsrfForMutation()
      sent = true
      const created = await client.createProduct({ name: normalizedName, category_code: normalizedCategory,
        description: normalizedDescription, active: false }, csrf)
      if (!returnedId(created.product_id)) throw new Error('Product create response has no valid ID')
      onCreated(created.product_id, normalizedName)
    } catch (failure) {
      const apiError = asApiError(failure)
      if (apiError.code === 'recent_reauthentication_required') setReauth(true)
      else if (sent && uncertainCreate(apiError)) {
        setUncertain(true)
        setError('Product creation could not be confirmed. It may already have been saved. Review the Products list and open the matching record before creating again; a matching name alone does not confirm it is the same product.')
      } else setError(errorMessage(apiError))
    } finally { setBusy(false) }
  }
  return <section className="products-secondary" aria-label="Add product">
    <h2>Add product</h2>
    <p>This product will remain inactive while you set it up.</p>
    {error && <p role="alert">{error}</p>}
    {uncertain && <button className="button button--secondary" onClick={onReview}>Refresh Products list</button>}
    <form className="products-setup" onSubmit={(event) => void save(event)} noValidate>
      <label htmlFor={`${id}-name`}>Product name</label>
      <input id={`${id}-name`} ref={nameField} value={name} maxLength={200} required disabled={busy || uncertain}
        aria-invalid={!!fieldErrors.name} aria-describedby={fieldErrors.name ? `${id}-name-error` : undefined}
        onChange={(event) => setName(event.target.value)} />
      {fieldErrors.name && <p id={`${id}-name-error`}>{fieldErrors.name}</p>}
      <label htmlFor={`${id}-category`}>Category code</label>
      <input id={`${id}-category`} value={category} maxLength={50} required disabled={busy || uncertain}
        aria-invalid={!!fieldErrors.category} aria-describedby={`${id}-category-hint${fieldErrors.category ? ` ${id}-category-error` : ''}`}
        onChange={(event) => setCategory(event.target.value)} />
      <p id={`${id}-category-hint`}>For example: air_fryer. Letters, numbers, and underscores.</p>
      {fieldErrors.category && <p id={`${id}-category-error`}>{fieldErrors.category}</p>}
      <label htmlFor={`${id}-description`}>Description</label>
      <textarea id={`${id}-description`} value={description} maxLength={4000} required disabled={busy || uncertain}
        aria-invalid={!!fieldErrors.description} aria-describedby={fieldErrors.description ? `${id}-description-error` : undefined}
        onChange={(event) => setDescription(event.target.value)} />
      {fieldErrors.description && <p id={`${id}-description-error`}>{fieldErrors.description}</p>}
      <div className="products-setup-actions">
        <button className="button button--primary" disabled={busy || uncertain || reauth}>Save product</button>
        <button className="button button--secondary" type="button" disabled={busy} onClick={onCancel}>Back to products</button>
      </div>
    </form>
    {reauth && <CreationPassword onConfirmed={() => { setReauth(false); setError('Password confirmed. Review and save the product.') }}
      onCancel={() => setReauth(false)} />}
  </section>
}

function FirstVariantForm({ productId, onCreated }: { productId: string; onCreated: (id: string) => void }) {
  const auth = useAuth()
  const client = useMemo(() => createBusinessProductsClient(auth.handleSessionExpired), [auth.handleSessionExpired])
  const [label, setLabel] = useState('')
  const [sku, setSku] = useState('')
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({})
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [reauth, setReauth] = useState(false)
  const [uncertain, setUncertain] = useState(false)
  const [review, setReview] = useState<BusinessProductDetail | null>(null)
  const id = useId()
  const labelField = useRef<HTMLInputElement>(null)
  useEffect(() => { labelField.current?.focus() }, [])
  const refresh = async () => {
    setError(null)
    try {
      const current = await client.detail(productId, new AbortController().signal)
      setReview(current)
    } catch (failure) { setError(productError(failure)) }
  }
  const save = async (event: FormEvent) => {
    event.preventDefault()
    if (busy || uncertain || reauth) return
    const normalizedLabel = label.trim()
    const normalizedSku = sku.trim().toUpperCase()
    const errors: Record<string, string> = {}
    if (normalizedLabel.length > 100) errors.label = 'Use at most 100 characters.'
    if (normalizedSku && !/^[A-Z0-9][A-Z0-9._-]{0,63}$/.test(normalizedSku)) errors.sku = 'Use up to 64 letters, numbers, periods, underscores, or hyphens.'
    setFieldErrors(errors); setError(null)
    if (Object.keys(errors).length) return
    const expiry = auth.session?.recent_reauthentication_expires_at_epoch
    if (expiry == null || expiry < Math.floor(Date.now() / 1000)) { setReauth(true); return }
    setBusy(true)
    let sent = false
    try {
      const csrf = await auth.getCsrfForMutation()
      sent = true
      const created = await client.createFirstVariant(productId, { model_label: normalizedLabel || null,
        sku: normalizedSku || null, attributes: {}, active: false }, csrf)
      if (!returnedId(created.sellable_item_id)) throw new Error('Variant create response has no valid ID')
      onCreated(created.sellable_item_id)
    } catch (failure) {
      const apiError = asApiError(failure)
      if (apiError.code === 'recent_reauthentication_required') setReauth(true)
      else if (sent && uncertainCreate(apiError)) {
        setUncertain(true)
        setError('Variant creation could not be confirmed. It may already have been saved. Inspect this Product before adding another variant.')
      } else if (apiError.status === 409) {
        setFieldErrors({ sku: 'This SKU is already in use. Choose another SKU or leave it blank.' })
      } else setError(errorMessage(apiError))
    } finally { setBusy(false) }
  }
  return <section className="products-secondary" aria-label="Add first variant">
    <h2>Add first variant</h2>
    <p>This variant will remain inactive. Price and availability can be set on Product detail afterward.</p>
    {error && <p role="alert">{error}</p>}
    {uncertain && <button className="button button--secondary" onClick={() => void refresh()}>Refresh Product detail</button>}
    {review && <div role="status">
      {review.variants.length ? <><p>Current variants on Product detail. Inspect them before deciding whether another is needed.</p>
        <ul>{review.variants.map((item) => <li key={item.sellable_item_id}>
          <Link to={`/business/products/${productId}?variant=${encodeURIComponent(item.sellable_item_id)}`}>
            {item.model_label || 'Standard variant'}{item.sku ? ` · ${item.sku}` : ''}
          </Link>
        </li>)}</ul></> : <p>No variant is currently listed. Review Product detail before resuming setup.</p>}
    </div>}
    <form className="products-setup" onSubmit={(event) => void save(event)} noValidate>
      <label htmlFor={`${id}-label`}>Variant name (optional)</label>
      <input id={`${id}-label`} ref={labelField} value={label} maxLength={100} disabled={busy || uncertain}
        aria-invalid={!!fieldErrors.label} aria-describedby={`${id}-label-hint${fieldErrors.label ? ` ${id}-label-error` : ''}`}
        onChange={(event) => setLabel(event.target.value)} />
      <p id={`${id}-label-hint`}>Leave blank for Standard variant.</p>
      {fieldErrors.label && <p id={`${id}-label-error`}>{fieldErrors.label}</p>}
      <label htmlFor={`${id}-sku`}>SKU (optional)</label>
      <input id={`${id}-sku`} value={sku} maxLength={64} disabled={busy || uncertain}
        aria-invalid={!!fieldErrors.sku} aria-describedby={fieldErrors.sku ? `${id}-sku-error` : undefined}
        onChange={(event) => setSku(event.target.value)} />
      {fieldErrors.sku && <p id={`${id}-sku-error`}>{fieldErrors.sku}</p>}
      <div className="products-setup-actions">
        <button className="button button--primary" disabled={busy || uncertain || reauth}>Save first variant</button>
        <Link className="button button--secondary" to={`/business/products/${productId}`}>View Product detail</Link>
      </div>
    </form>
    {reauth && <CreationPassword onConfirmed={() => { setReauth(false); setError('Password confirmed. Review and save the variant.') }}
      onCancel={() => setReauth(false)} />}
  </section>
}

function ProductList() {
  const auth = useAuth()
  const manager = auth.session?.capabilities.includes('commerce.manage') ?? false
  const navigate = useNavigate()
  const client = useMemo(() => createBusinessProductsClient(auth.handleSessionExpired), [auth.handleSessionExpired])
  const [query, setQuery] = useState('')
  const [submitted, setSubmitted] = useState('')
  const [result, setResult] = useState<Awaited<ReturnType<typeof client.list>> | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const controller = useRef<AbortController | null>(null)
  const inputId = useId()
  const [adding, setAdding] = useState(false)
  const [createdProduct, setCreatedProduct] = useState<{ id: string; name: string } | null>(null)
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
    {manager && !adding && <button className="button button--primary" onClick={() => setAdding(true)}>Add product</button>}
    {manager && adding && !createdProduct && <ProductCreateForm
      onCreated={(id, name) => setCreatedProduct({ id, name })}
      onCancel={() => { setAdding(false); void load('') }} onReview={() => void load('')} />}
    {manager && adding && createdProduct && <>
      <p role="status">{createdProduct.name} was saved inactive. Add its first variant or return to Product detail.</p>
      <FirstVariantForm productId={createdProduct.id} onCreated={(id) => navigate(`/business/products/${createdProduct.id}?variant=${encodeURIComponent(id)}`)} />
    </>}
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

function LifecycleAction({ kind, id, active, offer, onChanged }: {
  kind: 'product' | 'variant'; id: string; active: boolean; offer?: ProductOffer;
  onChanged: () => Promise<void>
}) {
  const auth = useAuth()
  const client = useMemo(() => createBusinessProductsClient(auth.handleSessionExpired), [auth.handleSessionExpired])
  const [pending, setPending] = useState(false)
  const [review, setReview] = useState<ProductCommercialReview | null>(null)
  const [reauth, setReauth] = useState(false)
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [denied, setDenied] = useState(false)
  const [uncertain, setUncertain] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const passwordField = useRef<HTMLInputElement>(null)
  const confirmButton = useRef<HTMLButtonElement>(null)
  const fieldId = useId()
  const label = `${active ? 'Deactivate' : 'Activate'} ${kind}`
  useEffect(() => {
    if (reauth) passwordField.current?.focus()
    else if (pending) confirmButton.current?.focus()
  }, [pending, reauth, review])
  const open = async () => {
    if (busy || uncertain) return
    setError(null)
    if (kind === 'product' && !active) {
      setBusy(true)
      try {
        const data = await client.commercialReview(id, 0, new AbortController().signal)
        setReview(data)
        setPending(true)
      } catch (failure) { setError(productError(failure)) }
      finally { setBusy(false) }
    } else setPending(true)
  }
  const loadMore = async () => {
    if (!review || !review.has_more || busy) return
    setBusy(true); setError(null)
    try {
      const page = await client.commercialReview(id, review.items.length, new AbortController().signal)
      setReview({ ...review, items: [...review.items, ...page.items], has_more: page.has_more })
    } catch (failure) { setError(productError(failure)) }
    finally { setBusy(false) }
  }
  const cancel = () => {
    setPending(false); setReview(null); setReauth(false); setPassword(''); setError(null)
    queueMicrotask(() => trigger.current?.focus())
  }
  const confirmPassword = async (event: FormEvent) => {
    event.preventDefault()
    setBusy(true); setError(null)
    try {
      await auth.reauthenticate(password)
      setReauth(false)
    } catch (failure) { setError(errorMessage(asApiError(failure))) }
    finally { setPassword(''); setBusy(false) }
  }
  const confirm = async () => {
    if (!pending || busy || uncertain || (kind === 'product' && !active && (!review || review.has_more))) return
    const expiry = auth.session?.recent_reauthentication_expires_at_epoch
    if (expiry == null || expiry < Math.floor(Date.now() / 1000)) { setReauth(true); return }
    setBusy(true); setError(null)
    let sent = false
    try {
      const csrf = await auth.getCsrfForMutation()
      sent = true
      const signal = new AbortController().signal
      if (kind === 'product') await client.setProductActive(id, !active, csrf, signal)
      else await client.setVariantActive(id, !active, csrf, signal)
      await onChanged()
    } catch (failure) {
      const apiError = asApiError(failure)
      if (apiError.code === 'recent_reauthentication_required') setReauth(true)
      else if (apiError.category === 'forbidden') { setDenied(true); setPending(false); setError('You do not have permission to maintain products.') }
      else if (sent && uncertainCreate(apiError)) {
        setUncertain(true); setPending(false)
        setError('The change could not be confirmed. Refresh Product detail before another change; it may already have been saved.')
        await onChanged()
      } else setError(errorMessage(apiError))
    } finally { setBusy(false) }
  }
  if (denied) return <p role="alert">{error}</p>
  return <section className="products-secondary" aria-label={`${kind} status control`}>
    {!pending && <button ref={trigger} className="button button--secondary" disabled={busy || uncertain} onClick={() => void open()}>{label}</button>}
    {error && <p role="alert">{error}</p>}
    {uncertain && <button className="button button--secondary" onClick={() => void onChanged()}>Refresh Product detail</button>}
    {pending && <div className="products-lifecycle-confirmation">
      <h3>{label}</h3>
      {kind === 'product' && active && <p>This stops the Product and all its variants from being offered. Product, variants, and history are preserved.</p>}
      {kind === 'variant' && active && <p>This stops only this variant from being offered. Sibling variants may continue. Its history is preserved.</p>}
      {kind === 'product' && !active && <>
        <p>Active variants may become offerable. Price and stock still determine each variant’s commercial status.</p>
        {review && <>
          <h4>Current variant commercial facts</h4>
          {!review.items.length && <p>No variants have been added.</p>}
          <ul className="products-lifecycle-list">{review.items.map((item) => <li key={item.sellable_item_id}>
            <strong>{item.model_label || 'Standard variant'}{item.sku ? ` · ${item.sku}` : ''}</strong>
            <span className="products-meta">{item.active ? 'Active variant' : 'Inactive variant'}</span>
            <dl>
              <div><dt>Price</dt><dd>{item.current_usd_price === null ? 'Price not set' : `USD ${item.current_usd_price}`}</dd></div>
              <div><dt>Availability</dt><dd>{availability[item.inventory_status]}</dd></div>
              <div><dt>Current Product Offer</dt><dd>{statuses[item.offer_status]}</dd></div>
            </dl>
          </li>)}</ul>
          {review.has_more && <button className="button button--secondary" disabled={busy} onClick={() => void loadMore()}>Load more variant facts</button>}
          <p className="products-meta">Review started: {new Date(review.read_at).toLocaleString()}</p>
        </>}
      </>}
      {kind === 'variant' && offer && <p>Current facts: {offer.current_usd_price === null ? 'Price not set' : `USD ${offer.current_usd_price}`} · {availability[offer.inventory_status]} · {statuses[offer.offer_status]}.</p>}
      {!reauth && <div className="products-setup-actions">
        <button ref={confirmButton} className="button button--primary" disabled={busy || (kind === 'product' && !active && (!review || review.has_more))} onClick={() => void confirm()}>Confirm {label.toLowerCase()}</button>
        <button className="button button--secondary" disabled={busy} onClick={cancel}>Cancel</button>
      </div>}
      {reauth && <form onSubmit={(event) => void confirmPassword(event)}>
        <PasswordField ref={passwordField} id={`${fieldId}-password`} label="Confirm Administrator password" autoComplete="current-password"
          value={password} disabled={busy} required onChange={(event) => setPassword(event.target.value)} />
        <div className="products-setup-actions">
          <button className="button button--primary" disabled={busy}>Confirm password</button>
          <button className="button button--secondary" type="button" disabled={busy} onClick={cancel}>Cancel</button>
        </div>
      </form>}
    </div>}
  </section>
}

function ProductDetail({ id }: { id: string }) {
  const auth = useAuth()
  const manager = auth.session?.capabilities.includes('commerce.manage') ?? false
  const navigate = useNavigate()
  const [params] = useSearchParams()
  const preferredVariantId = params.get('variant')
  const client = useMemo(() => createBusinessProductsClient(auth.handleSessionExpired), [auth.handleSessionExpired])
  const [product, setProduct] = useState<BusinessProductDetail | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const controller = useRef<AbortController | null>(null)
  const load = useCallback(async () => {
    controller.current?.abort()
    const current = new AbortController(); controller.current = current
    setLoading(true); setError(null); setNotice(null); setProduct(null)
    try {
      const data = await client.detail(id, current.signal)
      if (!current.signal.aborted) {
        setProduct(data)
        setSelected((previous) => data.variants.some((variant) => variant.sellable_item_id === previous)
          ? previous : (data.variants.some((variant) => variant.sellable_item_id === preferredVariantId) ? preferredVariantId : null))
      }
      return !current.signal.aborted
    } catch (failure) {
      if (!current.signal.aborted) setError(productError(failure))
      return false
    } finally { if (!current.signal.aborted) setLoading(false) }
  }, [client, id, preferredVariantId])
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load()
    return () => controller.current?.abort()
  }, [load])
  const selectedVariant = product?.variants.find((variant) => variant.sellable_item_id === selected)
  return <>
    <Link to="/business/products">Back to products</Link>
    {notice && <p role="status">{notice}</p>}
    {loading && <p role="status">Loading product…</p>}
    {error && <div role="alert"><p>{error}</p><button className="button button--secondary" onClick={() => void load()}>Retry</button></div>}
    {product && <>
      <header className="products-family">
        <ProductImage key={product.primary_media?.asset_url ?? 'none'} media={product.primary_media} />
        <div>
          <h1>{product.name}</h1>
          <p className="products-meta">{product.category_code.replaceAll('_', ' ')}</p>
          {manager && <p className="products-meta">{product.active ? 'Active product' : 'Inactive product'}</p>}
        </div>
      </header>
      {manager && <LifecycleAction kind="product" id={id} active={product.active}
        onChanged={async () => { if (await load()) setNotice('Showing refreshed Product detail.') }} />}
      <h2>Variants</h2>
      {product.variants.length > 0 && <p className="products-meta">Select a variant to inspect its current price and availability.</p>}
      {!product.variants.length && <p>No variants have been added.</p>}
      {manager && !product.active && !product.variants.length && <FirstVariantForm productId={id}
        onCreated={(itemId) => navigate(`/business/products/${id}?variant=${encodeURIComponent(itemId)}`)} />}
      {product.has_more_variants && <p role="status">Showing the first 200 variants. Additional variants are not shown.</p>}
      <ul className="products-variants">
        {product.variants.map((variant) => <li key={variant.sellable_item_id}>
          <button className="products-variant-choice" aria-pressed={selected === variant.sellable_item_id}
            aria-label={`${variant.model_label || 'Standard variant'}${variant.sku ? ` · ${variant.sku}` : ''}`}
            onClick={() => setSelected(variant.sellable_item_id)}>
            <span className="products-variant-name">{variant.model_label || 'Standard variant'}</span>
            {variant.sku && <span className="products-meta">{` · ${variant.sku}`}</span>}
            <span className="products-variant-marker" aria-hidden="true">{selected === variant.sellable_item_id ? 'Selected' : 'Select'}</span>
          </button>
          {manager && <span className="products-meta">{variant.active ? 'Active variant' : 'Inactive variant'}</span>}
        </li>)}
      </ul>
      {selectedVariant && <VariantDetail key={`${selectedVariant.sellable_item_id}-${product.active}-${selectedVariant.active}`} id={selectedVariant.sellable_item_id}
        active={selectedVariant.active}
        onChanged={async () => { if (await load()) setNotice('Showing refreshed Product detail.') }} />}
      <section className="products-secondary" aria-label="Product description">
        {product.description.length > 280
          ? <details><summary>Show product description</summary><p className="products-description">{product.description}</p></details>
          : <><h2>Product description</h2><p className="products-description">{product.description}</p></>}
      </section>
    </>}
  </>
}

function VariantDetail({ id, active, onChanged }: { id: string; active: boolean; onChanged: () => Promise<void> }) {
  const auth = useAuth()
  const manager = auth.session?.capabilities.includes('commerce.manage') ?? false
  const offers = useMemo(() => createProductOfferClient(auth.handleSessionExpired), [auth.handleSessionExpired])
  const commerce = useMemo(() => createBusinessProductsClient(auth.handleSessionExpired), [auth.handleSessionExpired])
  const [offer, setOffer] = useState<ProductOffer | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)
  const [action, setAction] = useState<'price' | 'stock' | null>(null)
  const [amount, setAmount] = useState('')
  const [quantity, setQuantity] = useState('')
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
    const value = (action === 'price' ? amount : quantity).trim()
    if (action === 'price' && (!/^\d{1,10}(?:\.\d{1,2})?$/.test(value) || /^0+(?:\.0+)?$/.test(value))) {
      setError('Enter a positive USD price with at most two decimal places.'); return
    }
    if (action === 'stock' && value !== '' && (!/^\d+$/.test(value) || Number(value) > 2147483647)) {
      setError('Enter a whole stock quantity from 0 to 2147483647, or leave it blank for unknown.'); return
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
      else await commerce.updateStockQuantity(id, value === '' ? null : Number(value), csrf, current.signal)
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
  const editStock = async (trigger: HTMLButtonElement) => {
    actionTrigger.current = trigger
    const current = new AbortController(); operation.current = current
    setBusy(true); setError(null); setSuccess(null)
    try {
      const inventory = await commerce.getInventory(id, current.signal)
      if (!current.signal.aborted) {
        setQuantity(inventory.quantity === null ? '' : String(inventory.quantity))
        setAction('stock'); setReauth(false)
      }
    } catch (failure) {
      if (!current.signal.aborted) setError(errorMessage(asApiError(failure)))
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
  const characteristics = offer && <dl className="context-details">
    {Object.entries(offer.attributes).map(([key, value]) => <div key={key}><dt>{key.replaceAll('_', ' ')}</dt><dd>{String(value)}</dd></div>)}
  </dl>
  return <section className="products-variant-detail" aria-label="Selected variant">
    {loading && <p role="status">Loading current variant facts…</p>}
    {error && <p role="alert">{error}</p>}
    {success && <p role="status">{success}</p>}
    <div className="products-detail-heading">
      {offer && <div><p className="products-meta">Selected variant</p>
        <h2 ref={heading} tabIndex={-1}>{offer.model_label || 'Standard variant'}</h2>
        {offer.sku && <p className="products-meta">SKU: {offer.sku}</p>}
      </div>}
      <button className="button button--secondary" disabled={busy || loading} onClick={() => void load()}>Refresh current facts</button>
    </div>
    {offer && <>
      {manager && !denied && <LifecycleAction kind="variant" id={id} active={active} offer={offer} onChanged={onChanged} />}
      <div className="products-commercial-layout">
        <section aria-label="Current commercial facts">
          <h3>Current commercial facts</h3>
          <dl className="products-facts">
            <div><dt>Current price</dt><dd className="products-price">{offer.current_usd_price === null ? 'Price not set' : `USD ${offer.current_usd_price}`}</dd>
              {manager && !denied && <dd className="products-fact-action"><button className="button button--secondary" disabled={busy || needsRefresh} onClick={(event) => { actionTrigger.current = event.currentTarget; setAction('price'); setAmount(offer.current_usd_price ?? ''); setReauth(false); setError(null); setSuccess(null) }}>Change price</button></dd>}
            </div>
            <div><dt>CDF quote</dt><dd>{offer.cdf_quote_status === 'available' && offer.derived_cdf_quote ? `CDF ${offer.derived_cdf_quote.cdf_amount}` : 'CDF quote unavailable'}</dd></div>
            <div><dt>Availability</dt><dd><span className="products-status">{availability[offer.inventory_status]}</span></dd>
              {manager && !denied && <dd className="products-fact-action"><button className="button button--secondary" disabled={busy || needsRefresh} onClick={(event) => void editStock(event.currentTarget)}>Update stock quantity</button></dd>}
            </div>
            <div><dt>Commercial status</dt><dd><span className="products-status">{statuses[offer.offer_status]}</span></dd>
              <dd className="products-fact-reason">{reasons[offer.reason_code]}</dd>
            </div>
          </dl>
          <p className="products-meta">Checked: {new Date(offer.read_at).toLocaleString()}</p>
        </section>
        <ProductImage key={offer.primary_media?.asset_url ?? 'none'} media={offer.primary_media} large />
      </div>
    </>}
    {manager && !denied && action && <form className="products-write" onSubmit={(event) => void save(event)}>
      {action === 'price' ? <><label htmlFor={fieldId}>New USD price</label>
        <input id={fieldId} ref={(element) => { formField.current = element }} inputMode="decimal" value={amount} maxLength={13} required disabled={busy}
          onChange={(event) => setAmount(event.target.value)} /></>
        : <><label htmlFor={fieldId}>Stock quantity</label><input id={fieldId} ref={(element) => { formField.current = element }} inputMode="numeric" type="number" min="0" max="2147483647" step="1" value={quantity} disabled={busy}
          onChange={(event) => setQuantity(event.target.value)} /><p className="products-meta">Leave blank when stock is unconfirmed.</p></>}
      <button className="button button--primary" disabled={busy || loading || needsRefresh || reauth}>Save {action === 'price' ? 'price' : 'stock quantity'}</button>
      <button className="button button--secondary" type="button" disabled={busy} onClick={cancelAction}>Cancel</button>
    </form>}
    {manager && !denied && reauth && <form className="products-write" onSubmit={(event) => void confirmPassword(event)}>
      <PasswordField ref={passwordField} id={`${fieldId}-password`} label="Confirm Administrator password" autoComplete="current-password"
        value={password} disabled={busy} required onChange={(event) => setPassword(event.target.value)} />
      <button className="button button--primary" disabled={busy}>Confirm password</button>
    </form>}
    {offer && Object.keys(offer.attributes).length > 0 && <section className="products-secondary" aria-label="Characteristics">
      {Object.keys(offer.attributes).length > 4
        ? <details><summary>Show characteristics ({Object.keys(offer.attributes).length})</summary>{characteristics}</details>
        : <><h3>Characteristics</h3>{characteristics}</>}
    </section>}
  </section>
}
