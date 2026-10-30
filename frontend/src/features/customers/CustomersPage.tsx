import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react'
import { Link, useLocation, useSearchParams } from 'react-router-dom'
import { createBusinessCustomersClient, type BusinessCustomerListResponse } from '../../api/businessCustomers'
import { asApiError, errorMessage } from '../../api/errors'
import { useAuth } from '../../auth/AuthProvider'
import { InlineAlert } from '../../components/InlineAlert'

const pageSize = 25

function label(value: string | null) {
  if (!value) return 'Not recorded'
  return value.split('_').map((part) => part.charAt(0).toUpperCase() + part.slice(1)).join(' ')
}

function timestamp(value: string) {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? 'Unavailable' : new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium', timeStyle: 'short',
  }).format(date)
}

export function CustomersPage() {
  const auth = useAuth()
  const location = useLocation()
  const [params, setParams] = useSearchParams()
  const query = params.get('q')?.slice(0, 100) ?? ''
  const rawOffset = Number(params.get('offset') ?? '0')
  const offset = Number.isInteger(rawOffset) && rawOffset >= 0 && rawOffset <= 10000 ? rawOffset : 0
  const [input, setInput] = useState(query)
  const [result, setResult] = useState<BusinessCustomerListResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const client = useMemo(() => createBusinessCustomersClient(auth.handleSessionExpired), [auth.handleSessionExpired])

  const load = useCallback(async (signal: AbortSignal) => {
    setResult(null); setError(null)
    try {
      const next = await client.list(query, offset, signal)
      if (!signal.aborted) setResult(next)
    } catch (failure) {
      if (!signal.aborted) setError(errorMessage(asApiError(failure)))
    }
  }, [client, offset, query])

  useEffect(() => {
    const controller = new AbortController()
    // Query-string state is the source of truth for a reloadable, shareable overview.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setInput(query)
    void load(controller.signal)
    return () => controller.abort()
  }, [load, query])

  const search = (event: FormEvent) => {
    event.preventDefault()
    const trimmed = input.trim()
    const digits = trimmed.replaceAll(/\D/g, '')
    const safeQuery = trimmed && /^[+\d\s().-]+$/.test(trimmed)
      ? digits.slice(-4)
      : trimmed
    const next = new URLSearchParams()
    if (safeQuery) next.set('q', safeQuery)
    setParams(next)
  }

  const page = (nextOffset: number) => {
    const next = new URLSearchParams(params)
    if (nextOffset) next.set('offset', String(nextOffset)); else next.delete('offset')
    setParams(next)
  }

  const detailLocation = (customerReference: string, conversationId: string | null) => {
    const detailParams = new URLSearchParams(location.search)
    if (conversationId) detailParams.set('conversation', conversationId)
    return {
      pathname: `/business/customers/ref/${encodeURIComponent(customerReference)}`,
      search: detailParams.toString() ? `?${detailParams.toString()}` : '',
    }
  }

  return <section className="customers-page" aria-labelledby="customers-title">
    <header className="business-page-heading">
      <div><p className="eyebrow">Customer relationships</p><h1 id="customers-title">Customers</h1></div>
    </header>
    <form className="customers-search" role="search" onSubmit={search}>
      <label htmlFor="customer-search">Find by name or last 4 phone digits</label>
      <div><input id="customer-search" value={input} maxLength={100} onChange={(event) => setInput(event.target.value)} /><button className="button button--primary">Search</button></div>
    </form>
    <div className="customers-results">
      {result ? <div className="customers-results-heading"><strong>{query ? 'Filtered Customers' : 'All Customers'}</strong><span>Page {Math.floor(offset / pageSize) + 1}</span></div> : null}
      {!result && !error ? <p role="status">Loading Customers…</p> : null}
      {error ? <InlineAlert>{error}<button type="button" className="button button--secondary alert__action" onClick={() => { const controller = new AbortController(); void load(controller.signal) }}>Retry</button></InlineAlert> : null}
      {result && !result.items.length ? <div className="customers-empty"><h2>{query ? 'No Customers match this search' : 'No Customers yet'}</h2><p>{query ? 'Check the name or last four digits and try again.' : 'Customers with an authoritative conversation will appear here.'}</p></div> : null}
      {result?.items.length ? <div className="customers-table-wrap"><table className="customers-table"><thead><tr><th scope="col">Customer</th><th scope="col">Channel</th><th scope="col">First recorded Lead source</th><th scope="col">Last interaction</th></tr></thead><tbody>{result.items.map((customer) => <tr key={customer.customer_id}><th scope="row"><Link to={detailLocation(customer.customer_reference, customer.conversation_id)} state={{ fromCustomers: `${location.pathname}${location.search}` }}>{customer.display_name?.trim() || 'Unnamed Customer'}</Link><span>Customer ID · {customer.customer_id}</span></th><td data-label="Channel">{customer.communication_channel === 'whatsapp' ? 'WhatsApp' : 'Not verified'}</td><td data-label="First recorded Lead source">{label(customer.acquisition_source)}</td><td data-label="Last interaction"><time dateTime={customer.last_interaction_at}>{timestamp(customer.last_interaction_at)}</time></td></tr>)}</tbody></table></div> : null}
      {result ? <nav className="customers-pagination" aria-label="Customer pages"><button type="button" className="button button--secondary" disabled={offset === 0} onClick={() => page(Math.max(0, offset - pageSize))}>Previous</button><button type="button" className="button button--secondary" disabled={!result.has_more} onClick={() => page(offset + pageSize)}>Next</button></nav> : null}
    </div>
  </section>
}
