/**
 * How the MCP page shows a launch before anyone shares it: the command and
 * environment lines, the launch shown beside a share button, a row's Details, and
 * the share action itself. `McpManagement` mounts them per row; its Share all list
 * reuses the launch lines.
 */
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Check, ChevronRight, Share2 } from 'lucide-react'
import {
  api,
  type McpManagedServer,
} from '../../api/client'
import { Btn } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { fmtBytes } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { type LaunchRefusal, type LaunchPreview, SHARE_BLOCKED_KEY } from './mcpManagementModel'

/** Every character the C category covers: escaped, never rendered, in a launch. */
const HIDDEN_LAUNCH_CHAR = /\p{C}/u

/** A launch value as text a reader can see all of: invisible characters escaped. */
function visibleLaunchText(value: string): string {
  return Array.from(value, character => {
    if (!HIDDEN_LAUNCH_CHAR.test(character)) return character
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

/** Characters an argument may hold and still show bare; anything else is quoted. */
const BARE_ARG = /^[A-Za-z0-9_@%+=:,./-]+$/

/** One argument quoted the way the card's `shlex.join` does, so argument
 *  boundaries survive the join: `sh -c 'echo ok'` never reads as `sh -c echo ok`. */
function quoteArg(arg: string): string {
  if (arg !== '' && BARE_ARG.test(arg)) return arg
  // A single quote, and the shell's way to put one inside a quoted argument.
  const quote = String.fromCharCode(39)
  const doubleQuote = String.fromCharCode(34)
  const escaped = quote + doubleQuote + quote + doubleQuote + quote
  return quote + arg.split(quote).join(escaped) + quote
}

/** Whether a row offers a share action that approves a launch: a first share, or a changed command. */
export function offersShare(server: McpManagedServer, waiting: boolean): boolean {
  return server.can_stub && (waiting || (!server.stub && !server.share_blocked))
}

/** The one read of what a server runs. Every surface uses this key, so what is shown is what is approved.
 *  The key carries whether the row waits on a changed command: a read cached before the
 *  change never stands in for the new one. */
function useLaunchPreview(name: string, enabled: boolean, waiting: boolean) {
  return useQuery<LaunchPreview>({
    queryKey: ['mcpGatewayLaunchPreview', name, waiting],
    queryFn: () => api.mcpGatewayLaunchPreview(name),
    enabled,
    retry: false,
  })
}

/** Every launch a server resolves to: its command, its env names, then their values. */
export function LaunchList({ preview }: { preview?: LaunchPreview }) {
  const launches = (preview?.commands ?? [])
    .map((argv, i) => ({ argv, env: preview?.envs?.[i] ?? [] }))
    .filter(launch => launch.argv.length > 0)
  return (
    <>
      {launches.length > 1 && (
        <p>{i18nT('pages.mcpManagement.details_variants', { count: launches.length })}</p>
      )}
      {launches.map((launch, i) => (
        <div key={i} className="space-y-0.5">
          {/* pre-wrap: runs of spaces inside an argument must show as they run. */}
          <code className="block whitespace-pre-wrap break-words font-mono text-[11.5px] text-[var(--text)] [overflow-wrap:anywhere]">
            {launch.argv.map(arg => visibleLaunchText(quoteArg(arg))).join(' ')}
          </code>
          {launch.env.length > 0 && (
            <ul aria-label={i18nT('pages.mcpManagement.reapproval_env')} className="space-y-0.5">
              {launch.env.map((entry, entryIndex) => (
                <li
                  key={entryIndex}
                  className="whitespace-pre-wrap break-words font-mono text-[11px] text-[var(--muted)] [overflow-wrap:anywhere]"
                >
                  {visibleLaunchText(entry)}
                </li>
              ))}
            </ul>
          )}
        </div>
      ))}
    </>
  )
}

/**
 * What a share would approve, shown in the row before its button can be
 * pressed. A shared copy runs as the person outside the chat sandbox, and the
 * command can be agent-written, so an approval covers only a launch shown here.
 */
export function ShareLaunch({
  server,
  waiting,
  refusal,
  alsoStarts,
}: {
  server: McpManagedServer
  waiting: boolean
  refusal?: LaunchRefusal
  /** Marked servers that start sharing too, because this press turns sharing on. */
  alsoStarts: string[]
}) {
  const offered = offersShare(server, waiting)
  const preview = useLaunchPreview(server.name, offered, waiting)
  if (!offered || !preview.data?.complete) return null
  const approved = waiting && refusal?.approved_commands?.length
    ? { name: server.name, commands: refusal.approved_commands, envs: refusal.approved_envs ?? [], complete: true }
    : null
  return (
    <div className="mt-2 space-y-1.5 rounded-lg border border-[var(--border)] px-3 py-2 text-[12px] leading-relaxed text-[var(--muted)]">
      <p className="font-medium text-[var(--text)]">
        {i18nT(waiting ? 'pages.mcpManagement.share_runs_new' : 'pages.mcpManagement.share_runs')}
      </p>
      <p>{i18nT('pages.mcpManagement.share_access')}</p>
      {server.agents.length > 0 && (
        <p className="break-words [overflow-wrap:anywhere]">
          {i18nT('pages.mcpManagement.share_set_in', { agents: server.agents.join(', ') })}
        </p>
      )}
      <LaunchList preview={preview.data} />
      {approved && (
        <div className="space-y-1 border-t border-[var(--border)] pt-1.5">
          <p className="font-medium text-[var(--text)]">{i18nT('pages.mcpManagement.share_approved_before')}</p>
          <LaunchList preview={approved} />
        </div>
      )}
      {alsoStarts.length > 0 && (
        <p className="border-t border-[var(--border)] pt-1.5 text-[var(--text)]">
          {i18nT('pages.mcpManagement.share_also_starts', { names: alsoStarts.join(', ') })}
        </p>
      )}
    </div>
  )
}

/**
 * The debugging view of a server, collapsed by default and loaded on open:
 * which agents declare it, and, when the row does not already show it, every
 * launch it resolves to.
 */
export function ServerDetails({ server, showLaunch }: { server: McpManagedServer; showLaunch: boolean }) {
  const [open, setOpen] = useState(false)
  const preview = useLaunchPreview(server.name, open && showLaunch && server.can_stub, false)
  return (
    <details
      className="group mt-1"
      open={open}
      onToggle={e => setOpen((e.currentTarget as HTMLDetailsElement).open)}
    >
      <summary className="inline-flex cursor-pointer list-none items-center gap-1 text-[12px] text-[var(--muted)] hover:text-[var(--text)] [&::-webkit-details-marker]:hidden">
        <ChevronRight size={12} className="transition-transform group-open:rotate-90" aria-hidden="true" />
        {i18nT('pages.mcpManagement.details')}
      </summary>
      <div className="mt-1.5 space-y-1.5 pl-4 text-[12px] leading-relaxed text-[var(--muted)]">
        <p className="break-words [overflow-wrap:anywhere]">
          {i18nT('pages.mcpManagement.details_used_by', { agents: server.agents.join(', ') })}
        </p>
        {open && preview.isLoading && (
          <p role="status">{i18nT('pages.mcpManagement.details_loading')}</p>
        )}
        {preview.isError && (
          <ErrorNotice variant="inline" message={i18nT('pages.mcpManagement.details_failed')} askAgent />
        )}
        {showLaunch && <LaunchList preview={preview.data} />}
      </div>
    </details>
  )
}

/** Every share action is a real, labelled button, never a bare switch or link. */
export const SHARE_BUTTON_CLASS =
  'shrink-0 px-3 py-1.5 font-medium focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--accent)]'

/**
 * The one action a row offers. A shared copy runs outside the chat sandbox, so
 * a command that changed after it was shared waits here until the person
 * confirms the new one, which the row shows above its buttons and pins.
 */
export function ShareAction({
  server,
  waiting,
  busy,
  supported,
  describedBy,
  onShare,
}: {
  server: McpManagedServer
  waiting: boolean
  busy: boolean
  supported: boolean
  describedBy?: string
  onShare: (stub: boolean, expectedLaunch?: string) => void
}) {
  // Same key as the launch the row shows, so the launch approved is the one
  // on screen. Both share actions send the `expected_launch` they read and
  // stay disabled until there is one.
  const offered = offersShare(server, waiting)
  const preview = useLaunchPreview(server.name, offered, waiting)
  const expected = preview.data?.complete ? preview.data.expected_launch : undefined
  if (!server.can_stub) return null
  const stopButton = (
    <Btn
      type="button"
      className={SHARE_BUTTON_CLASS}
      disabled={busy}
      aria-label={i18nT('pages.mcpManagement.unshare_button_aria', { name: server.name })}
      aria-describedby={describedBy}
      onClick={() => onShare(false)}
    >
      {i18nT('pages.mcpManagement.unshare_button')}
    </Btn>
  )
  if (waiting) {
    // Stop sharing stays reachable: a changed launch the person cannot or will
    // not confirm must still be turnable off.
    return (
      <div className="flex w-full min-w-0 flex-wrap items-center gap-2 md:w-auto md:justify-end">
        {stopButton}
        <Btn
          type="button"
          primary
          className={SHARE_BUTTON_CLASS}
          disabled={busy || !supported || !expected}
          aria-label={i18nT('pages.mcpManagement.new_command_aria', { name: server.name })}
          aria-describedby={describedBy}
          onClick={() => onShare(true, expected)}
        >
          <Check size={14} aria-hidden="true" />
          {i18nT('pages.mcpManagement.new_command_button')}
        </Btn>
        {preview.isError && (
          <div className="basis-full">
            <ErrorNotice variant="inline" message={i18nT('pages.mcpManagement.share_unreadable')} askAgent />
          </div>
        )}
      </div>
    )
  }
  if (server.stub) return stopButton
  if (server.share_blocked) return null
  // A launch read that answered but gave nothing to pin cannot be shared at
  // all: say so in place of the button, like a refused server.
  if (preview.isSuccess && !expected) {
    return (
      <span className="text-[12px] text-[var(--muted)] md:text-right">
        {i18nT('pages.mcpManagement.share_unreadable')}
      </span>
    )
  }
  // Say why the button is off once the read is done and gave nothing to pin;
  // a failed read is an error and goes through ErrorNotice.
  return (
    <div className="flex w-full min-w-0 flex-col items-start gap-1 md:w-auto md:items-end">
      <Btn
        type="button"
        primary
        className={SHARE_BUTTON_CLASS}
        disabled={busy || !supported || !expected}
        aria-label={i18nT('pages.mcpManagement.stub_aria', { name: server.name })}
        aria-describedby={describedBy}
        onClick={() => onShare(true, expected)}
      >
        <Share2 size={14} aria-hidden="true" />
        {i18nT('pages.mcpManagement.share_button')}
      </Btn>
      {preview.isLoading && (
        <span role="status" className="text-[12px] text-[var(--muted)]">
          {i18nT('pages.mcpManagement.share_checking')}
        </span>
      )}
      {preview.isError && (
        <ErrorNotice variant="inline" message={i18nT('pages.mcpManagement.share_unreadable')} askAgent />
      )}
    </div>
  )
}

/** The one hint under a row's switch: why it is off-limits, or what it saves. */
export function shareHint(s: McpManagedServer): string | null {
  if (s.share_blocked) {
    return i18nT(SHARE_BLOCKED_KEY[s.share_blocked] ?? 'pages.mcpManagement.stub_error_share_refused')
  }
  if (!s.can_stub || s.stub) return null
  if (typeof s.saves_bytes === 'number' && s.saves_bytes > 0) {
    return i18nT('pages.mcpManagement.share_saves', {
      size: fmtBytes(s.saves_bytes, { maximumFractionDigits: 0 }),
    })
  }
  // No measurement, no line: "Saves memory" alone says nothing a number would not.
  return null
}
