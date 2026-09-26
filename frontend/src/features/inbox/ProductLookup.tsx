import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { createProductOfferClient, type ProductOffer } from '../../api/productOffers'
import { asApiError, errorMessage } from '../../api/errors'
import { useAuth } from '../../auth/AuthProvider'

const availability = {
  available: 'Available', out_of_stock: 'Unavailable', unknown: 'Availability unconfirmed',
}
const statuses = {
  sellable_now: 'Sellable now', availability_unconfirmed: 'Availability unconfirmed',
  out_of_stock: 'Unavailable', price_unavailable: 'Price not set', inactive: 'Inactive',
}
const reasons = {
  sellable_now: 'Active, priced and marked available',
  availability_unconfirmed: 'Availability has not been confirmed',
  inventory_out_of_stock: 'Marked unavailable', price_unavailable: 'Current USD price is not set',
  product_inactive: 'Product is inactive', sellable_item_inactive: 'Variant is inactive',
}
const name = (offer: ProductOffer) =>
  `${offer.product_name} — ${offer.model_label || 'Standard variant'}`

function OfferImage({ offer, large = false }: { offer: ProductOffer; large?: boolean }) {
  const [failed, setFailed] = useState(false)
  return offer.primary_media && !failed ? (
    <img className={`product-lookup__image${large ? ' product-lookup__image--large' : ''}`}
      src={offer.primary_media.asset_url} alt={offer.primary_media.alt_text ?? ''}
      loading="lazy" onError={() => setFailed(true)} />
  ) : (
    <span className={`product-lookup__image product-lookup__placeholder${large ? ' product-lookup__image--large' : ''}`}>No image</span>
  )
}

type Request = { kind: 'search'; query: string } | { kind: 'detail'; id: string }

/** Keyed by conversation in the workspace: no selection or reply state is persisted. */
export function ProductLookup() {
  const auth = useAuth()
  const client = useMemo(() => createProductOfferClient(auth.handleSessionExpired), [auth.handleSessionExpired])
  const titleId = useId()
  const inputId = useId()
  const trigger = useRef<HTMLButtonElement>(null)
  const panel = useRef<HTMLElement>(null)
  const input = useRef<HTMLInputElement>(null)
  const detailHeading = useRef<HTMLHeadingElement>(null)
  const controller = useRef<AbortController | null>(null)
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<ProductOffer[] | null>(null)
  const [offer, setOffer] = useState<ProductOffer | null>(null)
  const [request, setRequest] = useState<Request | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const close = useCallback(() => {
    controller.current?.abort()
    setOpen(false)
    setLoading(false)
    setError(null)
    setOffer(null)
    setResults(null)
    setRequest(null)
  }, [])
  useEffect(() => () => controller.current?.abort(), [])

  // Match the existing Inbox drawer's modal isolation and focus recovery.
  useEffect(() => {
    if (!open) return
    const frame = document.querySelector<HTMLElement>('.app-frame')
    const previousHidden = frame?.getAttribute('aria-hidden')
    const previousOverflow = document.body.style.overflow
    frame?.setAttribute('inert', '')
    frame?.setAttribute('aria-hidden', 'true')
    document.body.style.overflow = 'hidden'
    input.current?.focus()
    const returnTarget = trigger.current
    const focusFirst = () => panel.current?.querySelector<HTMLElement>('button, input')?.focus()
    const keyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { event.preventDefault(); close(); return }
      if (event.key !== 'Tab') return
      const elements = Array.from(panel.current?.querySelectorAll<HTMLElement>(
        'button:not([disabled]), input:not([disabled]), [tabindex="0"]',
      ) ?? [])
      const first = elements[0]
      const last = elements[elements.length - 1]
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
    }
    const focusIn = (event: FocusEvent) => {
      if (!panel.current?.contains(event.target as Node)) focusFirst()
    }
    document.addEventListener('keydown', keyDown)
    document.addEventListener('focusin', focusIn)
    return () => {
      document.removeEventListener('keydown', keyDown)
      document.removeEventListener('focusin', focusIn)
      frame?.removeAttribute('inert')
      if (previousHidden == null) frame?.removeAttribute('aria-hidden')
      else frame?.setAttribute('aria-hidden', previousHidden)
      document.body.style.overflow = previousOverflow
      queueMicrotask(() => { if (returnTarget?.isConnected) returnTarget.focus() })
    }
  }, [open, close])

  useEffect(() => {
    if (offer) detailHeading.current?.focus()
  }, [offer])

  const run = async (next: Request) => {
    controller.current?.abort()
    const current = new AbortController()
    controller.current = current
    setRequest(next)
    setLoading(true)
    setError(null)
    setOffer(null)
    if (next.kind === 'search') setResults(null)
    try {
      if (next.kind === 'search') {
        const response = await client.search(next.query, current.signal)
        if (!current.signal.aborted) setResults(response.items)
      } else {
        const response = await client.detail(next.id, current.signal)
        if (!current.signal.aborted) setOffer(response)
      }
    } catch (unknownError) {
      if (!current.signal.aborted) {
        const failure = asApiError(unknownError)
        setError(failure.status === 404 ? 'This product variant is no longer available to inspect.' : errorMessage(failure))
      }
    } finally {
      if (!current.signal.aborted) setLoading(false)
    }
  }
  const back = () => {
    controller.current?.abort()
    setRequest(null)
    setOffer(null)
    setLoading(false)
    setError(null)
    input.current?.focus()
  }
  const detailMode = request?.kind === 'detail'

  return <>
    <button className="button button--secondary product-lookup-trigger" type="button" ref={trigger}
      aria-haspopup="dialog" aria-expanded={open}
      onClick={() => { back(); setOpen(true) }}>Find a product</button>
    {open && createPortal(
      <div className="context-drawer" role="presentation">
        <section className="context-drawer__panel product-lookup" role="dialog" aria-modal="true"
          aria-labelledby={titleId} ref={panel} tabIndex={-1}>
          <header className="context-drawer__header">
            <h2 id={titleId}>Find a product</h2>
            <button className="button button--secondary" type="button" onClick={close}>Back to conversation</button>
          </header>
          <div className="context-drawer__content">
            <form className="product-lookup__search" onSubmit={(event) => {
              event.preventDefault()
              if (query.trim()) void run({ kind: 'search', query: query.trim() })
            }}>
              <label htmlFor={inputId}>Product name, model or SKU</label>
              <input id={inputId} ref={input} value={query} required maxLength={120}
                onChange={(event) => setQuery(event.target.value)} />
              <button className="button button--secondary" type="submit" disabled={loading || !query.trim()}>Search</button>
            </form>
            {detailMode && <button className="button button--secondary" type="button" onClick={back}>Back to results</button>}
            {loading && <p role="status">{detailMode ? 'Loading current product details…' : 'Searching products…'}</p>}
            {error && <div role="alert"><p>{error}</p>
              <button className="button button--secondary" type="button" onClick={() => { if (request) void run(request) }}>Retry</button>
            </div>}
            {!loading && !error && !detailMode && results && <>
              <p role="status">{results.length ? `${results.length} matching variants` : 'No matching active variants. Try another name, model or SKU.'}</p>
              {results.length === 20 && <p>Showing up to 20 variants. Refine your search for more specific results.</p>}
              <ul className="product-lookup__results">
                {results.map((item) => <li key={item.sellable_item_id}>
                  <OfferImage key={item.primary_media?.asset_url ?? 'none'} offer={item} />
                  <div><button className="button button--secondary" type="button"
                    onClick={() => void run({ kind: 'detail', id: item.sellable_item_id })}>{name(item)}</button>
                    {item.sku && <p>SKU: {item.sku}</p>}
                    <p>{item.current_usd_price === null ? 'Price not set' : `USD ${item.current_usd_price}`}</p>
                    <p>{availability[item.inventory_status]} · {statuses[item.offer_status]}</p>
                  </div>
                </li>)}
              </ul>
            </>}
            {!loading && !error && offer && <article>
              <h3 ref={detailHeading} tabIndex={-1}>{name(offer)}</h3>
              <OfferImage key={offer.primary_media?.asset_url ?? 'none'} offer={offer} large />
              {offer.sku && <p>SKU: {offer.sku}</p>}
              <p className="product-lookup__description">{offer.description}</p>
              <dl className="context-details">
                <div><dt>Current price</dt><dd>{offer.current_usd_price === null ? 'Price not set' : `USD ${offer.current_usd_price}`}</dd></div>
                <div><dt>CDF quote</dt><dd>{offer.cdf_quote_status === 'available' && offer.derived_cdf_quote
                  ? `CDF ${offer.derived_cdf_quote.cdf_amount}` : 'CDF quote unavailable'}</dd></div>
                <div><dt>Availability</dt><dd>{availability[offer.inventory_status]}</dd></div>
                <div><dt>Commercial status</dt><dd>{statuses[offer.offer_status]}</dd></div>
                <div><dt>Reason</dt><dd>{reasons[offer.reason_code]}</dd></div>
              </dl>
              {Object.keys(offer.attributes).length > 0 && <>
                <h4>Characteristics</h4><dl className="context-details">
                  {Object.entries(offer.attributes).map(([key, value]) => <div key={key}><dt>{key.replaceAll('_', ' ')}</dt><dd>{String(value)}</dd></div>)}
                </dl>
              </>}
              <p>Read at <time dateTime={offer.read_at}>{new Date(offer.read_at).toLocaleString()}</time></p>
              <button className="button button--secondary" type="button"
                onClick={() => void run({ kind: 'detail', id: offer.sellable_item_id })}>Refresh</button>
            </article>}
          </div>
        </section>
      </div>, document.body,
    )}
  </>
}
