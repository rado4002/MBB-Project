import { useCallback, useEffect, useId, useMemo, useState, type FormEvent } from 'react'
import { createBusinessFxClient, type FxAuthority, type FxRate } from '../../api/businessFx'
import { asApiError, errorMessage } from '../../api/errors'
import { useAuth } from '../../auth/AuthProvider'
import { PasswordField } from '../../components/PasswordField'

const ratePattern = /^(?:0|[1-9]\d{0,11})(?:\.\d{1,6})?$/

export function FxPage() {
  const auth = useAuth()
  const client = useMemo(() => createBusinessFxClient(auth.handleSessionExpired), [auth.handleSessionExpired])
  const [authority, setAuthority] = useState<FxAuthority | null>(null)
  const [rates, setRates] = useState<FxRate[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [rate, setRate] = useState('')
  const [review, setReview] = useState(false)
  const [password, setPassword] = useState('')
  const [reauth, setReauth] = useState(false)
  const [busy, setBusy] = useState(false)
  const [uncertain, setUncertain] = useState(false)
  const [modeReview, setModeReview] = useState(false)
  const rateId = useId()
  const passwordId = useId()
  const active = rates.find((item) => item.authority_mode === authority?.mode && item.ended_at === null)

  const load = useCallback(async (signal: AbortSignal): Promise<boolean> => {
    setLoading(true); setError(null)
    try {
      const [nextAuthority, history] = await Promise.all([client.authority(signal), client.history(signal)])
      if (!signal.aborted) { setAuthority(nextAuthority); setRates(history.items); setUncertain(false); return true }
    } catch (failure) {
      if (!signal.aborted) { setAuthority(null); setRates([]); setError(errorMessage(asApiError(failure))) }
    } finally { if (!signal.aborted) setLoading(false) }
    return false
  }, [client])
  useEffect(() => {
    const controller = new AbortController()
    // Initial authoritative read is cancelled on navigation.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load(controller.signal)
    return () => controller.abort()
  }, [load])

  const requireReauth = () => {
    const expiry = auth.session?.recent_reauthentication_expires_at_epoch
    if (expiry == null || expiry < Math.floor(Date.now() / 1000)) { setReauth(true); return true }
    return false
  }
  const save = async (kind: 'rate' | 'mode') => {
    if (busy || uncertain || requireReauth()) return
    setBusy(true); setError(null); setNotice(null)
    let sent = false
    try {
      const csrf = await auth.getCsrfForMutation()
      sent = true
      if (kind === 'rate') await client.setManualRate(rate, csrf)
      else await client.setManualMode(csrf)
      setReview(false); setModeReview(false); setRate('')
      const controller = new AbortController()
      if (await load(controller.signal)) {
        setNotice(kind === 'rate' ? 'Manual rate saved. Review the current authority before using it.' : 'Manual FX authority is active.')
      } else {
        setUncertain(true)
        setError('The update was sent, but current FX records could not be read. Refresh before another update.')
      }
    } catch (failure) {
      const apiError = asApiError(failure)
      if (apiError.code === 'recent_reauthentication_required') setReauth(true)
      else if (sent && (apiError.status === 0 || apiError.status >= 500)) {
        setUncertain(true)
        setError('The result could not be confirmed. Refresh the FX records before trying another update.')
      } else setError(errorMessage(apiError))
    } finally { setBusy(false) }
  }
  const submitRate = (event: FormEvent) => {
    event.preventDefault()
    if (!ratePattern.test(rate) || Number(rate) <= 0) {
      setError('Enter a positive USD/CDF rate with at most six decimal places.')
      return
    }
    setError(null); setReview(true)
  }
  const confirmPassword = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true); setError(null)
    try { await auth.reauthenticate(password); setReauth(false); setNotice('Password confirmed. Review and confirm the proposed change.') }
    catch (failure) { setError(errorMessage(asApiError(failure))) }
    finally { setPassword(''); setBusy(false) }
  }

  return <section className="products-page">
    <header className="business-page-heading"><div><p className="eyebrow">Business · FX</p><h1>USD/CDF exchange rate</h1></div></header>
    {loading && <p role="status">Loading FX authority…</p>}
    {error && <p role="alert">{error}</p>}
    {notice && <p role="status">{notice}</p>}
    {uncertain && <button className="button button--secondary" type="button" onClick={() => void load(new AbortController().signal)}>Refresh FX records</button>}
    {authority && <>
      <section className="products-secondary" aria-label="Current FX authority">
        <h2>Current authority</h2>
        <p>Mode: {authority.mode === 'MANUAL' ? 'Manual' : 'Automatic'} · Revision {authority.revision}</p>
        {active ? <p>Current rate record for this mode: 1 USD = {active.rate} CDF · Effective {new Date(active.effective_at).toLocaleString()}{authority.mode === 'AUTOMATIC' ? ' · May be stale; unavailable for new quotes after 36 hours' : ''}</p>
          : <p role="status">No usable current rate. New CDF quotes are unavailable.</p>}
        <p>Changes affect new quotes only. Accepted Order amounts and rates remain unchanged.</p>
      </section>
      <section className="products-secondary" aria-label="Manual FX management">
        <h2>Manual rate</h2>
        <p>Saving a manual rate does not switch authority automatically.</p>
        <form className="products-setup" onSubmit={submitRate}>
          <label htmlFor={rateId}>CDF per 1 USD</label>
          <input id={rateId} inputMode="decimal" value={rate} disabled={busy || uncertain}
            onChange={(event) => { setRate(event.target.value); setReview(false) }} />
          <button className="button button--secondary" disabled={busy || uncertain}>Review manual rate</button>
        </form>
        {review && <div role="group" aria-label="Confirm manual rate">
          <p>Replace the current manual rate with 1 USD = {rate} CDF?</p>
          <button className="button button--primary" disabled={busy || uncertain || reauth} onClick={() => void save('rate')}>Confirm rate update</button>
          <button className="button button--secondary" disabled={busy} onClick={() => setReview(false)}>Cancel</button>
        </div>}
      </section>
      <section className="products-secondary" aria-label="FX mode selection">
        <h2>FX mode</h2>
        <p>Automatic retrieval is pending provider approval and credential validation. No automatic request is available here.</p>
        {authority.mode !== 'MANUAL' && rates.some((item) => item.authority_mode === 'MANUAL' && item.ended_at === null) &&
          (modeReview ? <div role="group" aria-label="Confirm manual mode">
            <p>Switch new quotes to the current manual rate?</p>
            <button className="button button--primary" disabled={busy || uncertain || reauth} onClick={() => void save('mode')}>Confirm manual mode</button>
            <button className="button button--secondary" disabled={busy} onClick={() => setModeReview(false)}>Cancel</button>
          </div> : <button className="button button--secondary" disabled={busy || uncertain} onClick={() => setModeReview(true)}>Select manual mode</button>)}
        {authority.mode === 'MANUAL' && <p>Manual mode selected.</p>}
      </section>
      <section className="products-secondary" aria-label="FX history">
        <h2>Recent rates</h2>
        <ul>{rates.map((item) => <li key={item.exchange_rate_id}>{item.authority_mode} · {item.rate} CDF · {item.source} · {new Date(item.effective_at).toLocaleString()}{item.ended_at === null ? ' · Current in mode' : ''}</li>)}</ul>
      </section>
    </>}
    {reauth && <form className="products-setup" onSubmit={(event) => void confirmPassword(event)}>
      <PasswordField id={passwordId} label="Confirm Administrator password" autoComplete="current-password"
        value={password} onChange={(event) => setPassword(event.target.value)} required disabled={busy} />
      <button className="button button--primary" disabled={busy}>Confirm password</button>
      <button className="button button--secondary" type="button" disabled={busy} onClick={() => setReauth(false)}>Back to review</button>
    </form>}
  </section>
}
