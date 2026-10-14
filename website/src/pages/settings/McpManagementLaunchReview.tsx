/**
 * How the MCP page shows a launch before anyone vouches for it: the command and
 * environment lines, the review a STUB switch opens, and the panel of a stubbed server
 * the gateway refused to launch. `McpManagement` mounts them per row; its bulk review
 * dialog reuses the launch lines.
 */
import { useEffect, useRef } from 'react'
import type { McpManagedServer } from '../../api/client'
import { Btn } from '../../components/ui'
import { i18nT } from '../../i18n/t'
import {
  type LaunchRefusal,
  type LaunchPreview,
  pairLaunches,
  refusalLaunches,
  launchIsApprovable,
  REFUSAL_REASON_KEY,
} from './mcpManagementModel'

/** Every character the C category covers -- what a launch display must escape
 *  rather than render, because none of them is readable where it sits. */
const HIDDEN_LAUNCH_CHAR = /\p{C}/u

/** The command and declared environment of one launch, as the operator reads it.
 *
 *  Shared by the three surfaces that show a launch -- the review a stub switch
 *  opens, a refused row, and the bulk review list -- because three copies of this
 *  markup would be free to disagree about what an approval covers.
 */
function visibleLaunchText(value: string): string {
  return Array.from(value, character => {
    // The whole Unicode C category, not a list of the ranges that have been
    // reported: control, format, surrogate, private-use and unassigned code
    // points are all characters a command can carry and an operator cannot see
    // in it, and a list of ranges is a list someone has to keep adding to --
    // U+00AD soft hyphen and U+2060 word joiner are outside the bidi and
    // zero-width blocks and hide just as much.
    if (!HIDDEN_LAUNCH_CHAR.test(character)) return character
    // Iterating with `Array.from` yields whole code points, so an astral
    // character is escaped once from its own value rather than twice from its
    // surrogate halves.
    const code = character.codePointAt(0) ?? 0
    const zero = String.fromCharCode(48)
    if (code < 0x20) return JSON.stringify(character).slice(1, -1)
    if (code <= 0xff) {
      const hexPrefix = String.fromCharCode(92, 120)
      return `${hexPrefix}${code.toString(16).padStart(2, zero)}`
    }
    const unicodePrefix = String.fromCharCode(92, 117)
    if (code <= 0xffff) return `${unicodePrefix}${code.toString(16).padStart(4, zero)}`
    return `${unicodePrefix}{${code.toString(16)}}`
  }).join('')
}

export function LaunchLines({ launches }: { launches: { argv: string[]; env: string[] }[] }) {
  return (
    <>
      {launches.map((launch, i) => (
        <div key={i} className="mt-1.5">
          <span className="block text-[11px] font-medium text-[var(--muted)]">
            {i18nT('pages.mcpManagement.approval_command')}
          </span>
          <div
            role="list"
            aria-label={i18nT('pages.mcpManagement.approval_command')}
            className="mt-0.5 flex flex-wrap gap-1"
          >
            {launch.argv.map((argument, argumentIndex) => (
              <code
                key={argumentIndex}
                role="listitem"
                className="break-words rounded border border-[var(--border)] bg-[var(--surface-2,transparent)] px-1.5 py-1 font-mono text-[11px] text-[var(--text)]"
              >
                {visibleLaunchText(argument)}
              </code>
            ))}
          </div>
          {/* The environment is part of what approval runs: a changed value with
              the same command still counts. */}
          {launch.env.length > 0 && (
            <>
              <span className="mt-1 block text-[11px] font-medium text-[var(--muted)]">
                {i18nT('pages.mcpManagement.reapproval_env')}
              </span>
              <div
                role="list"
                aria-label={i18nT('pages.mcpManagement.reapproval_env')}
                className="mt-0.5 space-y-1"
              >
                {launch.env.map((entry, entryIndex) => (
                  <code
                    key={entryIndex}
                    role="listitem"
                    className="block break-words rounded border border-[var(--border)] bg-[var(--surface-2,transparent)] px-1.5 py-1 font-mono text-[11px] text-[var(--text)]"
                  >
                    {visibleLaunchText(entry)}
                  </code>
                ))}
              </div>
            </>
          )}
        </div>
      ))}
    </>
  )
}

/**
 * The review a STUB switch opens before anything is written.
 *
 * Turning the switch on writes nothing by itself, because a switch that posted
 * straight away asked the operator to approve a NAME: the command only became
 * visible afterwards, in the row the gateway had already refused. The decision and
 * the thing being decided now arrive together, and the write carries
 * `expected_launch` -- the identity of the launch ON SCREEN -- so a command that
 * moves between this render and the click is refused rather than approved blind.
 *
 * Turning a stub OFF stays one click: it only ever narrows what runs outside a
 * session, and a confirmation on the safe direction trains people to click through
 * the other one.
 */
export function LaunchReviewPanel({
  preview,
  busy,
  onApprove,
  onCancel,
}: {
  preview: LaunchPreview
  busy: boolean
  onApprove: () => void
  onCancel: () => void
}) {
  const panel = useRef<HTMLDivElement | null>(null)
  // Read through a ref so the listener is bound ONCE per reviewed server. Binding
  // it on the handler identity instead re-runs this effect every render, and the
  // focus call inside it would then drag focus back here while the operator is
  // reading -- the panel would fight its own keyboard user.
  const cancel = useRef(onCancel)
  cancel.current = onCancel
  useEffect(() => {
    const el = panel.current
    if (!el) return
    // Remembered so dismissing the panel hands focus back to the switch that
    // opened it, instead of dropping a keyboard user onto the page body.
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null
    // Focus follows the decision: the switch that opened this is now the wrong
    // control to be sitting on, and a keyboard user would otherwise have to hunt
    // forward through the table for the buttons that answer it.
    el.focus()
    // Escape leaves the decision unmade, as it does on this page's dialog. Bound
    // on the element rather than as a JSX prop so the panel stays a plain group
    // that owns its own keys, and stopped here so it reaches nothing outside.
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      e.stopPropagation()
      cancel.current()
    }
    el.addEventListener('keydown', onKey)
    return () => {
      el.removeEventListener('keydown', onKey)
      // Only when focus was inside the panel or has fallen to the body: a user who
      // already moved elsewhere keeps their place.
      const active = document.activeElement
      const lost = active === null || active === document.body || el.contains(active)
      if (lost && opener && opener !== el && opener.isConnected) opener.focus()
    }
  }, [preview.name])
  const approvable = launchIsApprovable(preview)
  return (
    <div
      ref={panel}
      tabIndex={-1}
      role="group"
      aria-label={i18nT('pages.mcpManagement.launch_review_title', { name: preview.name })}
      className="space-y-1.5 outline-none"
    >
      <p className="text-[13px] font-medium text-[var(--text)]">
        {i18nT('pages.mcpManagement.launch_review_title', { name: preview.name })}
      </p>
      {/* The decision copy at reading size and full contrast. This is the sentence
          the operator has to weigh, not a footnote about a state they already
          have, and 11px muted is how the old layout said the opposite. */}
      <p className="text-[12.5px] leading-relaxed text-[var(--text)]">
        {i18nT(
          approvable
            ? 'pages.mcpManagement.approval_consequence'
            : 'pages.mcpManagement.approval_not_approvable_here',
        )}
      </p>
      <LaunchLines launches={pairLaunches(preview.commands, preview.envs)} />
      {/* The switch stays off while this is open. Said here so the operator does
          not have to guess that approving is what turns it on. */}
      {approvable && (
        <p className="text-[12.5px] text-[var(--muted)]">
          {i18nT('pages.mcpManagement.launch_review_switch_pending')}
        </p>
      )}
      <div className="flex flex-wrap items-center gap-2 pt-1">
        {/* No approve button when the display was cut short: an approval names the
            launch it covers. Cancel stays either way, so this is never a dead end. */}
        {approvable && (
          <Btn
            type="button"
            disabled={busy}
            aria-label={i18nT('pages.mcpManagement.launch_review_approve_aria', {
              name: preview.name,
            })}
            onClick={onApprove}
          >
            {i18nT('pages.mcpManagement.reapprove_action', { name: preview.name })}
          </Btn>
        )}
        <button
          type="button"
          aria-label={i18nT('pages.mcpManagement.launch_review_cancel_aria', {
            name: preview.name,
          })}
          onClick={onCancel}
          className="rounded-md border border-[var(--border)] px-2.5 py-1 text-[13px] text-[var(--text)] transition-colors hover:bg-[var(--hover)]"
        >
          {i18nT('pages.mcpManagement.cancel')}
        </button>
      </div>
    </div>
  )
}

/**
 * A stubbed server the gateway will not launch until its command is approved.
 *
 * On a CHANGED command this shows both sides, because the question the operator
 * is actually being asked is what moved -- a bare "would run now" makes them
 * reconstruct the diff from memory, and the reason they are here is that it
 * changed. An older approval record kept no content, and that says so rather than
 * rendering an empty before-state as an approval of nothing.
 */
export function RefusalPanel({
  server,
  refusal,
  busy,
  onApprove,
  approveRef,
}: {
  server: McpManagedServer
  refusal: LaunchRefusal
  busy: boolean
  onApprove: () => void
  /** The row's STUB switch hands the keyboard here, so the decision the operator
   *  came to make is the next thing they reach. */
  approveRef?: (node: HTMLButtonElement | null) => void
}) {
  const approvable = launchIsApprovable(refusal)
  const changed = refusal.reason === 'changed_needs_reapproval'
  const before = pairLaunches(refusal.approved_commands, refusal.approved_envs)
  // The default reason line invites the operator to approve the command below it,
  // which is a dead end on a row nothing here can approve. The unapprovable
  // spelling drops that clause; why it cannot be approved is the line further
  // down, said once.
  const reasonKey =
    refusal.reason === 'added_outside_dashboard' && !approvable
      ? 'pages.mcpManagement.reapproval_reason_added_outside_dashboard_unapprovable'
      : REFUSAL_REASON_KEY[refusal.reason] ?? REFUSAL_REASON_KEY.changed_needs_reapproval
  return (
    <div className="space-y-1.5">
      <p className="text-[12.5px] leading-snug text-[var(--muted)]">{i18nT(reasonKey)}</p>
      <p className="text-[12.5px] leading-snug text-[var(--muted)]">
        {i18nT('pages.mcpManagement.reapproval_optin_kept')}
      </p>
      {/* Where the command comes from, so the operator can check the launch
          against its source before trusting it. */}
      {server.agents.length > 0 && (
        <p className="text-[12.5px] leading-snug text-[var(--muted)]">
          {i18nT('pages.mcpManagement.reapproval_declared_by', {
            agents: server.agents.join(', '),
          })}
        </p>
      )}
      <p className="text-[12.5px] leading-relaxed text-[var(--text)]">
        {i18nT(
          approvable
            ? 'pages.mcpManagement.approval_consequence'
            : 'pages.mcpManagement.approval_not_approvable_here',
        )}
      </p>
      {changed && (
        <div>
          <p className="text-[11px] font-semibold uppercase tracking-wider text-[var(--muted)]">
            {i18nT('pages.mcpManagement.approval_before_heading')}
          </p>
          {before.length > 0 ? (
            <LaunchLines launches={before} />
          ) : (
            <p className="mt-1 text-[12.5px] leading-snug text-[var(--muted)]">
              {i18nT('pages.mcpManagement.approval_before_unrecorded')}
            </p>
          )}
        </div>
      )}
      <div>
        {/* Headed only when there is a before-state to tell it apart from. On a
            server never approved here the single command needs no label saying
            which of two it is. */}
        {changed && (
          <p className="text-[11px] font-semibold uppercase tracking-wider text-[var(--muted)]">
            {i18nT('pages.mcpManagement.approval_now_heading')}
          </p>
        )}
        <LaunchLines launches={refusalLaunches(refusal)} />
      </div>
      {approvable && (
        <div className="pt-1">
          <Btn
            type="button"
            ref={approveRef}
            disabled={busy}
            aria-label={i18nT('pages.mcpManagement.reapprove_aria', { name: server.name })}
            onClick={onApprove}
            className="px-2 py-0.5 text-[12px]"
          >
            {i18nT('pages.mcpManagement.reapprove_action', { name: server.name })}
          </Btn>
        </div>
      )}
    </div>
  )
}
