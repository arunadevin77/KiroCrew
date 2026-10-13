import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo, useRef, useState } from 'react'
import EnvironmentHealthCard from './EnvironmentHealthCard'
import { Popover, PopoverContent, PopoverTrigger } from './ui/popover'
import { useTranslation } from 'react-i18next'
import type { TFunction } from 'i18next'
import { Cloud, Laptop, Loader2 } from 'lucide-react'
import { BoxdIcon, CoderIcon, FargateIcon, FlyIcon } from './BrandIcon'
import {
  api,
  type SessionEnvironmentPool,
  type SessionEnvironmentStartStage,
  type SlotEnvironment,
} from '../api/client'
import { fmtElapsed } from '../i18n/format'
import { useAppDispatch } from '../store'
import { setAgentSwitchNotice } from '../store/chatSlice'
import { agentSwitchFailureMessage } from '../utils/agentSwitchFeedback'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from './ui/dropdown-menu'

/** Radix radio values are strings; this one stands for "no profile". */
const LOCAL_VALUE = '__local__'
/** Start-stage poll cadence; also what ticks the elapsed time on the chip. */
const START_POLL_MS = 1000

/** Static keys (no template literal) so the i18n gates can see every one. */
function stageText(t: (key: string) => string, stage: SessionEnvironmentStartStage): string {
  switch (stage) {
    case 'checking': return t('sessionEnvironments.picker.stage.checking')
    case 'provisioning': return t('sessionEnvironments.picker.stage.provisioning')
    case 'connecting': return t('sessionEnvironments.picker.stage.connecting')
    case 'starting_agent': return t('sessionEnvironments.picker.stage.starting_agent')
    default: return t('sessionEnvironments.picker.stage.checking')
  }
}

export const slotEnvironmentQueryKey = (slot: string) => ['slot-environment', slot] as const

/** Hover-to-open delays: long enough that crossing the shelf does not flash
 *  the card, short enough to feel like a tooltip. */
const HOVER_OPEN_MS = 250
const HOVER_CLOSE_MS = 200

function LockedRemoteChip({ slot, name, title, tone, children }: {
  slot: string; name: string; title: string; tone: string; children: React.ReactNode
}) {
  const [open, setOpen] = useState(false)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const later = (next: boolean, ms: number) => {
    if (timer.current) clearTimeout(timer.current)
    timer.current = setTimeout(() => setOpen(next), ms)
  }
  useEffect(() => () => { if (timer.current) clearTimeout(timer.current) }, [])
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          data-testid="composer-environment-chip"
          title={title}
          aria-label={title}
          aria-haspopup="dialog"
          onMouseEnter={() => later(true, HOVER_OPEN_MS)}
          onMouseLeave={() => later(false, HOVER_CLOSE_MS)}
          className={`inline-flex items-center gap-1.5 h-7 min-w-0 text-[12px] px-2.5 rounded-md bg-transparent hover:bg-[color-mix(in_srgb,var(--bg-elevated)_84%,var(--text))] transition-colors border-none cursor-default ${tone}`}
        >
          {children}
        </button>
      </PopoverTrigger>
      <PopoverContent
        side="top"
        align="start"
        className="p-0 w-auto"
        onMouseEnter={() => later(true, 0)}
        onMouseLeave={() => later(false, HOVER_CLOSE_MS)}
        onOpenAutoFocus={(e: Event) => e.preventDefault()}
      >
        <EnvironmentHealthCard slot={slot} name={name} />
      </PopoverContent>
    </Popover>
  )
}

/** The remote environment a slot's session runs on, or null when it runs on
 *  this machine (or nothing is known yet). Shares the chip's query.
 *
 *  For views that otherwise read the gateway's local project (file tree, git,
 *  the project chip): a remote session's files are not there. */
export function useSlotRemoteEnvironment(
  slot: string | null | undefined,
): { profile: string; name: string; workdir: string } | null {
  const { data } = useQuery({
    queryKey: slotEnvironmentQueryKey(slot ?? ''),
    queryFn: () => api.chatSlotEnvironment(slot ?? ''),
    enabled: !!slot,
    staleTime: 30_000,
    retry: false,
  })
  const id = data?.profile ?? null
  const match = id ? data?.profiles?.find((p) => p.id === id) : undefined
  return useMemo(
    () => (id ? { profile: id, name: match?.name ?? id, workdir: match?.workdir ?? '' } : null),
    [id, match?.name, match?.workdir],
  )
}

/** "Starting session environment · 22 s" while ``slot``'s remote environment is starting
 *  during a running turn, else null. Shares the chip's query (same key), so the
 *  transcript's turn indicator and the chip read one poll, never two.
 *
 *  For the turn indicator: a first remote turn spends its opening minute(s)
 *  creating compute, and "Thinking…" there misdescribes what is happening. */
export function useEnvironmentStartLabel(
  slot: string | null | undefined,
  running: boolean,
  /** The session has replied before, so its environment connected at least once. */
  connectedBefore = false,
): EnvironmentStart | null {
  const { t } = useTranslation()
  const { data, dataUpdatedAt, refetch } = useQuery({
    queryKey: slotEnvironmentQueryKey(slot ?? ''),
    queryFn: () => api.chatSlotEnvironment(slot ?? ''),
    enabled: !!slot && running,
    staleTime: 30_000,
    retry: false,
    refetchInterval: (query) => (running && query.state.data?.profile ? START_POLL_MS : false),
  })
  // The cached record predates the turn (it says "not starting"), so without
  // an immediate read the first second of a start showed "Thinking…".
  const wasRunning = useRef(false)
  useEffect(() => {
    if (running && !wasRunning.current && slot) void refetch()
    wasRunning.current = running
  }, [running, slot, refetch])
  // When this turn began, for the gap before the backend reports its first
  // stage: the send, the turn queue and the session lookup all run before the
  // environment start does, and "Thinking…" there is wrong too.
  const turnStarted = useRef(0)
  if (running && !turnStarted.current) turnStarted.current = Date.now()
  if (!running) turnStarted.current = 0
  const remote = running && !!data?.profile
  // An active session's engine restart reconnects to a workspace that is
  // already up in a few seconds, and a setup checklist mid-conversation reads
  // as starting over. Only a restart that has to create or start compute again
  // (a stopped workspace) is worth the card; otherwise the turn shows as usual.
  // Two witnesses: the transcript (a reply means it connected before) and the
  // gateway (it already had this session attached). The composer chip passes
  // no transcript, so without the gateway's flag it alone kept saying
  // "Starting agent" while the transcript said "Thinking…".
  const reconnecting = (connectedBefore || !!data?.starting?.reconnect)
    && data?.starting?.stage !== 'provisioning'
    && !(data?.starting?.entered ?? []).includes('provisioning')
  const reported = remote && !reconnecting ? data?.starting ?? null : null
  // Not attached and nothing reported yet = setup has not begun: show it as
  // the first stage rather than falling through to "Thinking…".
  const pendingSetup = remote && !reconnecting && !reported && data?.attached === false
  const stageKey: SessionEnvironmentStartStage | null = reported?.stage ?? (pendingSetup ? 'checking' : null)
  const sincePoll = Math.max(0, Date.now() - dataUpdatedAt)
  // The ONE timer: how long the current step has run (shown on that step).
  const stageMs = reported
    ? reported.stage_elapsed_s * 1000 + sincePoll
    : pendingSetup ? Date.now() - turnStarted.current : 0
  const elapsed = stageKey ? fmtElapsed(stageMs) : ''
  const stage = stageKey ? stageText(t, stageKey) : ''
  const profileName = data?.profiles?.find((p) => p.id === data?.profile)?.name ?? data?.profile ?? ''
  const label = stageKey ? t('sessionEnvironments.picker.startingHeader', { name: profileName }) : ''
  // Stable identity between polls so the memoized footer does not re-render
  // on every unrelated parent render.
  return useMemo(() => {
    if (!stageKey || !label) return null
    const current = START_STAGE_ORDER.indexOf(stageKey)
    // Every stage before the current one is done, whether or not the gateway
    // had to run it: one it never entered still has its outcome (a warm pool
    // workspace is already started), and a dash there read as a failure. A
    // stage can also finish between two polls, so polled sightings could not
    // tell "ran" from "not needed" anyway.
    const steps = START_STAGE_ORDER.map((key, i): EnvironmentStartStep => ({
      key,
      label: stageText(t, key),
      state: i === current ? 'current' : i > current ? 'pending' : 'done',
    }))
    return { label, stage, elapsed, steps }
  }, [label, stage, elapsed, stageKey, t])
}

const START_STAGE_ORDER: SessionEnvironmentStartStage[] = ['checking', 'provisioning', 'connecting', 'starting_agent']

export interface EnvironmentStartStep {
  key: SessionEnvironmentStartStage
  label: string
  state: 'done' | 'skipped' | 'current' | 'pending'
}

export interface EnvironmentStart {
  /** "Setting up Coder MicroVM (dev)": the heading, with no timer of its own. */
  label: string
  /** The current stage alone, for live regions (the label ticks every second). */
  stage: string
  elapsed: string
  steps: EnvironmentStartStep[]
}

interface Props {
  slot: string
  /** A running turn keeps its environment: switching applies at the next start. */
  disabled?: boolean
  /** Narrow shelf: icon only, the name stays in the tooltip. */
  compact?: boolean
}

/** Composer chip choosing where a session's files and commands live: this
 *  machine, or one of the Session Environments profiles from Settings.
 *
 *  Renders nothing until profiles exist, so a gateway with no remote
 *  environment configured shows no new control. The endpoint is owner-only, so
 *  an app caller's 403 also renders nothing. Lives in its own separated group
 *  on the composer shelf (`max-two-buttons-per-row`). */
/** The provider's brand mark beside a profile, decorative (the name is the text
 * next to it). An unknown or missing provider, as from an older gateway, keeps
 * the generic cloud. */
function ProviderGlyph({ provider, size }: { provider?: string; size: number }) {
  const mark = provider === 'coder' ? <CoderIcon size={Math.round(size * 0.8)} />
    : provider === 'fargate' ? <FargateIcon size={size} />
      : provider === 'sprites' ? <FlyIcon size={size} />
        : provider === 'boxd' ? <BoxdIcon size={size} />
          : null
  if (!mark) return <Cloud size={size} className="shrink-0 opacity-80" aria-hidden />
  return <span data-testid={`provider-mark-${provider}`} className="inline-flex shrink-0 items-center" aria-hidden="true">{mark}</span>
}

/** One profile's hot pool in words: ready now, warming up, or none yet. */
function poolText(t: TFunction, pool: SessionEnvironmentPool | undefined): string {
  if (!pool || pool.target <= 0) return ''
  if (pool.ready > 0) return t('sessionEnvironments.pool.ready', { count: pool.ready })
  if (pool.warming > 0) return t('sessionEnvironments.pool.warming', { count: pool.warming })
  return t('sessionEnvironments.pool.none')
}

export default function SessionEnvironmentChip({ slot, disabled, compact }: Props) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const dispatch = useAppDispatch()
  const key = slotEnvironmentQueryKey(slot)
  const { data, refetch } = useQuery({
    queryKey: key,
    queryFn: () => api.chatSlotEnvironment(slot),
    staleTime: 30_000,
    retry: false,
    // While a turn runs on a remote environment, its first start may be
    // creating compute for minutes: poll the start stage so the chip can show
    // progress instead of an indistinguishable "thinking". Local sessions and
    // idle composers never poll.
    refetchInterval: (query) => (disabled && query.state.data?.profile ? START_POLL_MS : false),
  })
  // The first send is what locks the choice; re-read when that turn ends so
  // the chip settles into its locked form without waiting out staleTime.
  const wasRunning = useRef(false)
  useEffect(() => {
    if (wasRunning.current && !disabled) void refetch()
    wasRunning.current = !!disabled
  }, [disabled, refetch])
  // The same start the turn indicator shows (one query, one poll), including
  // the setup gap before the first stage is reported.
  const start = useEnvironmentStartLabel(slot, !!disabled)
  const choose = useMutation({
    mutationFn: (profile: string | null) => api.setChatSlotEnvironment(slot, profile),
    onSuccess: (result) => {
      queryClient.setQueryData<SlotEnvironment>(key, (old) =>
        old ? { ...old, profile: result.profile } : old,
      )
    },
    onError: (error: unknown) => {
      dispatch(setAgentSwitchNotice(agentSwitchFailureMessage(error)))
    },
  })

  if (!data || !Array.isArray(data.profiles) || data.profiles.length === 0) return null

  const pending = choose.isPending ? choose.variables : undefined
  const selected = pending !== undefined ? pending : data.profile
  const profile = data.profiles.find((p) => p.id === selected)
  // A choice naming a profile that was since removed still binds the session
  // (it fails closed at start), so show its id rather than "This machine".
  const name = profile?.name ?? selected ?? t('sessionEnvironments.picker.thisMachine')
  const remote = selected !== null
  // Only a start for the environment the slot is bound to; the stage is
  // cleared by the backend when the start ends (ready, failed or stopped).
  const starting = disabled && remote ? start : null
  const stageLabel = starting ? starting.stage : ''
  const elapsed = starting ? starting.elapsed : ''
  // Picked once for the session's life: the backend reports `locked` once the
  // session has a message, and refuses a change after that.
  const locked = !!data.locked
  // Before the first message, a pooled profile says whether a ready workspace
  // is waiting, because that decides whether the start skips provisioning.
  const pool = !locked && remote && !starting ? poolText(t, profile?.pool) : ''
  const title = starting
    ? t('sessionEnvironments.picker.startingTitle', { name, stage: stageLabel, elapsed })
    : locked
      ? t('sessionEnvironments.picker.lockedTitle', { name })
      : disabled
        ? t('sessionEnvironments.picker.busy')
        : pool
          ? t('sessionEnvironments.picker.runsOnPool', { name, pool })
          : t('sessionEnvironments.picker.runsOn', { name })
  const tone = remote ? 'text-accent' : 'text-muted'
  // Same chip, two forms: while the environment starts it carries the stage
  // and elapsed time in place of the name, then returns to the name.
  // motion-safe: reduced motion keeps a still glyph.
  const content = (
    <>
      {starting
        ? <Loader2 size={13} className="shrink-0 motion-safe:animate-spin" aria-hidden />
        : remote
          ? <ProviderGlyph provider={profile?.provider} size={13} />
          : <Laptop size={13} className="shrink-0 opacity-80" aria-hidden />}
      {!compact && (
        <span className="truncate max-w-[220px] tabular-nums">
          {/* Stage only: the running step in the transcript carries the timer. */}
          {starting ? stageLabel : name}
        </span>
      )}
      {!compact && pool && (
        <span data-testid="environment-pool" className="shrink-0 text-[11px] text-muted tabular-nums">{pool}</span>
      )}
    </>
  )
  const announce = <span className="sr-only" role="status" aria-live="polite">{stageLabel}</span>

  if (locked && !remote) {
    // A status, not a control: same place and form as the picker it was, so it
    // reads as that chip settling rather than a new element appearing.
    return (
      <div className="flex items-center min-w-0 shrink-0 pr-2 border-r border-border">
        <span
          data-testid="composer-environment-chip"
          title={title}
          aria-label={title}
          className={`inline-flex items-center gap-1.5 h-7 min-w-0 text-[12px] px-2.5 ${tone}`}
        >
          {content}
        </span>
        {announce}
      </div>
    )
  }
  if (locked) {
    // A fixed REMOTE environment: hover (or focus and click) shows its live
    // state and machine metrics. Same place and form as the picker it was.
    return (
      <div className="flex items-center min-w-0 shrink-0 pr-2 border-r border-border">
        <LockedRemoteChip slot={slot} name={name} title={title} tone={tone}>{content}</LockedRemoteChip>
        {announce}
      </div>
    )
  }

  return (
    <div className="flex items-center min-w-0 shrink-0 pr-2 border-r border-border">
      <DropdownMenu onOpenChange={(open) => { if (open) void refetch() }}>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            data-testid="composer-environment-chip"
            disabled={disabled}
            title={title}
            aria-label={title}
            className={`inline-flex items-center gap-1.5 h-7 min-w-0 text-[12px] px-2.5 rounded-md bg-transparent hover:bg-[color-mix(in_srgb,var(--bg-elevated)_84%,var(--text))] transition-colors border-none cursor-pointer disabled:cursor-not-allowed disabled:hover:bg-transparent ${remote ? 'text-accent' : 'text-muted hover:text-text disabled:hover:text-muted'}`}
          >
            {content}
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent
          align="start"
          aria-label={t('sessionEnvironments.picker.menuLabel')}
          className="min-w-[240px] max-w-[340px]"
        >
          <DropdownMenuLabel>{t('sessionEnvironments.picker.menuLabel')}</DropdownMenuLabel>
          <DropdownMenuRadioGroup
            value={selected ?? LOCAL_VALUE}
            onValueChange={(value: string) => {
              const next = value === LOCAL_VALUE ? null : value
              if (next !== data.profile) choose.mutate(next)
            }}
          >
            <DropdownMenuRadioItem value={LOCAL_VALUE} className="gap-2 text-[13px]">
              <Laptop className="lucide-inline shrink-0" aria-hidden />
              <span className="truncate">{t('sessionEnvironments.picker.thisMachine')}</span>
            </DropdownMenuRadioItem>
            <DropdownMenuSeparator />
            {data.profiles.map((p) => (
              <DropdownMenuRadioItem key={p.id} value={p.id} className="gap-2 text-[13px]" title={p.workdir}>
                <ProviderGlyph provider={p.provider} size={14} />
                <span className="flex flex-col min-w-0 flex-1">
                  <span className="truncate">{p.name}</span>
                  <span className="truncate text-[12px] text-muted font-mono">{p.workdir}</span>
                </span>
                {p.pool && p.pool.target > 0 && (
                  <span className={`shrink-0 text-[11px] tabular-nums ${p.pool.ready > 0 ? 'text-accent' : 'text-muted'}`}>
                    {poolText(t, p.pool)}
                  </span>
                )}
              </DropdownMenuRadioItem>
            ))}
          </DropdownMenuRadioGroup>
          <DropdownMenuSeparator />
          <p className="px-3 py-1.5 text-[11px] text-muted leading-snug">
            {t('sessionEnvironments.picker.appliesNext')}
          </p>
        </DropdownMenuContent>
      </DropdownMenu>
      {/* Announces each STAGE once. The visible label ticks every second, so
          it must not be the live region itself. */}
      {announce}
    </div>
  )
}
