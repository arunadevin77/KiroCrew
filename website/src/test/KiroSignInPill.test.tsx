import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { act, cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react'

import { renderWithProviders } from './helpers'
import KiroSignInPill, { KIRO_SIGN_IN_PILL_POLL_MS } from '../components/KiroSignInPill'
import { KIRO_SIGN_IN_PATH } from '../pages/developer/kiroSignInLink'
import { api, type KasLoginStatus } from '../api/client'

const navigate = vi.fn()
vi.mock('react-router-dom', async importOriginal => ({
  ...(await importOriginal<typeof import('react-router-dom')>()),
  useNavigate: () => navigate,
}))

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return {
    ...mod,
    api: {
      ...mod.api,
      kasLoginStatus: vi.fn(),
      kirocrewConfig: vi.fn(),
    },
  }
})

const kasLoginStatus = vi.mocked(api.kasLoginStatus)
const kirocrewConfig = vi.mocked(api.kirocrewConfig)

// Token-free status bodies, exactly the fields `/api/kas-login` returns.
const HEALTHY: KasLoginStatus = {
  authenticated: true,
  provider: 'Google',
  identity: 'social',
  transport: 'device',
  expires_at: '2099-01-01T00:00:00Z',
  expired: false,
  has_refresh_token: true,
  refresh_rejected: false,
  usable: true,
}
/** Access token past the margin and nothing to renew it with. */
const EXPIRED: KasLoginStatus = { ...HEALTHY, expires_at: '2000-01-01T00:00:00Z', expired: true, has_refresh_token: false, usable: false }
/** The issuer refused the last renewal; `usable` stays true (see auth/service.py status). */
const REFUSED: KasLoginStatus = { ...HEALTHY, refresh_rejected: true }
const SIGNED_OUT: KasLoginStatus = { ...HEALTHY, authenticated: false, provider: '', identity: '', expires_at: null, has_refresh_token: false, usable: false }

const pill = () => screen.queryByTestId('kiro-sign-in-pill')

function backend(id: string) {
  kirocrewConfig.mockResolvedValue({ agent: { acp_backend: id } } as Awaited<ReturnType<typeof api.kirocrewConfig>>)
}

beforeEach(() => {
  navigate.mockReset()
  kasLoginStatus.mockReset()
  kirocrewConfig.mockReset()
})

// Unmount first so no query timer fires on the fake clock, then restore real
// timers even when a fake-clock test fails part-way.
afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

describe('KiroSignInPill', () => {
  it('appears on KAS when the stored sign-in has expired with nothing to renew it', async () => {
    backend('kas')
    kasLoginStatus.mockResolvedValue(EXPIRED)
    renderWithProviders(<KiroSignInPill />)
    const el = await screen.findByTestId('kiro-sign-in-pill')
    expect(el).toHaveTextContent('Kiro sign-in expired')
    expect(el).toHaveAttribute('data-cause', 'expired')
    expect(el).toHaveAttribute('title', expect.stringMatching(/has expired and cannot be renewed/))
  })

  it('appears on KAS when the issuer refused the renewal, though the token is still usable', async () => {
    backend('kas')
    kasLoginStatus.mockResolvedValue(REFUSED)
    renderWithProviders(<KiroSignInPill />)
    const el = await screen.findByTestId('kiro-sign-in-pill')
    expect(el).toHaveAttribute('data-cause', 'refresh_rejected')
    expect(el).toHaveAttribute('title', expect.stringMatching(/refused to renew/))
  })

  it('keeps its visible label inside its accessible name, so the folded icon-only form is still named', async () => {
    backend('kas')
    kasLoginStatus.mockResolvedValue(EXPIRED)
    renderWithProviders(<KiroSignInPill />)
    const el = await screen.findByTestId('kiro-sign-in-pill')
    // The top bar's last collapse rung hides this class (index.css).
    expect(el.querySelector('.tb-drop-update-label')).toHaveTextContent('Kiro sign-in expired')
    expect(el).toHaveAccessibleName(/^Kiro sign-in expired\. Open the Kiro sign-in/)
  })

  it('carries a shape cue beside the key, outside the label, so the icon-only form does not rely on colour', async () => {
    backend('kas')
    kasLoginStatus.mockResolvedValue(REFUSED)
    renderWithProviders(<KiroSignInPill />)
    const el = await screen.findByTestId('kiro-sign-in-pill')
    const alert = within(el).getByTestId('kiro-sign-in-pill-alert')
    // Not inside the label the narrow bar hides, and not announced twice.
    expect(el.querySelector('.tb-drop-update-label')?.contains(alert)).toBe(false)
    expect(alert).toHaveAttribute('aria-hidden', 'true')
  })

  it('deep-links to the sign-in card instead of starting a sign-in', async () => {
    backend('kas')
    kasLoginStatus.mockResolvedValue(EXPIRED)
    renderWithProviders(<KiroSignInPill />)
    fireEvent.click(await screen.findByTestId('kiro-sign-in-pill'))
    expect(navigate).toHaveBeenCalledWith(KIRO_SIGN_IN_PATH)
  })

  it('is absent while the sign-in is healthy', async () => {
    backend('kas')
    kasLoginStatus.mockResolvedValue(HEALTHY)
    renderWithProviders(<KiroSignInPill />)
    await waitFor(() => expect(kasLoginStatus).toHaveBeenCalled())
    expect(pill()).not.toBeInTheDocument()
  })

  it('is absent with no stored sign-in: not signed in is not lapsed', async () => {
    backend('kas')
    kasLoginStatus.mockResolvedValue(SIGNED_OUT)
    renderWithProviders(<KiroSignInPill />)
    await waitFor(() => expect(kasLoginStatus).toHaveBeenCalled())
    expect(pill()).not.toBeInTheDocument()
  })

  it.each([
    ['kiro-cli', ''],
    ['Claude', 'claude'],
  ])('never reads the sign-in on the %s backend, where it concerns no agent', async (_name, id) => {
    backend(id)
    kasLoginStatus.mockResolvedValue(EXPIRED)
    renderWithProviders(<KiroSignInPill />)
    await waitFor(() => expect(kirocrewConfig).toHaveBeenCalled())
    // Let the config land and any status read that would follow it run.
    await act(async () => { await Promise.resolve() })
    expect(kasLoginStatus).not.toHaveBeenCalled()
    expect(pill()).not.toBeInTheDocument()
  })

  it('clears as soon as a new sign-in lands on the shared status entry', async () => {
    backend('kas')
    kasLoginStatus.mockResolvedValue(EXPIRED)
    const { queryClient } = renderWithProviders(<KiroSignInPill />)
    await screen.findByTestId('kiro-sign-in-pill')
    // What the sign-in flow does when its credential lands (KasLoginGate
    // invalidates `['kas-login']`), so the pill does not wait for its poll.
    kasLoginStatus.mockResolvedValue(HEALTHY)
    await act(async () => { await queryClient.invalidateQueries({ queryKey: ['kas-login'] }) })
    await waitFor(() => expect(pill()).not.toBeInTheDocument())
  })

  it('re-reads the status on its own interval, so a refusal or a renewal shows without a reload', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    backend('kas')
    kasLoginStatus.mockResolvedValue(HEALTHY)
    renderWithProviders(<KiroSignInPill />)
    await waitFor(() => expect(kasLoginStatus).toHaveBeenCalledTimes(1))
    expect(pill()).not.toBeInTheDocument()

    // The gateway's renewal tick records a refusal between two reads.
    kasLoginStatus.mockResolvedValue(REFUSED)
    await vi.advanceTimersByTimeAsync(KIRO_SIGN_IN_PILL_POLL_MS)
    await waitFor(() => expect(pill()).toBeInTheDocument())

    // A successful renewal (or a new sign-in) clears it on the next read.
    kasLoginStatus.mockResolvedValue(HEALTHY)
    await vi.advanceTimersByTimeAsync(KIRO_SIGN_IN_PILL_POLL_MS)
    await waitFor(() => expect(pill()).not.toBeInTheDocument())
  })
})
