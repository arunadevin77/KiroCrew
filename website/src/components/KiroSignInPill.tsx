import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { KeyRound } from 'lucide-react'

import { api } from '../api/client'
import type { AcpBackendConfig } from '../api/acpBackend'
import { i18nT } from '../i18n/t'
import { KIRO_SIGN_IN_BACKEND, KIRO_SIGN_IN_PATH } from '../pages/developer/kiroSignInLink'
import { signInLapsed } from '../pages/developer/kiroSignInState'

// Same cache key the sign-in flow and card read status under
// (KasLoginGate.QUERY_KEY): a sign-in, sign-out or switch landing there
// re-reads this one entry, so the pill clears the moment a new sign-in lands
// instead of waiting for its own next poll.
const STATUS_QUERY_KEY = ['kas-login'] as const

/**
 * How often the pill re-reads the stored sign-in while KAS is the backend. The
 * gateway's renewal tick records a refused renewal in the store, and the status
 * read is a plain store read (no refresh, no network), so a minute bounds how
 * late the pill reports one at the cost of one small read per minute.
 */
export const KIRO_SIGN_IN_PILL_POLL_MS = 60_000

/**
 * Top-bar sign-in pill: the persistent, passive home for a stored Kiro sign-in
 * that no longer works. The sign-in card on Settings > Agent Harness says the
 * same thing, but on a page nobody keeps open; without a top-bar signal a lapsed
 * or issuer-refused sign-in stays invisible until a chat turn fails.
 *
 * Absent while there is nothing to say, the way the update pill is: it renders
 * only while KAS is the selected agent backend (the one backend that runs agents
 * as this identity; for kiro-cli or Claude a lapsed Kiro sign-in concerns
 * nothing) AND a stored sign-in exists AND the card's own verdict
 * (`signInLapsed`) calls it lapsed. A gateway on another backend never even
 * reads the status. Signing in again, or the renewal succeeding, clears it on
 * the next read.
 *
 * Clicking deep-links to the sign-in card (`KIRO_SIGN_IN_PATH`, which rings the
 * card) rather than starting a sign-in itself: the card is where the provider
 * is chosen, and a top-bar control that opens a browser sign-in on one click is
 * a misclick hazard.
 */
export default function KiroSignInPill() {
  const navigate = useNavigate()
  const { data: cfg } = useQuery<AcpBackendConfig>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
  })
  const kas = cfg?.agent?.acp_backend === KIRO_SIGN_IN_BACKEND
  const { data: status } = useQuery({
    queryKey: STATUS_QUERY_KEY,
    queryFn: api.kasLoginStatus,
    enabled: kas,
    refetchInterval: KIRO_SIGN_IN_PILL_POLL_MS,
  })

  if (!kas || !status?.authenticated || !signInLapsed(status)) return null

  // A status label, like the update pill's: the click only navigates. The card
  // heads both lapsed states "Sign-in expired", so the pill does too; the cause
  // (no way to renew vs. the issuer refused) rides the tooltip.
  const label = i18nT('components.kiroSignInPill.sign_in_expired')
  const detail = status.refresh_rejected
    ? i18nT('components.kiroSignInPill.refused_detail')
    : i18nT('components.kiroSignInPill.expired_detail')
  return (
    <button
      type="button"
      data-testid="kiro-sign-in-pill"
      data-cause={status.refresh_rejected ? 'refresh_rejected' : 'expired'}
      className="flex items-center gap-1.5 h-7 px-2.5 rounded-xl shrink-0 cursor-pointer text-[12px] whitespace-nowrap border border-warn/30 bg-warn/15 text-warn hover:opacity-90 transition-opacity"
      onClick={() => navigate(KIRO_SIGN_IN_PATH)}
      title={detail}
      // Starts with the visible label, so the accessible name still contains
      // it while the label is hidden on a narrow bar.
      aria-label={i18nT('components.kiroSignInPill.open_sign_in', { status: label })}
    >
      <span className="relative inline-flex shrink-0">
        <KeyRound size={13} className="lucide-inline" aria-hidden="true" />
        {/* A shape cue, not only the warn colour: on a narrow bar the label is
            gone and a bare key does not read as "something is wrong". */}
        <span
          data-testid="kiro-sign-in-pill-alert"
          aria-hidden="true"
          className="absolute -top-1 -right-1.5 size-1.5 rounded-full bg-warn ring-1 ring-bg"
        />
      </span>
      {/* The label yields exactly as the update pill's does: below 640px, and
          on the actions group's last collapse rung, which both passive pills
          share (`tb-drop-update-label`, index.css), so neither can push the
          notification control out of the bar. */}
      <span className="tb-drop-update-label hidden sm:inline">{label}</span>
    </button>
  )
}
