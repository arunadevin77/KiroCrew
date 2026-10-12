import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { CheckCircle2, Download, Loader2 } from 'lucide-react'

import { api, type KasBundleStatus } from '../../api/client'
import ErrorNotice from '../../components/ErrorNotice'
import { SettingsToggle } from '../../components/settings'
import { SchemaRestartBadge } from '../../components/settingRef/RestartRequiredBadge'
import { Btn } from '../../components/ui'
import { fmtBytes, fmtPercent } from '../../i18n/format'
import { i18nT } from '../../i18n/t'

/** The config field this card's switch writes. */
export const KAS_BUNDLE_CONFIG_KEY = 'agent.kas_bundle'

const STATUS_QUERY_KEY = ['kas-bundle'] as const

/** How often the card re-reads status while an install is running. */
const INSTALL_POLL_MS = 1000

/**
 * Settings > Agent Harness, inside the KAS row: run Kiro Crew's own KAS engine
 * instead of reaching it through kiro-cli.
 *
 * The switch is `agent.kas_bundle` (an ordinary config PATCH). It can only be
 * turned ON once the pinned engine is installed, because an "on" with nothing
 * installed refuses every KAS session -- the card offers the install instead.
 * Turning it OFF is always allowed, so a broken install never traps anyone on
 * this route.
 *
 * The install runs in the gateway; this card starts it and polls status once a
 * second while it runs. Status is the single authority: nothing here mirrors
 * the server's answer in local state.
 */
export function KasBundleCard() {
  const qc = useQueryClient()
  const statusQ = useQuery<KasBundleStatus>({
    queryKey: STATUS_QUERY_KEY,
    queryFn: () => api.kasBundleStatus(),
    retry: false,
    staleTime: 0,
    refetchInterval: query => (query.state.data?.installing ? INSTALL_POLL_MS : false),
  })
  const status = statusQ.data

  const installMut = useMutation({
    mutationFn: () => api.kasBundleInstall(),
    onSuccess: data => qc.setQueryData(STATUS_QUERY_KEY, data),
  })

  const toggleMut = useMutation({
    mutationFn: (value: boolean) => api.patchConfig(KAS_BUNDLE_CONFIG_KEY, value),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: STATUS_QUERY_KEY })
      void qc.invalidateQueries({ queryKey: ['kirocrewConfig'] })
    },
  })

  // The poll stops on its own once the server reports the install finished.
  const installing = status?.installing === true || installMut.isPending

  if (!status) {
    return statusQ.isError ? (
      <ErrorNotice
        message={i18nT('pages.developer.kasBundleCard.status_unavailable')}
        variant="inline"
        askAgent
      />
    ) : null
  }

  const installed = status.installed
  const canTurnOn = installed !== null && !installing
  const progress = status.progress
  const progressText =
    progress.total > 0
      ? i18nT('pages.developer.kasBundleCard.installing_progress', {
          version: status.runs_version,
          percent: fmtPercent(progress.done / progress.total),
          done: fmtBytes(progress.done),
          total: fmtBytes(progress.total),
        })
      : i18nT('pages.developer.kasBundleCard.installing', { version: status.runs_version })

  return (
    <div className="space-y-2 pt-2 border-t border-border" data-testid="kas-bundle-card">
      <div className="flex flex-wrap items-center gap-2">
        <div className="min-w-0 flex-1">
          <SettingsToggle
            configKey={KAS_BUNDLE_CONFIG_KEY}
            label={i18nT('pages.developer.kasBundleCard.toggle_label')}
            description={i18nT('pages.developer.kasBundleCard.toggle_description')}
            checked={status.enabled}
            onChange={value => toggleMut.mutate(value)}
            disabled={toggleMut.isPending || (!status.enabled && !canTurnOn)}
          />
        </div>
        <SchemaRestartBadge configKey={KAS_BUNDLE_CONFIG_KEY} />
      </div>

      <div className="flex flex-wrap items-center gap-2 text-sm" data-testid="kas-bundle-state">
        {installing ? (
          <>
            <Loader2 className="lucide-inline animate-spin text-muted" aria-hidden="true" />
            <span role="status" aria-live="polite">{progressText}</span>
          </>
        ) : installed ? (
          <>
            <CheckCircle2 className="lucide-inline text-ok" aria-hidden="true" />
            <span>
              {i18nT('pages.developer.kasBundleCard.installed', {
                version: installed.version,
                node: installed.node_version,
              })}
            </span>
          </>
        ) : (
          <>
            <span className="text-muted">
              {i18nT('pages.developer.kasBundleCard.not_installed', { version: status.runs_version })}
            </span>
            <Btn type="button" primary onClick={() => installMut.mutate()} data-testid="kas-bundle-install">
              <Download className="lucide-inline" aria-hidden="true" />
              {i18nT('pages.developer.kasBundleCard.install')}
            </Btn>
          </>
        )}
      </div>

      {/* No hand-off draft to lose: the install and the switch are both server state. */}
      <ErrorNotice
        message={
          status.error
            ? status.error
            : installMut.isError
              ? i18nT('pages.developer.kasBundleCard.install_failed')
              : toggleMut.isError
                ? i18nT('pages.developer.kasBundleCard.save_failed')
                : undefined
        }
        variant="inline"
        askAgent
      />
    </div>
  )
}
