import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { sessionFixture } from '../../test/fixtures'
import { renderApp } from '../../test/renderApp'
import { server } from '../../test/server'

const base = '/api/v1/operator/commerce/exchange-rates/usd-cdf'
const effective = '2026-10-20T12:00:00Z'
const manual = { exchange_rate_id: '11111111-1111-4111-8111-111111111111', rate: '2800.000000',
  authority_mode: 'MANUAL', source: 'MBB_ADMIN', published_at: null, effective_at: effective, ended_at: null }
const automatic = { ...manual, exchange_rate_id: '22222222-2222-4222-8222-222222222222',
  rate: '2900.000000', authority_mode: 'AUTOMATIC', source: 'EXCHANGE_RATE_API', published_at: effective }

function setup(mode: 'MANUAL' | 'AUTOMATIC' = 'MANUAL', role: 'administrator' | 'operator' = 'administrator') {
  const writes: string[] = []
  let currentMode = mode
  let manualRate = manual.rate
  server.use(
    http.get('/api/v1/auth/session', () => HttpResponse.json({ ...sessionFixture(role), recent_reauthentication_expires_at_epoch: 2_000_000_000 })),
    http.get('/api/v1/auth/csrf', () => HttpResponse.json({ csrf_token: 'fx-csrf', expires_at_epoch: 2_000_000_000 })),
    http.get(`${base}/authority`, () => HttpResponse.json({ mode: currentMode, revision: 1, updated_at: effective })),
    http.get(base, () => HttpResponse.json({ items: [{ ...manual, rate: manualRate }, automatic] })),
    http.put(base, async ({ request }) => {
      writes.push('rate')
      expect(request.headers.get('X-CSRF-Token')).toBe('fx-csrf')
      const body = await request.json() as { rate: string; base_currency: string; quote_currency: string }
      expect(body).toEqual({ base_currency: 'USD', quote_currency: 'CDF', rate: '3000.123456' })
      manualRate = body.rate
      return HttpResponse.json({ ...manual, rate: manualRate })
    }),
    http.put(`${base}/authority`, async ({ request }) => {
      writes.push('mode')
      expect(await request.json()).toEqual({ mode: 'MANUAL' })
      currentMode = 'MANUAL'
      return HttpResponse.json({ mode: currentMode, revision: 2, updated_at: effective })
    }),
  )
  return writes
}

describe('Administrator FX management', () => {
  it('reviews a precise manual rate, saves it without switching authority, then explicitly selects manual', async () => {
    const writes = setup('AUTOMATIC')
    const user = userEvent.setup()
    renderApp('/business/fx')
    expect(await screen.findByText(/Mode: Automatic/)).toBeInTheDocument()
    await user.type(screen.getByLabelText('CDF per 1 USD'), '3000.123456')
    await user.click(screen.getByRole('button', { name: 'Review manual rate' }))
    expect(writes).toEqual([])
    await user.click(screen.getByRole('button', { name: 'Confirm rate update' }))
    await waitFor(() => expect(writes).toEqual(['rate']))
    expect(await screen.findByText(/Manual rate saved/)).toBeInTheDocument()
    expect(screen.getByText(/Mode: Automatic/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Select manual mode' }))
    expect(writes).toEqual(['rate'])
    await user.click(screen.getByRole('button', { name: 'Confirm manual mode' }))
    await waitFor(() => expect(writes).toEqual(['rate', 'mode']))
    expect(await screen.findByText(/Mode: Manual/)).toBeInTheDocument()
  })

  it('rejects excessive precision before writing and hides FX from non-administrators', async () => {
    const writes = setup()
    const user = userEvent.setup()
    renderApp('/business/fx')
    await screen.findByText(/Mode: Manual/)
    await user.type(screen.getByLabelText('CDF per 1 USD'), '3000.1234567')
    await user.click(screen.getByRole('button', { name: 'Review manual rate' }))
    expect(screen.getByRole('alert')).toHaveTextContent('at most six decimal places')
    expect(writes).toEqual([])
  })

  it('does not expose FX management to an operator', async () => {
    const writes = setup('MANUAL', 'operator')
    renderApp('/business/fx')
    await waitFor(() => expect(window.location.pathname).toBe('/inbox'))
    expect(screen.queryByRole('link', { name: 'FX' })).not.toBeInTheDocument()
    expect(writes).toEqual([])
  })

  it('holds a rate update after an uncertain response until FX records are reread', async () => {
    setup()
    let attempts = 0
    server.use(http.put(base, () => { attempts += 1; return HttpResponse.json({}, { status: 503 }) }))
    const user = userEvent.setup()
    renderApp('/business/fx')
    await screen.findByText(/Mode: Manual/)
    await user.type(screen.getByLabelText('CDF per 1 USD'), '3000.123456')
    await user.click(screen.getByRole('button', { name: 'Review manual rate' }))
    await user.click(screen.getByRole('button', { name: 'Confirm rate update' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Refresh the FX records')
    expect(screen.getByRole('button', { name: 'Confirm rate update' })).toBeDisabled()
    expect(attempts).toBe(1)
    await user.click(screen.getByRole('button', { name: 'Refresh FX records' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Confirm rate update' })).toBeEnabled())
    expect(attempts).toBe(1)
  })
})
