import { X } from 'lucide-react'
import ErrorNotice from './ErrorNotice'
import { i18nT } from '../i18n/t'

/**
 * The agent-switch result toast.
 *
 * Why this surface changed at all: it rendered a rejected request through hand-rolled markup with
 * `role="status"` and a warn-tinted border, and `errors-use-error-notice` (blocking) names a
 * warn-toned or `role="status"` failure as the same violation as a hand-rolled red div. That rule
 * decides by where the VALUE came from, not by how it reads, so a withheld switch counts even
 * though a turn already in flight clears itself: it arrives as a rejected request carrying a
 * backend `{ error, code }` body, which puts it outside the rule's carve-out for status text about
 * something that has not failed.
 *
 * `kind` exists because the store's notice is SHARED. Nineteen of its twenty dispatchers resolve a
 * failure through `agentSwitchFailureMessage`, but `shell/shortcuts/shellKeyboard` also announces
 * that a new chat could not get its own session folder -- a create that SUCCEEDED, which the same
 * rule's carve-out covers and so "must not be dressed as" an error. That one marks itself
 * `status`, and only it keeps the pre-existing warn-bordered `role="status"` treatment.
 *
 * This wrapper owns only what a FLOATING notice needs and the shared component cannot know --
 * viewport anchoring, elevation, and an OPAQUE backdrop, since the notice's own tint is
 * translucent and would otherwise read against whatever it covers.
 */
export default function AgentSwitchNotice({
  message,
  kind,
  onDismiss,
}: {
  /** Resolved by `agentSwitchFailureMessage`; falsy renders nothing. */
  message?: string | null
  /** `status` marks a notice whose subject did NOT fail. Absent means a failure. */
  kind?: 'status'
  onDismiss: () => void
}) {
  if (!message) return null
  return (
    <div
      data-testid="agent-switch-notice"
      className="fixed z-[70] top-safe-offset-14 left-safe-offset-4 right-safe-offset-4 sm:left-auto sm:w-[440px] bg-bg-elevated rounded-lg shadow-xl animate-rise"
    >
      {kind === 'status' ? (
        <div
          role="status"
          data-testid="agent-switch-status"
          className="border rounded-lg p-3 flex items-center gap-3"
          style={{ borderColor: 'color-mix(in srgb, var(--warn) 45%, transparent)' }}
        >
          <span className="text-sm text-text flex-1">{message}</span>
          <button onClick={onDismiss} aria-label={i18nT('app.dismiss')} className="text-muted hover:text-text leading-none p-0.5">
            <X className="lucide-inline w-4 h-4" />
          </button>
        </div>
      ) : (
        /* No hand-off: this notice is `position: fixed` and floats over the whole app, and the
          * switch that raises it is reachable from a global Alt+Shift cycle that is not
          * input-gated -- so the chat composer draft, or any half-filled form, can be mounted
          * UNDERNEATH it. The hand-off navigates to chat and unmounts that subtree, discarding
          * the draft with no prompt, and this surface cannot see what it is covering. */
        <ErrorNotice
          message={message}
          onDismiss={onDismiss}
          testId="agent-switch-error"
        />
      )}
    </div>
  )
}
