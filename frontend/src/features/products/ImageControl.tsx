import { useCallback, useEffect, useId, useMemo, useRef, useState, type FormEvent } from 'react'
import { createBusinessProductsClient, type CatalogMedia } from '../../api/businessProducts'
import { asApiError, errorMessage } from '../../api/errors'
import { useAuth } from '../../auth/AuthProvider'
import { PasswordField } from '../../components/PasswordField'

type OwnerScope = 'product' | 'sellable_item'
type Preview = 'idle' | 'loading' | 'ready' | 'failed'

function validImageUrl(value: string): boolean {
  try {
    const url = new URL(value)
    return url.protocol === 'https:' && !!url.hostname && !url.username && !url.password
  } catch { return false }
}

export function ImageControl({ scope, ownerId, onChanged }: {
  scope: OwnerScope; ownerId: string; onChanged: () => Promise<void>
}) {
  const auth = useAuth()
  const client = useMemo(() => createBusinessProductsClient(auth.handleSessionExpired), [auth.handleSessionExpired])
  const [primary, setPrimary] = useState<CatalogMedia | null>(null)
  const [loading, setLoading] = useState(true)
  const [editing, setEditing] = useState(false)
  const [url, setUrl] = useState('')
  const [alt, setAlt] = useState('')
  const [preview, setPreview] = useState<Preview>('idle')
  const [busy, setBusy] = useState(false)
  const [reauth, setReauth] = useState(false)
  const [password, setPassword] = useState('')
  const [needsRefresh, setNeedsRefresh] = useState(false)
  const [denied, setDenied] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const readController = useRef<AbortController | null>(null)
  const operation = useRef<AbortController | null>(null)
  const fieldId = useId()
  const label = scope === 'product' ? 'Product image' : 'Variant image override'

  const load = useCallback(async () => {
    readController.current?.abort()
    const current = new AbortController(); readController.current = current
    setLoading(true); setError(null)
    try {
      const result = await client.listMedia(scope, ownerId, current.signal)
      if (current.signal.aborted) return false
      setPrimary(result.items.find((item) => item.active && item.is_primary) ?? null)
      setNeedsRefresh(false)
      return true
    } catch (failure) {
      if (!current.signal.aborted) { setError(errorMessage(asApiError(failure))); setNeedsRefresh(true) }
      return false
    } finally { if (!current.signal.aborted) setLoading(false) }
  }, [client, scope, ownerId])

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load()
    return () => { readController.current?.abort(); operation.current?.abort() }
  }, [load])

  const begin = () => {
    setUrl(primary?.asset_url ?? '')
    setAlt(primary?.alt_text ?? '')
    setPreview('idle'); setError(null); setNotice(null); setEditing(true)
  }
  const updateUrl = (value: string) => {
    setUrl(value)
    setPreview(value.trim() && validImageUrl(value.trim()) ? 'loading' : 'idle')
  }
  const recent = () => {
    const expiry = auth.session?.recent_reauthentication_expires_at_epoch
    if (expiry == null || expiry < Math.floor(Date.now() / 1000)) { setReauth(true); return false }
    return true
  }
  const mutate = async (kind: 'save' | 'remove') => {
    if (busy || loading || needsRefresh || denied || !recent()) return
    const assetUrl = url.trim()
    if (kind === 'save' && (!validImageUrl(assetUrl) || preview !== 'ready')) {
      setError('Enter an HTTPS image URL and wait for a successful preview.'); return
    }
    const current = new AbortController(); operation.current = current
    setBusy(true); setError(null); setNotice(null)
    let saved = false
    try {
      const csrf = await auth.getCsrfForMutation()
      if (current.signal.aborted) return
      if (kind === 'remove') {
        if (!primary) return
        await client.removeMedia(primary.media_id, csrf, current.signal)
      } else if (primary) {
        await client.replaceMedia(primary.media_id, assetUrl, alt.trim() || null, csrf, current.signal)
      } else {
        await client.addMedia(scope, ownerId, assetUrl, alt.trim() || null, csrf, current.signal)
      }
      saved = true
      if (current.signal.aborted) return
      const refreshed = await load()
      if (!refreshed) throw new Error('Current image could not be reread')
      setEditing(false); setReauth(false); setPreview('idle')
      await onChanged()
      setNotice(kind === 'remove' ? 'Image removed. Showing current image.' : 'Image saved. Showing current image.')
    } catch (failure) {
      if (current.signal.aborted) return
      const apiError = asApiError(failure)
      if (apiError.code === 'recent_reauthentication_required') setReauth(true)
      else if (apiError.category === 'forbidden') { setDenied(true); setEditing(false); setError('You do not have permission to manage images.') }
      else if (saved || apiError.status === 0 || apiError.status >= 500) {
        setNeedsRefresh(true)
        setError('The image change could not be confirmed. Refresh current image before trying again; it may already be saved.')
      } else setError(errorMessage(apiError))
    } finally { if (!current.signal.aborted) setBusy(false) }
  }
  const confirmPassword = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true); setError(null)
    try {
      await auth.reauthenticate(password)
      setReauth(false); setNotice('Password confirmed. Review and save the image change.')
    } catch (failure) { setError(errorMessage(asApiError(failure))) }
    finally { setPassword(''); setBusy(false) }
  }

  return <section className="products-secondary products-image-control" aria-label={label}>
    <h2>{label}</h2>
    {loading && <p role="status">Loading current image…</p>}
    {error && <p role="alert">{error}</p>}
    {notice && <p role="status">{notice}</p>}
    {needsRefresh && <button className="button button--secondary" disabled={loading || busy} onClick={() => void load()}>Refresh current image</button>}
    {!loading && !needsRefresh && !denied && !editing && <div className="products-setup-actions">
      <button className="button button--secondary" onClick={begin}>{primary ? 'Replace image' : 'Add image'}</button>
      {primary && <button className="button button--secondary" disabled={busy} onClick={() => void mutate('remove')}>Remove image</button>}
    </div>}
    {editing && !needsRefresh && !denied && <form className="products-image-form" onSubmit={(event) => { event.preventDefault(); void mutate('save') }}>
      <label htmlFor={`${fieldId}-url`}>HTTPS image URL</label>
      <input id={`${fieldId}-url`} type="url" required maxLength={2048} value={url} disabled={busy}
        onChange={(event) => updateUrl(event.target.value)} />
      <label htmlFor={`${fieldId}-alt`}>Alt text (optional)</label>
      <input id={`${fieldId}-alt`} maxLength={500} value={alt} disabled={busy} onChange={(event) => setAlt(event.target.value)} />
      {preview === 'loading' && <p role="status">Checking image preview…</p>}
      {preview === 'failed' && <p role="alert">Image preview failed. Check the URL before saving.</p>}
      {url.trim() && validImageUrl(url.trim()) && <img key={url.trim()} className="products-image products-image--large"
        src={url.trim()} alt={alt.trim() || 'Image preview'} referrerPolicy="no-referrer"
        onLoad={() => setPreview('ready')} onError={() => setPreview('failed')} />}
      {preview === 'ready' && <p role="status">Image preview ready.</p>}
      <div className="products-setup-actions">
        <button className="button button--primary" disabled={busy || reauth || preview !== 'ready'}>Save image</button>
        <button className="button button--secondary" type="button" disabled={busy} onClick={() => { setEditing(false); setReauth(false); setError(null) }}>Cancel</button>
      </div>
    </form>}
    {reauth && !denied && <form className="products-image-form" onSubmit={(event) => void confirmPassword(event)}>
      <PasswordField id={`${fieldId}-password`} label="Confirm Administrator password" autoComplete="current-password"
        value={password} disabled={busy} required onChange={(event) => setPassword(event.target.value)} />
      <button className="button button--primary" disabled={busy}>Confirm password</button>
    </form>}
  </section>
}
