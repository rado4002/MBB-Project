import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  createBusinessCustomersClient,
  type BusinessCustomerProfile,
  type CustomerLanguage,
  type CustomerProfileSnapshot,
} from '../../api/businessCustomers'
import { asApiError, errorMessage } from '../../api/errors'
import { useAuth } from '../../auth/AuthProvider'
import { InlineAlert } from '../../components/InlineAlert'
import { PasswordField } from '../../components/PasswordField'

function label(value: string) {
  return value.split('_').map((part) => part.charAt(0).toUpperCase() + part.slice(1)).join(' ')
}

function timestamp(value: string | null) {
  if (!value) return 'Not recorded'
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? 'Unavailable' : new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium', timeStyle: 'short',
  }).format(parsed)
}

function snapshot(profile: BusinessCustomerProfile): CustomerProfileSnapshot {
  return {
    display_name: profile.display_name,
    city: profile.city,
    preferred_language: profile.preferred_language,
  }
}

export function CustomerProfilePage() {
  const { conversationId = '' } = useParams()
  const auth = useAuth()
  const client = useMemo(
    () => createBusinessCustomersClient(auth.handleSessionExpired),
    [auth.handleSessionExpired],
  )
  const controller = useRef<AbortController | null>(null)
  const [profile, setProfile] = useState<BusinessCustomerProfile | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [name, setName] = useState('')
  const [city, setCity] = useState('')
  const [language, setLanguage] = useState<CustomerLanguage>('french')
  const [reason, setReason] = useState<'customer_provided_update' | 'operator_correction'>('customer_provided_update')
  const [evidence, setEvidence] = useState('')
  const [attested, setAttested] = useState(false)
  const [reauth, setReauth] = useState(false)
  const [password, setPassword] = useState('')
  const canManage = auth.session?.capabilities.includes('customer.manage') ?? false

  const applyProfile = useCallback((next: BusinessCustomerProfile) => {
    setProfile(next)
    setName(next.display_name ?? '')
    setCity(next.city)
    setLanguage(next.preferred_language)
  }, [])

  const load = useCallback(async () => {
    controller.current?.abort()
    const active = new AbortController()
    controller.current = active
    setLoading(true); setError(null)
    try {
      applyProfile(await client.detail(conversationId, active.signal))
    } catch (failure) {
      if (!active.signal.aborted) setError(errorMessage(asApiError(failure)))
    } finally {
      if (!active.signal.aborted) setLoading(false)
    }
  }, [applyProfile, client, conversationId])

  useEffect(() => {
    // The route-scoped profile must discard any previous Customer before loading.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load()
    return () => controller.current?.abort()
  }, [load])

  const recentlyAuthenticated = () => {
    const expiry = auth.session?.recent_reauthentication_expires_at_epoch
    if (expiry == null || expiry < Math.floor(Date.now() / 1000)) {
      setReauth(true)
      return false
    }
    return true
  }

  const save = async (event: FormEvent) => {
    event.preventDefault()
    if (!profile || !canManage || busy || !city.trim() || !recentlyAuthenticated()) return
    const active = new AbortController()
    controller.current?.abort(); controller.current = active
    setBusy(true); setError(null); setNotice(null)
    try {
      const csrf = await auth.getCsrfForMutation()
      const next = await client.update(
        conversationId,
        snapshot(profile),
        { display_name: name.trim() || null, city: city.trim(), preferred_language: language },
        reason,
        csrf,
        active.signal,
      )
      applyProfile(next)
      setNotice('Customer profile updated and audited.')
    } catch (failure) {
      if (active.signal.aborted) return
      const apiError = asApiError(failure)
      if (apiError.code === 'recent_reauthentication_required') setReauth(true)
      else if (apiError.code === 'CUSTOMER_PROFILE_STALE') {
        setError('The profile changed since review. Current values have been reloaded.')
        void load()
      } else setError(errorMessage(apiError))
    } finally {
      if (!active.signal.aborted) setBusy(false)
    }
  }

  const optOut = async (event: FormEvent) => {
    event.preventDefault()
    if (!profile || !canManage || busy || !evidence || !attested || !recentlyAuthenticated()) return
    const active = new AbortController()
    controller.current?.abort(); controller.current = active
    setBusy(true); setError(null); setNotice(null)
    try {
      const csrf = await auth.getCsrfForMutation()
      const next = await client.optOut(conversationId, evidence, csrf, active.signal)
      applyProfile(next)
      setEvidence(''); setAttested(false)
      setNotice('Customer opt-out recorded with message evidence. Pending follow-ups were stopped.')
    } catch (failure) {
      if (active.signal.aborted) return
      const apiError = asApiError(failure)
      if (apiError.code === 'recent_reauthentication_required') setReauth(true)
      else setError(errorMessage(apiError))
    } finally {
      if (!active.signal.aborted) setBusy(false)
    }
  }

  const confirmPassword = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true); setError(null)
    try {
      await auth.reauthenticate(password)
      setReauth(false)
      setNotice('Password confirmed. Review and submit the Customer change.')
    } catch (failure) {
      setError(errorMessage(asApiError(failure)))
    } finally {
      setPassword(''); setBusy(false)
    }
  }

  return <section className="customer-profile-page" aria-labelledby="customer-profile-title">
    <header className="business-page-header">
      <div>
        <p className="eyebrow">Customer</p>
        <h1 id="customer-profile-title">Profile &amp; contact preferences</h1>
      </div>
      <Link className="button button--secondary" to={`/inbox/${encodeURIComponent(conversationId)}`}>Back to conversation</Link>
    </header>
    {loading ? <p role="status">Loading Customer profile…</p> : null}
    {error ? <InlineAlert>{error}<button type="button" className="button button--secondary alert__action" onClick={() => void load()}>Reload</button></InlineAlert> : null}
    {notice ? <p className="form-notice" role="status">{notice}</p> : null}
    {profile ? <>
      <section className="business-card" aria-labelledby="customer-identity-heading">
        <h2 id="customer-identity-heading">Identity</h2>
        <dl className="context-details">
          <div><dt>Phone</dt><dd>{profile.phone_masked}</dd></div>
          <div><dt>Communication channel</dt><dd>{profile.communication_channels.length ? 'WhatsApp · verified provider message' : 'Not verified'}</dd></div>
          <div><dt>Acquisition source</dt><dd>{profile.acquisition_sources.length ? profile.acquisition_sources.map(label).join(', ') : 'Not recorded'}</dd></div>
        </dl>
        <p className="supporting-copy">Communication channel evidence and acquisition source are separate facts. This profile does not merge identities across channels.</p>
      </section>
      <section className="business-card" aria-labelledby="customer-details-heading">
        <h2 id="customer-details-heading">Customer details</h2>
        <form className="customer-profile-form" onSubmit={(event) => void save(event)}>
          <label htmlFor="customer-name">Name</label>
          <input id="customer-name" value={name} maxLength={100} disabled={!canManage || busy} onChange={(event) => setName(event.target.value)} />
          <label htmlFor="customer-city">City</label>
          <input id="customer-city" value={city} maxLength={50} required disabled={!canManage || busy} onChange={(event) => setCity(event.target.value)} />
          <label htmlFor="customer-language">Preferred language</label>
          <select id="customer-language" value={language} disabled={!canManage || busy} onChange={(event) => setLanguage(event.target.value as CustomerLanguage)}>
            <option value="french">French</option><option value="lingala">Lingala</option><option value="swahili">Swahili</option>
          </select>
          {canManage ? <><label htmlFor="customer-change-reason">Reason</label>
            <select id="customer-change-reason" value={reason} disabled={busy} onChange={(event) => setReason(event.target.value as typeof reason)}>
              <option value="customer_provided_update">Customer provided update</option>
              <option value="operator_correction">Operator correction</option>
            </select>
            <button className="button button--primary" disabled={busy || !city.trim()}>Save profile</button></> : <p>Administrator permission is required to edit Customer details.</p>}
        </form>
      </section>
      <section className="business-card" aria-labelledby="customer-contact-heading">
        <h2 id="customer-contact-heading">Contact preferences</h2>
        <dl className="context-details">
          <div><dt>Opt-out</dt><dd>{profile.opted_out ? `Recorded · ${timestamp(profile.opted_out_at)}` : 'Not recorded'}</dd></div>
          <div><dt>Consent record</dt><dd>{profile.consent_given ? `Present · ${timestamp(profile.consent_timestamp)}` : 'No consent recorded'}</dd></div>
        </dl>
        <p className="supporting-copy">Consent cannot be granted or an opt-out reversed in this workflow. Those actions require an approved evidence and revocation policy.</p>
        {canManage && !profile.opted_out ? <form className="customer-profile-form" onSubmit={(event) => void optOut(event)}>
          <label htmlFor="customer-opt-out-evidence">Customer opt-out message</label>
          <select id="customer-opt-out-evidence" required value={evidence} disabled={busy} onChange={(event) => setEvidence(event.target.value)}>
            <option value="">Select verified message evidence</option>
            {profile.opt_out_evidence.map((item) => <option key={item.message_id} value={item.message_id}>{timestamp(item.received_at)} · {item.content_excerpt}</option>)}
          </select>
          {!profile.opt_out_evidence.length ? <p>No recognized inbound WhatsApp opt-out message is available.</p> : null}
          <label className="checkbox-row"><input type="checkbox" checked={attested} disabled={busy} onChange={(event) => setAttested(event.target.checked)} />I confirm this message is the Customer’s opt-out request.</label>
          <button className="button button--secondary" disabled={busy || !evidence || !attested}>Record opt-out</button>
        </form> : null}
      </section>
      {reauth ? <section className="business-card" aria-labelledby="customer-reauth-heading">
        <h2 id="customer-reauth-heading">Confirm Administrator password</h2>
        <form className="customer-profile-form" onSubmit={(event) => void confirmPassword(event)}>
          <PasswordField id="customer-reauth-password" label="Password" autoComplete="current-password" value={password} required disabled={busy} onChange={(event) => setPassword(event.target.value)} />
          <button className="button button--primary" disabled={busy}>Confirm password</button>
        </form>
      </section> : null}
    </> : null}
  </section>
}
