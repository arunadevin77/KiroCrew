import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, ExternalLink, ListChecks, RefreshCw, Server as ServerIcon, Share2 } from 'lucide-react'
import {
  api,
  ApiError,
  type McpManagedServer,
  type McpMeasureProgress,
} from '../../api/client'
import {
  Tabs,
  TabsContent,
  TabsCount,
  TabsList,
  TabsTrigger,
  type TabItem,
} from '../../components/ui/tabs'
import { TABS_RAIL_ROW_CLASS } from '../../components/ui/tabsPill'
import { Btn } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { splitOnPlaceholder } from '../../lib/splitOnPlaceholder'
import { i18nT } from '../../i18n/t'
import {
  DOCS_URL,
  type GatewayStatus,
  type LaunchPreview,
  stubErrorKey,
  type McpView,
  rowState,
  stateLabelKey,
  CONTRARY_STRENGTHS,
  sharedWithoutSupport,
  waitForMeasurePass,
} from './mcpManagementModel'
import {
  offersShare,
  LaunchList,
  ShareLaunch,
  ServerDetails,
  SHARE_BUTTON_CLASS,
  ShareAction,
  shareHint,
} from './McpManagementLaunchReview'
import { AssessmentView } from './McpManagementAssessment'

/**
 * MCP Management: one button per server, and what it buys.
 *
 * Every action is a real, labelled button. "Share this server" shares it: one
 * request records the exact launch it runs and marks it shared, and turns the
 * page-wide sharing switch on if it was off. A shared copy runs outside the
 * chat sandbox, so the row shows the launch it approves above the button, and
 * the approval covers that launch only: when the command, args or declared env
 * later change, the row reads "Paused · command changed", shows the new
 * command, and waits for "Use new command", which pins the launch it
 * showed. "Stop sharing" turns it off. "Share all" lists every launch first and
 * shares only on the button under that list.
 *
 * The servers that can never be shared (they give each chat its own
 * credentials, or its own desktop) arrive with `share_blocked` set: they offer
 * no share button and say why in one line.
 *
 * The hint under each row is a measured saving or nothing. `saves_bytes` is
 * read from this host's process table by the gateway; when it is absent there
 * is no hint. Never estimate one here.
 *
 * The second sub-view, "Sharing assessment", is read-only evidence for anyone
 * who wants to know why a server is or is not a good fit. It changes nothing.
 */

function Switch({
  on,
  disabled,
  onClick,
  label,
  describedBy,
}: {
  on: boolean
  disabled?: boolean
  onClick: () => void
  label: string
  describedBy?: string
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={label}
      aria-describedby={describedBy}
      disabled={disabled}
      onClick={onClick}
      className={[
        'relative inline-flex h-[22px] w-[38px] shrink-0 items-center rounded-full transition-colors',
        on ? 'bg-accent' : 'bg-[var(--border-strong,var(--border))]',
        disabled ? 'cursor-not-allowed opacity-50' : 'cursor-pointer',
      ].join(' ')}
    >
      <span
        className={[
          // The knob rides ON the accent fill, so it needs the same light face in
          // every theme; there is no token for that pairing.
          'absolute h-[18px] w-[18px] rounded-full bg-white shadow transition-all',
          on ? 'left-[18px]' : 'left-[2px]',
        ].join(' ')}
      />
    </button>
  )
}

export function McpManagement() {
  const qc = useQueryClient()
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  // A change that waits for a restart is information, not a failure.
  const [restartNotice, setRestartNotice] = useState<string | null>(null)
  const [resolveNotice, setResolveNotice] = useState<string | null>(null)
  const [resolveError, setResolveError] = useState<string | null>(null)

  const statusQ = useQuery<GatewayStatus>({
    queryKey: ['mcpGatewayStatus'],
    queryFn: () => api.mcpGatewayStatus(),
  })
  const serversQ = useQuery<{ servers: McpManagedServer[] }>({
    queryKey: ['mcpGatewayServers'],
    queryFn: () => api.mcpGatewayServers(),
  })

  const status = statusQ.data
  const servers = useMemo(() => serversQ.data?.servers ?? [], [serversQ.data])
  const stubCount = useMemo(() => servers.filter(s => s.stub).length, [servers])
  // Marked servers the assessment argues against: they start the moment
  // page-wide sharing turns on, so the page names them before that press.
  const contraryMarked = useMemo(
    () =>
      servers
        .filter(s => s.stub && s.recommendation && CONTRARY_STRENGTHS.has(s.recommendation.strength))
        .map(s => s.name),
    [servers],
  )
  // The header counts the rows whose chip reads "Shared": a row waiting for its
  // new command, or one paused because sharing is off, is not counted.
  const switchedOnCount = useMemo(
    () =>
      servers.filter(s => rowState(s, !!status?.enabled, status?.launch_refused) === 'shared').length,
    [servers, status],
  )
  const supported = status?.supported ?? true

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ['mcpGatewayStatus'] })
    void qc.invalidateQueries({ queryKey: ['mcpGatewayServers'] })
    // A launch can change with any of these writes; never show a stale one beside a button.
    void qc.invalidateQueries({ queryKey: ['mcpGatewayLaunchPreview'] })
  }

  const setSharing = useMutation({
    mutationFn: (enabled: boolean) => api.mcpGatewayEnable(enabled),
    // Enabling can persist and still leave sharing unreachable; `ping_ok` says so.
    onSuccess: (res, enabled) => {
      invalidate()
      if (enabled && !res.ping_ok) setError(i18nT('pages.mcpManagement.sharing_not_live'))
    },
    onError: () => {
      invalidate()
      setError(i18nT('pages.mcpManagement.sharing_failed'))
    },
  })

  // One press, one request. Sharing records the exact launch the server runs and
  // marks it shared; the endpoint refuses the classes that can never be shared.
  // Confirming a changed command pins the launch the opened Details showed, so a
  // launch that moved again in between is refused rather than approved. A row
  // shared while sharing is off turns sharing on too: the person asked for this
  // server to be shared, and a second switch elsewhere would make that two steps.
  const setStub = useMutation({
    mutationFn: ({ name, stub, expectedLaunch }: { name: string; stub: boolean; expectedLaunch?: string }) =>
      api.mcpGatewaySetStub(name, stub, expectedLaunch),
    onSuccess: (res, vars) => {
      invalidate()
      if (vars.stub && supported && !status?.enabled && !setSharing.isPending) {
        setSharing.mutate(true)
      }
      if (res && res.restart_required) {
        setRestartNotice(i18nT('pages.mcpManagement.stub_restart_required'))
      } else if (res && res.applied === false) {
        setError(i18nT('pages.mcpManagement.stub_not_live'))
      }
    },
    onError: (err, vars) => {
      invalidate()
      // A refusal can mean the launch moved since it was shown: read it again,
      // so the row shows the current launch and the next press pins that one.
      void qc.invalidateQueries({ queryKey: ['mcpGatewayLaunchPreview', vars.name] })
      setError(i18nT(stubErrorKey(err), { name: vars.name }))
    },
  })

  const resolveRefresh = useMutation({
    mutationFn: () => api.mcpResolveRefresh(),
    onSuccess: res => {
      invalidate()
      if (!res.ok) {
        setResolveNotice(i18nT('pages.mcpManagement.resolve_no_targets'))
        return
      }
      const ready = res.ready?.length ?? 0
      // Every install failing must not read as "already up to date".
      const failed = Object.values(res.resolved ?? {}).filter(state => state === 'error').length
      if (failed > 0) {
        setResolveError(
          ready > 0
            ? i18nT('pages.mcpManagement.resolve_partly_ready', {
                ready: String(ready),
                failed: String(failed),
              })
            : i18nT('pages.mcpManagement.resolve_all_failed', { failed: String(failed) }),
        )
        return
      }
      setResolveNotice(
        ready > 0
          ? i18nT('pages.mcpManagement.resolve_ready', { ready: String(ready) })
          : i18nT('pages.mcpManagement.resolve_none_ready'),
      )
    },
    onError: err => {
      invalidate()
      // 409 is a pass already running elsewhere: information, not a failure.
      if (err instanceof ApiError && err.status === 409) {
        setResolveNotice(i18nT('pages.mcpManagement.resolve_already_running'))
        return
      }
      setError(i18nT('pages.mcpManagement.resolve_failed'))
    },
  })

  const busy = setStub.isPending || setSharing.isPending
  // One count shared by the tab badge and the assessment banner.
  const unsupportedCount = useMemo(
    () =>
      servers.filter(s => sharedWithoutSupport(s, !!status?.enabled, status?.launch_refused)).length,
    [servers, status?.enabled, status?.launch_refused],
  )
  const canEnableSharing = supported && stubCount > 0

  // Rows the measurement pass would act on, including an older gateway's row
  // with no verdict and a handshake that did not reproduce (re-measured every pass).
  const unmeasuredCount = useMemo(
    () =>
      servers.filter(
        s =>
          !s.recommendation ||
          s.recommendation.strength === 'unknown' ||
          s.recommendation.reasons.some(r => r.code === 'handshake_not_reproducible'),
      ).length,
    [servers],
  )

  const { data: measureProgress } = useQuery<McpMeasureProgress>({
    queryKey: ['mcp-measure-progress'],
    queryFn: () => api.mcpMeasureProgress(),
    enabled: false,
  })
  const stubbableNames = useMemo(
    () => servers.filter(s => s.can_stub && !s.stub && !s.share_blocked).map(s => s.name),
    [servers],
  )

  // "Share all": measure the unmeasured first, then read each candidate's launch
  // and SHOW them all. Each share approves that exact launch to run outside the
  // chat sandbox, so nothing is shared until the person presses the button
  // under the list, and each share is pinned to the launch the list shows.
  const [allPlan, setAllPlan] = useState<Array<{ name: string; preview: LaunchPreview; expected: string }> | null>(null)
  const prepareAll = useMutation({
    mutationFn: async () => {
      if (unmeasuredCount > 0) {
        await api.mcpMeasureStart()
        if (!(await waitForMeasurePass(qc))) return { pending: true as const }
      }
      // Re-read directly: the shared QueryClient never treats its cache as stale.
      const fresh = await api.mcpGatewayServers()
      const candidates = (fresh?.servers ?? [])
        .filter(s => s.can_stub && !s.stub && !s.share_blocked)
        .map(s => s.name)
      const plan: Array<{ name: string; preview: LaunchPreview; expected: string }> = []
      let unreadable = 0
      const failed: string[] = []
      for (const name of candidates) {
        try {
          const preview = await api.mcpGatewayLaunchPreview(name)
          if (preview?.complete && preview.expected_launch) {
            plan.push({ name, preview, expected: preview.expected_launch })
          } else unreadable++
        } catch {
          // A failed read is an error, not "nothing to share".
          failed.push(name)
        }
      }
      return { pending: false as const, plan, unreadable, failed }
    },
    onSuccess: r => {
      if (r.pending) {
        setNotice(i18nT('pages.mcpManagement.enable_all_still_measuring'))
        return
      }
      if (r.failed.length > 0) {
        setError(i18nT('pages.mcpManagement.enable_all_review_failed_names', { names: r.failed.join(', ') }))
      }
      if (r.plan.length === 0) {
        if (r.failed.length === 0) setNotice(i18nT('pages.mcpManagement.enable_all_review_nothing'))
        return
      }
      setAllPlan(r.plan)
      if (r.unreadable > 0) {
        setNotice(i18nT('pages.mcpManagement.enable_all_review_result_left', { count: r.unreadable }))
      }
    },
    onError: () => setError(i18nT('pages.mcpManagement.stub_failed')),
  })
  const enableAll = useMutation({
    mutationFn: async (plan: Array<{ name: string; expected: string }>) => {
      let shared = 0
      let skipped = 0
      const failed: string[] = []
      let restart = false
      let notLive = false
      // Sharing goes on BEFORE the writes, so each server is checked against the
      // bar for a pooled copy, not the looser bar for a server left unpooled.
      // Enabling can persist and still leave sharing unreachable; `ping_ok` says so.
      if (supported && !status?.enabled) {
        const on = await api.mcpGatewayEnable(true)
        if (!on.ping_ok) notLive = true
      }
      const skippedNames: string[] = []
      for (const { name, expected } of plan) {
        try {
          const res = await api.mcpGatewaySetStub(name, true, expected, true)
          if (res.stubbed?.includes(name)) shared++
          else {
            skipped++
            skippedNames.push(name)
          }
          if (res.restart_required) restart = true
        } catch {
          failed.push(name)
        }
      }
      return { shared, skipped, skippedNames, failed, restart, notLive }
    },
    onSuccess: r => {
      invalidate()
      setAllPlan(null)
      setNotice(
        [
          i18nT('pages.mcpManagement.enable_all_review_result_shared', { count: r.shared }),
          ...(r.skipped > 0
            ? [i18nT('pages.mcpManagement.enable_all_review_skipped_names', { names: r.skippedNames.join(', ') })]
            : []),
        ].join(' '),
      )
      // Failed requests and a sharing switch that did not come up are errors:
      // they go through ErrorNotice, not the status line.
      const errors = [
        ...(r.notLive ? [i18nT('pages.mcpManagement.sharing_not_live')] : []),
        ...(r.failed.length > 0
          ? [i18nT('pages.mcpManagement.enable_all_review_failed_names', { names: r.failed.join(', ') })]
          : []),
      ]
      if (errors.length > 0) setError(errors.join(' '))
      if (r.restart) setRestartNotice(i18nT('pages.mcpManagement.stub_restart_required'))
    },
    onError: () => {
      invalidate()
      setError(i18nT('pages.mcpManagement.stub_failed'))
    },
  })

  const [view, setView] = useState<McpView>('servers')
  const views: Array<TabItem<McpView>> = [
    {
      key: 'servers',
      label: i18nT('pages.mcpManagement.view_servers'),
      icon: <ServerIcon size={14} />,
    },
    {
      key: 'assessment',
      label: i18nT('pages.mcpManagement.view_assessment'),
      icon: <ListChecks size={14} />,
      count: unsupportedCount,
    },
  ]

  return (
    <Tabs
      value={view}
      onValueChange={v => setView(v as McpView)}
      layoutId="mcp-management-view"
      className="min-w-0 space-y-4"
    >
      <div className={TABS_RAIL_ROW_CLASS}>
        <TabsList aria-label={i18nT('pages.mcpManagement.views_aria')}>
          {views.map(v => (
            <TabsTrigger key={v.key} value={v.key}>
              {v.icon}
              <span>{v.label}</span>
              <TabsCount value={v.count} />
            </TabsTrigger>
          ))}
        </TabsList>
      </div>

      <ErrorNotice
        message={statusQ.isError ? i18nT('pages.mcpManagement.status_failed') : null}
        askAgent
      />

      <TabsContent value="assessment" className="min-w-0">
        <AssessmentView
          servers={servers}
          sharingOn={!!status?.enabled}
          refused={status?.launch_refused}
          loading={serversQ.isLoading}
          isError={serversQ.isError}
          onOpenServers={() => setView('servers')}
          unsupportedCount={unsupportedCount}
          unmeasuredCount={unmeasuredCount}
        />
      </TabsContent>
      <TabsContent value="servers" className="min-w-0 space-y-4">
      <p className="max-w-[76ch] text-[13px] leading-relaxed text-[var(--muted)]">
        {/* One key, one sentence, a {{link}} placeholder a translator can move. */}
        {splitOnPlaceholder(i18nT('pages.mcpManagement.lede'), 'link').map((part, i) =>
          part === null ? (
            <a
              key="link"
              href={DOCS_URL}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1 text-[var(--accent)] hover:underline"
            >
              {i18nT('pages.mcpManagement.learn_more')}
              <ExternalLink size={12} />
            </a>
          ) : (
            <span key={i}>{part}</span>
          ),
        )}
      </p>

      <ErrorNotice message={error} askAgent onDismiss={() => setError(null)} />

      {restartNotice && (
        <div
          role="status"
          className="flex items-start gap-2 rounded-lg border border-[var(--accent)] bg-[var(--accent-subtle,transparent)] px-3.5 py-2.5 text-[13px] text-[var(--text)]"
        >
          <RefreshCw size={14} className="mt-0.5 shrink-0 text-[var(--accent)]" aria-hidden="true" />
          <span>{restartNotice}</span>
        </div>
      )}

      {/* Per server: one labelled button is the decision. */}
      <section className="overflow-hidden rounded-xl border border-[var(--border)] bg-[var(--card)]">
        <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1.5 border-b border-[var(--border)] px-4 py-2.5">
          <span className="text-[12.5px] text-[var(--muted)]">
            {i18nT('pages.mcpManagement.stubbed_of_total', {
              stubbed: switchedOnCount,
              // Every row below, so the count and the list always agree.
              total: servers.length,
            })}
          </span>
          <Btn
            type="button"
            className={SHARE_BUTTON_CLASS}
            onClick={() => {
              setError(null)
              setNotice(null)
              setRestartNotice(null)
              prepareAll.mutate()
            }}
            disabled={
              busy
              || prepareAll.isPending
              || enableAll.isPending
              || allPlan !== null
              || serversQ.isLoading
              || !supported
              || (stubbableNames.length === 0 && unmeasuredCount === 0)
            }
          >
            {i18nT('pages.mcpManagement.enable_all')}
          </Btn>
          {(prepareAll.isPending || enableAll.isPending || notice) && (
            <span role="status" className="basis-full text-[12.5px] text-[var(--muted)]">
              {prepareAll.isPending && (measureProgress?.running || unmeasuredCount > 0)
                ? i18nT('pages.mcpManagement.assessment.measure_running', {
                    done: measureProgress?.running ? measureProgress.done : 0,
                    total: measureProgress?.running ? measureProgress.total : unmeasuredCount,
                  })
                : prepareAll.isPending
                  ? i18nT('pages.mcpManagement.share_checking')
                  : notice}
            </span>
          )}
        </div>
        {allPlan && (
          <div
            role="region"
            aria-label={i18nT('pages.mcpManagement.share_all_list_aria')}
            className="space-y-3 border-b border-[var(--border)] px-4 py-3 text-[12px] leading-relaxed text-[var(--muted)]"
          >
            <p className="font-medium text-[var(--text)]">{i18nT('pages.mcpManagement.share_all_runs')}</p>
            <p>{i18nT('pages.mcpManagement.share_all_access')}</p>
            <ul className="space-y-2.5">
              {allPlan.map(item => (
                <li key={item.name} className="space-y-1 rounded-lg border border-[var(--border)] px-3 py-2">
                  <p className="font-mono text-[12.5px] text-[var(--text)]">{item.name}</p>
                  <LaunchList preview={item.preview} />
                </li>
              ))}
            </ul>
            {!status?.enabled && servers.some(o => o.stub) && (
              <p className="text-[var(--text)]">
                {i18nT('pages.mcpManagement.share_also_starts', {
                  names: servers.filter(o => o.stub).map(o => o.name).join(', '),
                })}
              </p>
            )}
            <div className="flex flex-wrap items-center gap-2">
              <Btn
                type="button"
                primary
                className={SHARE_BUTTON_CLASS}
                disabled={enableAll.isPending || busy}
                onClick={() => enableAll.mutate(allPlan)}
              >
                <Share2 size={14} aria-hidden="true" />
                {i18nT('pages.mcpManagement.share_all_confirm', { count: allPlan.length })}
              </Btn>
              <Btn
                type="button"
                className={SHARE_BUTTON_CLASS}
                disabled={enableAll.isPending}
                onClick={() => {
                  setAllPlan(null)
                  setNotice(null)
                }}
              >
                {i18nT('pages.mcpManagement.share_all_cancel')}
              </Btn>
            </div>
          </div>
        )}
        <ul className="divide-y divide-[var(--border)]">
          {servers.map(s => {
            const state = rowState(s, !!status?.enabled, status?.launch_refused)
            const waiting = state === 'needs_reapproval'
            const flagged = sharedWithoutSupport(s, !!status?.enabled, status?.launch_refused)
            const hint = waiting
              ? i18nT('pages.mcpManagement.command_changed_hint')
              : state === 'direct_env'
                ? i18nT('pages.mcpManagement.state_direct_env_reason')
                : shareHint(s)
            const hintId = `mcp-share-hint-${s.name}`
            return (
              <li key={s.name} className="px-4 py-3">
                <div className="flex min-w-0 flex-col gap-3 md:flex-row md:items-start">
                  <div className="min-w-0 flex-1">
                    <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
                      <span className="min-w-0 break-words font-mono text-[13px] text-[var(--text)] [overflow-wrap:anywhere]">
                        {s.name}
                      </span>
                      <span
                        className={[
                          'inline-flex items-center gap-1 whitespace-nowrap rounded-full px-2 py-0.5 text-[11px]',
                          flagged
                            ? 'border border-[var(--danger)] text-[var(--danger)]'
                            : state === 'shared'
                              ? 'bg-[var(--accent-subtle,transparent)] text-[var(--accent)]'
                              : 'text-[var(--muted)]',
                        ].join(' ')}
                      >
                        {flagged && <AlertTriangle size={11} aria-hidden="true" />}
                        {i18nT(stateLabelKey(state))}
                      </span>
                    </div>
                    {hint && (
                      <p id={hintId} className="mt-0.5 text-[12.5px] text-[var(--muted)]">
                        {hint}
                      </p>
                    )}
                    <ShareLaunch
                      server={s}
                      waiting={waiting}
                      refusal={status?.launch_refused?.[s.name]}
                      alsoStarts={
                        status?.enabled || waiting
                          ? []
                          : servers.filter(o => o.stub && o.name !== s.name).map(o => o.name)
                      }
                    />
                    <ServerDetails server={s} showLaunch={!offersShare(s, waiting)} />
                  </div>
                  <ShareAction
                    server={s}
                    waiting={waiting}
                    busy={busy}
                    supported={supported}
                    describedBy={hint ? hintId : undefined}
                    onShare={(stub, expectedLaunch) => {
                      setError(null)
                      setRestartNotice(null)
                      setStub.mutate({ name: s.name, stub, expectedLaunch })
                    }}
                  />
                </div>
              </li>
            )
          })}
          {serversQ.isError && (
            <li className="px-4 py-4">
              <ErrorNotice message={i18nT('pages.mcpManagement.servers_failed')} askAgent />
            </li>
          )}
          {servers.length === 0 && !serversQ.isLoading && !serversQ.isError && (
            <li className="px-4 py-6 text-center text-[13px] text-[var(--muted)]">
              {i18nT('pages.mcpManagement.no_servers')}
            </li>
          )}
        </ul>
        <p className="border-t border-[var(--border)] px-4 py-2.5 text-[12.5px] text-[var(--muted)]">
          {i18nT('pages.mcpManagement.open_sessions_note')}
        </p>
      </section>

      {/* Page-wide: the main sharing switch. A row's switch turns it on by itself. */}
      <section className="rounded-xl border border-[var(--border)] bg-[var(--card)] px-4 py-3">
        <div className="flex min-w-0 items-center justify-between gap-3">
          <div className="min-w-0">
            <div className="text-[14px] font-medium text-[var(--text)]">
              {i18nT('pages.mcpManagement.sharing_label')}
            </div>
            <p id="mcp-sharing-desc" className="mt-0.5 text-[12.5px] text-[var(--muted)]">
              {!supported
                ? i18nT('pages.mcpManagement.unsupported_platform')
                : !status?.enabled && stubCount === 0
                  ? i18nT('pages.mcpManagement.sharing_needs_a_stub')
                  : i18nT('pages.mcpManagement.sharing_description')}
            </p>
            {/* The switch acts at once, so the assessment's verdict sits beside it
                before the press: naming marked servers it argues against here is
                what the old confirm dialog was for. */}
            {!status?.enabled && contraryMarked.length > 0 && (
              <p className="mt-1 flex items-start gap-1.5 text-[12.5px] text-[var(--danger)]">
                <AlertTriangle size={13} className="mt-0.5 shrink-0" aria-hidden="true" />
                <span className="break-words [overflow-wrap:anywhere]">
                  {i18nT('pages.mcpManagement.sharing_contrary', { names: contraryMarked.join(', ') })}
                </span>
              </p>
            )}
          </div>
          <Switch
            on={!!status?.enabled}
            disabled={busy || statusQ.isLoading || (!status?.enabled && !canEnableSharing)}
            label={i18nT('pages.mcpManagement.sharing_label')}
            describedBy="mcp-sharing-desc"
            onClick={() => {
              setError(null)
              setSharing.mutate(!status?.enabled)
            }}
          />
        </div>
      </section>

      {/* Page-wide: install npm servers once so chats start without a download. */}
      <section className="rounded-xl border border-[var(--border)] bg-[var(--card)] px-4 py-3">
        <div className="flex min-w-0 items-center justify-between gap-3">
          <div className="min-w-0">
            <div className="text-[14px] font-medium text-[var(--text)]">
              {i18nT('pages.mcpManagement.resolve_label')}
            </div>
            <p id="mcp-resolve-desc" className="mt-0.5 text-[12.5px] text-[var(--muted)]">
              {stubCount === 0
                ? i18nT('pages.mcpManagement.resolve_needs_a_stub')
                : i18nT('pages.mcpManagement.resolve_description')}
            </p>
            {resolveNotice && (
              <p role="status" className="mt-1 text-[12.5px] text-[var(--text)]">
                {resolveNotice}
              </p>
            )}
            <ErrorNotice variant="inline" className="mt-2" message={resolveError} askAgent />
          </div>
          <button
            type="button"
            aria-describedby="mcp-resolve-desc"
            disabled={resolveRefresh.isPending}
            onClick={() => {
              setError(null)
              setResolveNotice(null)
              setResolveError(null)
              resolveRefresh.mutate()
            }}
            className="shrink-0 rounded-lg border border-[var(--border)] px-3 py-1.5 text-[13px] text-[var(--text)] transition-colors hover:bg-[var(--hover)] disabled:cursor-not-allowed disabled:opacity-60"
          >
            {resolveRefresh.isPending
              ? i18nT('pages.mcpManagement.resolve_updating')
              : i18nT('pages.mcpManagement.resolve_update_now')}
          </button>
        </div>
      </section>
      </TabsContent>
    </Tabs>
  )
}

export default McpManagement
