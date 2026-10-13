import { useId, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Check, ChevronRight, ExternalLink, Minus, Plus, Trash2, X } from 'lucide-react'
import { AnimatePresence, motion } from 'framer-motion'
import type { ReactNode } from 'react'

import { api, type SessionEnvironmentsCheck } from '../../api/client'
import { fmtDateTime, fmtNumber } from '../../i18n/format'
import { safeHttpUrl } from '../../lib/safeUrl'
import Clickable from '../../components/Clickable'
import ErrorNotice from '../../components/ErrorNotice'
import { BoxdIcon, CoderIcon, FargateIcon, FlyIcon } from '../../components/BrandIcon'
import { SettingsField, SettingsInput, SettingsSelect, SettingsStepper, SettingsToggle } from '../../components/settings'
import { Btn, Input, PanelSectionHeader } from '../../components/ui'

interface EnvironmentProfile {
  id: string
  name: string
  template: string
  workdir: string
  /** Ready environments kept waiting for this profile. Older gateways omit it. */
  hot_pool?: number
}
/** Mirrors HOT_POOL_MAX in config/session_environments.py. */
const HOT_POOL_MAX = 5
/** How often the card re-reads the ready count while a pool is configured. */
const POOL_POLL_MS = 15_000
type ProfileTextKey = 'id' | 'name' | 'template' | 'workdir'
interface FargateProfile {
  id: string
  name: string
  aws_profile: string
  region: string
  cluster: string
  task_definition: string
  subnets: string[]
  security_groups: string[]
  assign_public_ip: boolean
  workdir: string
}
interface BoxdProfile {
  id: string
  name: string
  /** Snapshot (name or id) new machines start from; "" starts a fresh one. */
  snapshot: string
  /** 0 is the org's default size. A snapshot keeps its own size. */
  vcpu: number
  workdir: string
}
/** Mirrors BOXD_VCPU_CHOICES in config/session_environments.py. */
const BOXD_VCPU_CHOICES = [0, 1, 2, 4, 8]
interface SpritesProfile {
  id: string
  name: string
  /** Name of the gateway vault secret holding the Sprites API token. */
  token_secret: string
  workdir: string
}
interface EnvironmentSettings {
  coder_url: string
  profiles: EnvironmentProfile[]
  fargate_profiles?: FargateProfile[]
  /** Fly.io Sprites profiles. Older gateways omit it. */
  sprites_profiles?: SpritesProfile[]
  /** boxd machine profiles. Older gateways omit it. */
  boxd_profiles?: BoxdProfile[]
  /** Profile id new chats start on; "" is this machine. Older gateways omit it. */
  default_profile?: string
  /** Days an archived session's workspace is kept. Older gateways omit it. */
  archived_retention_days?: number
  /** Stop every remote environment when the gateway shuts down. Older gateways omit it. */
  stop_on_shutdown?: boolean
}
/** A removed profile cannot stay the default (the gateway refuses that save),
 * so removing the default falls back to this machine. */
function withoutDefault(next: EnvironmentSettings, removedId: string): EnvironmentSettings {
  return next.default_profile === removedId ? { ...next, default_profile: '' } : next
}
/** Mirrors ARCHIVED_RETENTION_DAYS_MAX in config/session_environments.py. */
const ARCHIVED_RETENTION_DAYS_MAX = 7
interface ConfigResponse {
  session_environments?: EnvironmentSettings
}

type ProviderId = 'coder' | 'fargate' | 'sprites' | 'boxd'
const SETUP_DOCS: Record<ProviderId, string> = {
  coder: 'https://coder.com/docs/admin/setup',
  fargate: 'https://docs.aws.amazon.com/AmazonECS/latest/developerguide/getting-started-fargate.html',
  sprites: 'https://docs.fly.io/sprites',
  boxd: 'https://docs.boxd.sh/quickstart',
}

/** One saved profile as a disclosure. The card stays one element: its header
 * keeps the profile's name in place and only the field body expands below it.
 * The body unmounts when collapsed, which loses nothing because every value
 * lives in the parent draft. */
function ProfileDisclosure({ title, subtitle, badge, open, onToggle, children }: {
  title: string
  subtitle?: string
  /** Live status shown on the header whether or not the body is open. */
  badge?: string
  open: boolean
  onToggle: () => void
  children: ReactNode
}) {
  const bodyId = useId()
  return (
    <div className="min-w-0 rounded-lg border border-border">
      <Clickable onClick={onToggle} aria-expanded={open} aria-controls={bodyId}
        className="group flex min-w-0 items-center gap-2.5 rounded-lg px-3 py-2.5 cursor-pointer hover:bg-bg-hover/40 transition-colors">
        <motion.span className="flex shrink-0 items-center text-muted group-hover:text-text"
          animate={{ rotate: open ? 90 : 0 }} transition={{ type: 'spring', damping: 22, stiffness: 300 }}>
          <ChevronRight className="lucide-inline" aria-hidden="true" />
        </motion.span>
        <span className="truncate text-[13px] font-semibold text-text">{title}</span>
        {subtitle && <span className="truncate text-[12px] text-muted">{subtitle}</span>}
        {badge && <span data-testid="profile-pool-status" className="ml-auto shrink-0 text-[12px] text-muted tabular-nums">{badge}</span>}
      </Clickable>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div key="body" id={bodyId}
            initial={{ height: 0, opacity: 0 }} animate={{ height: 'auto', opacity: 1 }} exit={{ height: 0, opacity: 0 }}
            transition={{ type: 'spring', damping: 26, stiffness: 280, mass: 0.7 }} style={{ overflow: 'hidden' }}>
            <fieldset aria-label={title} className="min-w-0 space-y-3 px-3 pb-3">{children}</fieldset>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

/** One check line: a status glyph and its text, announced as a list item. */
function CheckLine({ status, children }: { status: 'ok' | 'fail' | 'unknown'; children: ReactNode }) {
  const Icon = status === 'ok' ? Check : status === 'fail' ? X : Minus
  const tone = status === 'ok' ? 'text-ok' : status === 'fail' ? 'text-danger' : 'text-muted'
  return (
    <li className="flex min-w-0 items-start gap-2 text-[13px] text-text">
      <Icon size={14} className={`mt-0.5 shrink-0 ${tone}`} aria-hidden="true" />
      <span className="min-w-0 break-words">{children}</span>
    </li>
  )
}

const GIT_HOST_NAMES: Record<string, string> = {
  'github.com': 'GitHub',
  'gitlab.com': 'GitLab',
  'bitbucket.org': 'Bitbucket',
  'dev.azure.com': 'Azure DevOps',
}

/** What the readiness check found, in the order a person fixes things.
 *
 * Follows the provider tab, like the Manage step: the providers are mutually
 * exclusive choices, so a Sprites setup never reports Coder rows even when a
 * Coder URL is still saved from earlier. Git rows apply to every provider. */
function CheckResults({ result, provider, profiles, fargateProfiles, spritesProfiles, boxdProfiles, coderSaved }: {
  result: SessionEnvironmentsCheck
  provider: string
  profiles: EnvironmentProfile[]
  fargateProfiles: FargateProfile[]
  spritesProfiles: SpritesProfile[]
  boxdProfiles: BoxdProfile[]
  /** A Fargate-only setup has no Coder line to report. */
  coderSaved: boolean
}) {
  const { t } = useTranslation()
  const names = new Map(profiles.map(p => [p.id, p]))
  const fargateNames = new Map(fargateProfiles.map(p => [p.id, p]))
  const spritesNames = new Map(spritesProfiles.map(p => [p.id, p]))
  const boxdNames = new Map(boxdProfiles.map(p => [p.id, p]))
  return (
    <ul aria-label={t('sessionEnvironments.checkResults')} className="space-y-1.5">
      {provider === 'coder' && coderSaved && (
        <CheckLine status={result.coder.ok ? 'ok' : 'fail'}>
          {result.coder.ok
            ? t('sessionEnvironments.checkCoderOk', { owner: result.coder.owner ?? '' })
            : t('sessionEnvironments.checkCoderFailed', { error: result.coder.error ?? '' })}
        </CheckLine>
      )}
      {provider === 'coder' && result.profiles.map(row => {
        const profile = names.get(row.id)
        const name = profile?.name || row.id
        return (
          <CheckLine key={row.id} status={row.ok ? 'ok' : 'fail'}>
            {row.ok
              ? t('sessionEnvironments.checkTemplateOk', { name, template: profile?.template ?? '' })
              : t('sessionEnvironments.checkTemplateFailed', { name, error: row.error ?? '' })}
          </CheckLine>
        )
      })}
      {provider === 'fargate' && (result.fargate ?? []).map(row => {
        const profile = fargateNames.get(row.id)
        const name = profile?.name || row.id
        return (
          <CheckLine key={`fargate:${row.id}`} status={row.ok ? 'ok' : 'fail'}>
            {row.ok
              ? t('sessionEnvironments.checkTaskDefinitionOk', { name, taskDefinition: profile?.task_definition ?? '' })
              : t('sessionEnvironments.checkTemplateFailed', { name, error: row.error ?? '' })}
          </CheckLine>
        )
      })}
      {provider === 'sprites' && (result.sprites ?? []).map(row => {
        const name = spritesNames.get(row.id)?.name || row.id
        return (
          <CheckLine key={`sprites:${row.id}`} status={row.ok ? 'ok' : 'fail'}>
            {row.ok
              ? t('sessionEnvironments.checkSpritesTokenOk', { name })
              : t('sessionEnvironments.checkTemplateFailed', { name, error: row.error ?? '' })}
          </CheckLine>
        )
      })}
      {provider === 'boxd' && (result.boxd ?? []).map(row => {
        const name = boxdNames.get(row.id)?.name || row.id
        return (
          <CheckLine key={`boxd:${row.id}`} status={row.ok ? 'ok' : 'fail'}>
            {row.ok
              ? t('sessionEnvironments.checkBoxdOk', { name })
              : t('sessionEnvironments.checkTemplateFailed', { name, error: row.error ?? '' })}
          </CheckLine>
        )
      })}
      {Object.entries(result.git).map(([host, status]) => {
        const name = GIT_HOST_NAMES[host] ?? host
        return (
          <CheckLine key={host} status={status === 'signed_in' ? 'ok' : status === 'not_signed_in' ? 'fail' : 'unknown'}>
            {status === 'signed_in'
              ? t('sessionEnvironments.checkGitSignedIn', { name })
              : status === 'not_signed_in'
                ? t('sessionEnvironments.checkGitSignedOut', { name })
                : t('sessionEnvironments.checkGitUnchecked', { name })}
          </CheckLine>
        )
      })}
    </ul>
  )
}

/** Store a Sprites API token straight into the gateway vault under the
 * profile's secret name. Masked while typed, cleared once stored, and never
 * read back: the vault is write-only to the dashboard, so the field can only
 * say whether a token is saved, never show one. Saved on its own button, not
 * with the profile draft, so the token never enters config or the draft. */
function SpritesTokenField({ secretName, profileSaved }: { secretName: string; profileSaved: boolean }) {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const inputId = useId()
  const [value, setValue] = useState('')
  const secrets = useQuery({ queryKey: ['secrets'], queryFn: () => api.secretsList(), retry: false })
  const save = useMutation({
    mutationFn: () => api.secretsSave(secretName, value),
    onSuccess: () => {
      setValue('')
      void qc.invalidateQueries({ queryKey: ['secrets'] })
    },
  })
  // The vault entry outlives the profile (another profile may name the same
  // secret), so it is removed here explicitly, never by removing a profile.
  const remove = useMutation({
    mutationFn: () => api.secretsDelete(secretName),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['secrets'] }),
  })
  const stored = !!secretName && (secrets.data?.names ?? []).includes(secretName)
  return (
    <div className="space-y-1.5">
      <label htmlFor={inputId} className="block text-[13px] font-medium text-text">{t('sessionEnvironments.apiToken')}</label>
      <div className="flex min-w-0 gap-2">
        <Input id={inputId} type="password" autoComplete="new-password" spellCheck={false}
          className="min-w-0 flex-1 font-mono" value={value} onChange={e => setValue(e.target.value)}
          placeholder={stored ? t('sessionEnvironments.apiTokenSavedPlaceholder') : t('sessionEnvironments.apiTokenPlaceholder')} />
        <Btn type="button" disabled={!value || !secretName || save.isPending} onClick={() => save.mutate()}>
          {t('sessionEnvironments.apiTokenSave')}
        </Btn>
        {stored && (
          <Btn type="button" danger disabled={remove.isPending} onClick={() => remove.mutate()}>
            {t('sessionEnvironments.apiTokenRemove')}
          </Btn>
        )}
      </div>
      <p role="status" className="text-[13px] text-muted">
        {stored ? t('sessionEnvironments.apiTokenStored', { name: secretName }) : t('sessionEnvironments.apiTokenHint', { name: secretName })}
      </p>
      {/* Saving the token does not save the profile: say so until it is saved,
        * or the composer shows no Sprites choice and nothing explains why. */}
      {!profileSaved && (
        <div data-testid="sprites-profile-unsaved" className="flex flex-wrap items-center gap-2">
          <p className="min-w-0 flex-1 text-[13px] text-warn">{t('sessionEnvironments.spritesProfileUnsaved')}</p>
          {/* The same submit as the section's Save: it saves the whole draft. */}
          <Btn type="submit" primary>{t('sessionEnvironments.save')}</Btn>
        </div>
      )}
      {/* No hand-off: navigating would discard the typed token. */}
      <ErrorNotice message={save.isError ? t('sessionEnvironments.apiTokenError')
        : remove.isError ? t('sessionEnvironments.apiTokenRemoveError') : null} />
    </div>
  )
}

/** The workspaces Kiro Crew owns, from the gateway's registry (no Coder call). */
function WorkspaceList({ provider }: { provider: ProviderId }) {
  const { t } = useTranslation()
  const query = useQuery({
    queryKey: ['sessionEnvironmentsWorkspaces'],
    queryFn: () => api.sessionEnvironmentsWorkspaces(),
    refetchInterval: POOL_POLL_MS,
    retry: false,
  })
  if (query.isPending) return null
  if (query.isError) return <ErrorNotice askAgent message={t('sessionEnvironments.workspacesError')} />
  // A row without a provider comes from an older gateway, which only had Coder.
  const rows = query.data.workspaces.filter(row => (row.provider ?? 'coder') === provider)
  if (!rows.length) return <p className="text-[13px] text-muted">{t('sessionEnvironments.workspacesNone')}</p>
  return (
    <ul aria-label={t('sessionEnvironments.workspaces')} className="divide-y divide-border rounded-lg border border-border">
      {rows.map(row => (
        <li key={row.workspace} className="flex min-w-0 flex-wrap items-baseline gap-x-3 gap-y-0.5 px-3 py-2 text-[13px]">
          <span className="truncate font-mono text-[12px] text-text">{row.workspace}</span>
          <span className="text-muted">
            {row.kind === 'pool'
              ? row.state === 'ready' ? t('sessionEnvironments.workspaceReady') : t('sessionEnvironments.workspaceWarming')
              : row.provider === 'fargate'
                ? t('sessionEnvironments.workspaceFargateTask')
                : row.provider === 'sprites'
                  ? t('sessionEnvironments.workspaceSprite')
                  : row.provider === 'boxd'
                    ? t('sessionEnvironments.workspaceBoxd')
                    : t('sessionEnvironments.workspaceSession')}
          </span>
          {row.kind === 'session' && (row.state === 'deleting' || row.deletion_due_at) && (
            <span className="ml-auto text-[12px] text-muted">
              {row.state === 'deleting'
                ? t('sessionEnvironments.workspaceDeleting')
                : t('sessionEnvironments.workspaceDeletes', { when: fmtDateTime(row.deletion_due_at) })}
            </span>
          )}
        </li>
      ))}
    </ul>
  )
}

/** Configuration only: neither the flag nor a save enables remote execution.
 * The preview switch is indexed in FeaturePreviewsSection. The URL setting is
 * searchable only after opting in; profile rows are per-record CRUD fields.
 */
export function SessionEnvironmentsCard({ enabled }: { enabled: boolean }) {
  const { t } = useTranslation()
  const qc = useQueryClient()
  const controlId = useId()
  const query = useQuery<ConfigResponse>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
    enabled,
  })
  // Null follows fresh server data. Once edited, background refetches never
  // replace the draft. Saving freezes controls until the server responds.
  const [draft, setDraft] = useState<EnvironmentSettings | null>(null)
  const [saved, setSaved] = useState(false)
  // Null until the person picks a tab; until then the tab follows what is saved
  // (see `provider` below), so a Sprites-only setup does not open on Coder.
  const [providerChoice, setProvider] = useState<ProviderId | null>(null)
  // Saved profiles start collapsed; a newly added one opens so it can be
  // filled in. Tracked by row index, so add/remove keep it aligned.
  const [openRows, setOpenRows] = useState<Record<ProviderId, boolean[]>>({ coder: [], fargate: [], sprites: [], boxd: [] })
  const toggleRow = (kind: ProviderId, index: number) => setOpenRows(rows => {
    const next = [...rows[kind]]
    next[index] = !next[index]
    return { ...rows, [kind]: next }
  })
  const addRow = (kind: ProviderId, index: number) => setOpenRows(rows => {
    const next = [...rows[kind]]
    next[index] = true
    return { ...rows, [kind]: next }
  })
  const removeRow = (kind: ProviderId, index: number) => setOpenRows(rows => ({
    ...rows, [kind]: rows[kind].filter((_, i) => i !== index),
  }))
  /** Delete each named vault token no remaining Sprites profile still names.
   * A shared secret name stays while any profile uses it. Best effort: a
   * failure leaves the token, which the field then still reports as saved. */
  const deleteUnusedTokens = async (names: string[], remaining: SpritesProfile[]) => {
    const inUse = new Set(remaining.map(profile => profile.token_secret))
    const unused = [...new Set(names)].filter(name => name && !inUse.has(name))
    if (!unused.length) return
    await Promise.allSettled(unused.map(name => api.secretsDelete(name)))
    await qc.invalidateQueries({ queryKey: ['secrets'] })
  }
  const mutation = useMutation({
    mutationFn: (value: EnvironmentSettings) => api.patchConfig('session_environments', value) as Promise<ConfigResponse>,
    onMutate: () => qc.cancelQueries({ queryKey: ['kirocrewConfig'] }),
    onSuccess: (response, sent) => {
      // Tokens of Sprites profiles this save removed go with them, now that the
      // removal is saved (a discarded draft keeps both).
      const before = query.data?.session_environments?.sprites_profiles ?? []
      const after = response.session_environments?.sprites_profiles ?? sent.sprites_profiles ?? []
      // A kept profile pointed at another secret name orphans its old one too.
      const nowNames = new Map(after.map(profile => [profile.id, profile.token_secret]))
      void deleteUnusedTokens(
        before.filter(profile => nowNames.get(profile.id) !== profile.token_secret).map(profile => profile.token_secret),
        after,
      )
      qc.setQueryData(['kirocrewConfig'], response)
      setDraft(null)
      setSaved(true)
    },
  })
  const config = draft ?? query.data?.session_environments
  // Live hot-pool counts for SAVED profiles. Registry-only on the gateway (no
  // Coder command), polled only while some saved profile keeps a pool.
  const pooled = (query.data?.session_environments?.profiles ?? []).some(profile => (profile.hot_pool ?? 0) > 0)
  const poolQuery = useQuery({
    queryKey: ['sessionEnvironmentsPool'],
    queryFn: () => api.sessionEnvironmentsPool(),
    enabled: enabled && pooled,
    refetchInterval: POOL_POLL_MS,
    retry: false,
  })
  const poolBadge = (id: string): string => {
    const pool = poolQuery.data?.profiles[id]
    if (!pool || pool.target <= 0) return ''
    return t('sessionEnvironments.pool.summary', { ready: pool.ready, target: pool.target, warming: pool.warming })
  }
  // The check reads the SAVED configuration on the gateway, so it is offered
  // only with no unsaved draft, and its answer is dropped when one appears.
  const savedCoderUrl = safeHttpUrl(query.data?.session_environments?.coder_url ?? '')
  const savedProfiles = query.data?.session_environments?.profiles ?? []
  const savedFargateProfiles = query.data?.session_environments?.fargate_profiles ?? []
  const savedSpritesProfiles = query.data?.session_environments?.sprites_profiles ?? []
  const savedBoxdProfiles = query.data?.session_environments?.boxd_profiles ?? []
  // A setup without Coder is checkable and has workspaces without a Coder URL.
  const anythingSaved = !!savedCoderUrl || savedFargateProfiles.length > 0
    || savedSpritesProfiles.length > 0 || savedBoxdProfiles.length > 0
  // The providers are mutually exclusive choices, so open on the one in use.
  // Coder wins a tie because it is the only one with gateway-level settings.
  const provider: ProviderId = providerChoice
    ?? (savedCoderUrl || savedProfiles.length > 0 ? 'coder'
      : savedFargateProfiles.length > 0 ? 'fargate'
        : savedSpritesProfiles.length > 0 ? 'sprites'
          : savedBoxdProfiles.length > 0 ? 'boxd'
            : 'coder')
  const checkMutation = useMutation({ mutationFn: () => api.sessionEnvironmentsCheck() })
  const change = (next: EnvironmentSettings) => {
    setDraft(next)
    setSaved(false)
    mutation.reset()
  }

  if (!enabled && !draft) return null
  if (query.isPending) return <p role="status" className="text-[13px] text-muted">{t('sessionEnvironments.loading')}</p>
  if (query.isError && !query.data) return (
    <ErrorNotice message={t('sessionEnvironments.loadError')} askAgent footer={
      <Btn onClick={() => void query.refetch()}>{t('sessionEnvironments.retry')}</Btn>
    } />
  )
  // Older gateways do not expose the section. Do not let an apparently empty
  // form pretend this backend can save it, or fabricate a successful connection.
  if (!config) return <ErrorNotice message={t('sessionEnvironments.unavailable')} askAgent />

  const profileFields: { key: ProfileTextKey; label: string }[] = [
    { key: 'id', label: t('sessionEnvironments.id') },
    { key: 'name', label: t('sessionEnvironments.name') },
    { key: 'template', label: t('sessionEnvironments.template') },
    { key: 'workdir', label: t('sessionEnvironments.workdir') },
  ]
  const updateProfile = (index: number, field: keyof EnvironmentProfile, value: string | number) => {
    change({ ...config, profiles: config.profiles.map((profile, i) => i === index ? { ...profile, [field]: value } : profile) })
  }
  const addProfile = () => {
    let n = 1
    while (config.profiles.some(profile => profile.id === `profile-${n}`)) n++
    addRow('coder', config.profiles.length)
    change({ ...config, profiles: [...config.profiles, { id: `profile-${n}`, name: '', template: '', workdir: '/home/coder', hot_pool: 0 }] })
  }

  const fargateProfiles = config.fargate_profiles ?? []
  const fargateFields: { key: 'id' | 'name' | 'aws_profile' | 'region' | 'cluster' | 'task_definition' | 'workdir'; label: string }[] = [
    { key: 'id', label: t('sessionEnvironments.id') },
    { key: 'name', label: t('sessionEnvironments.name') },
    { key: 'aws_profile', label: t('sessionEnvironments.awsProfile') },
    { key: 'region', label: t('sessionEnvironments.region') },
    { key: 'cluster', label: t('sessionEnvironments.cluster') },
    { key: 'task_definition', label: t('sessionEnvironments.taskDefinition') },
    { key: 'workdir', label: t('sessionEnvironments.workdir') },
  ]
  const fargateNetworkFields: { key: 'subnets' | 'security_groups'; label: string }[] = [
    { key: 'subnets', label: t('sessionEnvironments.subnets') },
    { key: 'security_groups', label: t('sessionEnvironments.securityGroups') },
  ]
  const spritesProfiles = config.sprites_profiles ?? []
  const spritesFields: { key: 'id' | 'name' | 'token_secret' | 'workdir'; label: string }[] = [
    { key: 'id', label: t('sessionEnvironments.id') },
    { key: 'name', label: t('sessionEnvironments.name') },
    { key: 'token_secret', label: t('sessionEnvironments.tokenSecret') },
    { key: 'workdir', label: t('sessionEnvironments.workdir') },
  ]
  const updateSprites = (index: number, updates: Partial<SpritesProfile>) => {
    change({ ...config, sprites_profiles: spritesProfiles.map((profile, i) => i === index ? { ...profile, ...updates } : profile) })
  }
  const boxdProfiles = config.boxd_profiles ?? []
  const boxdFields: { key: 'id' | 'name' | 'snapshot' | 'workdir'; label: string }[] = [
    { key: 'id', label: t('sessionEnvironments.id') },
    { key: 'name', label: t('sessionEnvironments.name') },
    { key: 'snapshot', label: t('sessionEnvironments.boxdSnapshot') },
    { key: 'workdir', label: t('sessionEnvironments.workdir') },
  ]
  const updateBoxd = (index: number, updates: Partial<BoxdProfile>) => {
    // A snapshot keeps its own size (the gateway refuses both), so naming one
    // returns the size to the default.
    const next = updates.snapshot ? { ...updates, vcpu: 0 } : updates
    change({ ...config, boxd_profiles: boxdProfiles.map((profile, i) => i === index ? { ...profile, ...next } : profile) })
  }
  const publicIpLabel = t('sessionEnvironments.publicIp')
  const updateFargate = (index: number, updates: Partial<FargateProfile>) => {
    change({ ...config, fargate_profiles: fargateProfiles.map((profile, i) => i === index ? { ...profile, ...updates } : profile) })
  }

  return (
    <form className="min-w-0 space-y-3 pt-3" onSubmit={event => {
      event.preventDefault()
      if (draft && !mutation.isPending) mutation.mutate(draft)
    }}>
      {/* Global: these apply across every provider, so they sit above the
        * provider tabs instead of inside one of them. Older gateways omit
        * either key, and then its control stays hidden. */}
      {(config.default_profile !== undefined || config.archived_retention_days !== undefined || config.stop_on_shutdown !== undefined) && (() => {
        const all = [...config.profiles, ...fargateProfiles, ...spritesProfiles, ...boxdProfiles].filter(p => p.id)
        const defaultOn = !!config.default_profile
        return (
          <section aria-label={t('sessionEnvironments.globalDefaults')} className="space-y-3">
            <PanelSectionHeader label={t('sessionEnvironments.globalDefaults')} />
            <fieldset disabled={mutation.isPending} className="min-w-0 space-y-3">
              {config.default_profile !== undefined && <>
                <SettingsToggle configKey="session_environments.default_profile"
                  label={t('sessionEnvironments.defaultEnabled')}
                  description={all.length ? t('sessionEnvironments.defaultEnabledHint') : t('sessionEnvironments.defaultEnabledNoProfiles')}
                  checked={defaultOn} disabled={!all.length && !defaultOn}
                  onChange={on => change({ ...config, default_profile: on ? all[0]?.id ?? '' : '' })} />
                {defaultOn && (
                  <SettingsSelect label={t('sessionEnvironments.defaultProfile')}
                    description={t('sessionEnvironments.defaultProfileHint')}
                    value={config.default_profile ?? ''}
                    options={all.map(p => p.id)}
                    optionLabels={all.map(p => p.name || p.id)}
                    onChange={value => change({ ...config, default_profile: value })} />
                )}
              </>}
              {config.archived_retention_days !== undefined && (
                <SettingsStepper configKey="session_environments.archived_retention_days"
                  label={t('sessionEnvironments.archivedRetention')}
                  description={t('sessionEnvironments.archivedRetentionHint')}
                  value={config.archived_retention_days}
                  onDecrement={() => change({ ...config, archived_retention_days: Math.max(0, (config.archived_retention_days ?? 0) - 1) })}
                  onIncrement={() => change({ ...config, archived_retention_days: Math.min(ARCHIVED_RETENTION_DAYS_MAX, (config.archived_retention_days ?? 0) + 1) })} />
              )}
              {config.stop_on_shutdown !== undefined && (
                <SettingsToggle configKey="session_environments.stop_on_shutdown"
                  label={t('sessionEnvironments.stopOnShutdown')}
                  description={t('sessionEnvironments.stopOnShutdownHint')}
                  checked={config.stop_on_shutdown}
                  onChange={on => change({ ...config, stop_on_shutdown: on })} />
              )}
            </fieldset>
          </section>
        )
      })()}
      <fieldset disabled={mutation.isPending} className="min-w-0 pt-3">
        <legend className="mb-2 text-[13px] font-semibold text-text">{t('sessionEnvironments.provider')}</legend>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-2">
          {([
            { id: 'coder', label: t('sessionEnvironments.coder'), icon: <CoderIcon size={14} /> },
            { id: 'fargate', label: t('sessionEnvironments.fargate'), icon: <FargateIcon size={20} /> },
            { id: 'sprites', label: t('sessionEnvironments.sprites'), icon: <FlyIcon size={18} /> },
            { id: 'boxd', label: t('sessionEnvironments.boxd'), icon: <BoxdIcon size={16} /> },
          ] as const).map(option => (
            <label key={option.id} className={`flex items-center gap-3 rounded-lg border p-3 cursor-pointer text-[13px] ${provider === option.id ? 'border-accent bg-accent-subtle text-text' : 'border-border bg-bg text-text'}`}>
              <input type="radio" name={`${controlId}-provider`} value={option.id} checked={provider === option.id}
                aria-label={option.label} onChange={() => setProvider(option.id)} className="accent-[var(--accent)]" />
              {option.icon}
              {option.label}
            </label>
          ))}
        </div>
      </fieldset>
      <section aria-label={t('sessionEnvironments.configureStep')} className="space-y-3 pt-3">
      <PanelSectionHeader label={t('sessionEnvironments.configureStep')} />
      <details className="rounded-lg border border-border p-3">
        <summary className="cursor-pointer text-[13px] font-semibold text-text">{t('sessionEnvironments.setup')}</summary>
        <ol className="list-decimal pl-5 space-y-2 pt-3 text-[13px] text-muted">
          {provider === 'coder' ? <>
            <li>{t('sessionEnvironments.coderSetup1')}</li>
            <li>{t('sessionEnvironments.coderSetup2')}</li>
            <li>{t('sessionEnvironments.coderSetup3')}</li>
          </> : provider === 'fargate' ? <>
            <li>{t('sessionEnvironments.fargateSetup1')}</li>
            <li>{t('sessionEnvironments.fargateSetup2')}</li>
            <li>{t('sessionEnvironments.fargateSetup3')}</li>
          </> : provider === 'sprites' ? <>
            <li>{t('sessionEnvironments.spritesSetup1')}</li>
            <li>{t('sessionEnvironments.spritesSetup2')}</li>
            <li>{t('sessionEnvironments.spritesSetup3')}</li>
          </> : <>
            <li>{t('sessionEnvironments.boxdSetup1')}</li>
            <li>{t('sessionEnvironments.boxdSetup2')}</li>
            <li>{t('sessionEnvironments.boxdSetup3')}</li>
          </>}
        </ol>
        <a className="inline-block pt-3 text-[13px] text-accent underline" target="_blank" rel="noopener noreferrer"
          href={SETUP_DOCS[provider]}>
          {t('sessionEnvironments.setupDocs')}
        </a>
      </details>
      <fieldset disabled={mutation.isPending} className="min-w-0 space-y-3">
        <div hidden={provider !== 'coder'}>
        <SettingsInput
          configKey="session_environments.coder_url"
          label={t('sessionEnvironments.url')}
          description={t('sessionEnvironments.urlHint')}
          value={config.coder_url}
          onChange={value => change({ ...config, coder_url: value })}
        />
        <SettingsField configKey="session_environments.profiles" label={t('sessionEnvironments.profiles')}
          description={t('sessionEnvironments.profilesCoderHint')}>
          <div className="min-w-0 space-y-3">
            {config.profiles.map((profile, index) => (
              // Index remains stable while typing the editable id. Removing a
              // row is explicit and every input is controlled from this draft.
              <ProfileDisclosure key={index} title={profile.name || profile.id} subtitle={profile.template} badge={poolBadge(profile.id)}
                open={!!openRows.coder[index]} onToggle={() => toggleRow('coder', index)}>
                {profileFields.map(field => (
                  <SettingsInput key={field.key} label={field.label} value={profile[field.key]} onChange={value => updateProfile(index, field.key, value)} />
                ))}
                {profile.hot_pool !== undefined && (
                  <SettingsStepper label={t('sessionEnvironments.hotPool')} description={t('sessionEnvironments.hotPoolHint')}
                    value={profile.hot_pool}
                    onDecrement={() => updateProfile(index, 'hot_pool', Math.max(0, (profile.hot_pool ?? 0) - 1))}
                    onIncrement={() => updateProfile(index, 'hot_pool', Math.min(HOT_POOL_MAX, (profile.hot_pool ?? 0) + 1))} />
                )}
                <Btn type="button" onClick={() => {
                  removeRow('coder', index)
                  change(withoutDefault({ ...config, profiles: config.profiles.filter((_, i) => i !== index) }, profile.id))
                }}>
                  <Trash2 size={14} aria-hidden="true" />{t('sessionEnvironments.remove')}
                </Btn>
              </ProfileDisclosure>
            ))}
            <Btn type="button" disabled={config.profiles.length >= 20} onClick={addProfile}>
              <Plus size={14} aria-hidden="true" />{t('sessionEnvironments.add')}
            </Btn>
          </div>
        </SettingsField>
        </div>
        <div hidden={provider !== 'fargate'} className="space-y-3">
          {/* Deploy my crew hosts a whole headless crew; a session environment
            * keeps state on this gateway. Say so where the two look alike. */}
          <p className="text-[13px] text-muted">{t('sessionEnvironments.fargateVsDeploy')}</p>
          {config.fargate_profiles === undefined ? <ErrorNotice message={t('sessionEnvironments.unavailable')} askAgent /> : (
            <SettingsField configKey="session_environments.fargate_profiles" label={t('sessionEnvironments.profiles')}
              description={t('sessionEnvironments.profilesFargateHint')}>
              <div className="min-w-0 space-y-3">
                {fargateProfiles.map((profile, index) => (
                  <ProfileDisclosure key={index} title={profile.name || profile.id} subtitle={profile.task_definition}
                    open={!!openRows.fargate[index]} onToggle={() => toggleRow('fargate', index)}>
                    {fargateFields.map(field => <SettingsInput key={field.key} label={field.label} value={profile[field.key]} onChange={value => updateFargate(index, { [field.key]: value })} />)}
                    {fargateNetworkFields.map(field => <SettingsInput key={field.key} label={field.label} value={profile[field.key].join(', ')} onChange={value => updateFargate(index, { [field.key]: value.split(',').map(item => item.trim()) })} />)}
                    <SettingsToggle label={publicIpLabel} description={t('sessionEnvironments.publicIpHint')} checked={profile.assign_public_ip} onChange={value => updateFargate(index, { assign_public_ip: value })} />
                    <Btn type="button" onClick={() => {
                      removeRow('fargate', index)
                      change(withoutDefault({ ...config, fargate_profiles: fargateProfiles.filter((_, i) => i !== index) }, profile.id))
                    }}>
                      <Trash2 size={14} aria-hidden="true" />{t('sessionEnvironments.remove')}
                    </Btn>
                  </ProfileDisclosure>
                ))}
                <Btn type="button" disabled={fargateProfiles.length >= 20} onClick={() => {
                  let n = 1
                  while (fargateProfiles.some(profile => profile.id === `fargate-${n}`)) n++
                  addRow('fargate', fargateProfiles.length)
                  change({ ...config, fargate_profiles: [...fargateProfiles, {
                    id: `fargate-${n}`, name: '', aws_profile: '', region: '', cluster: '', task_definition: '',
                    subnets: [], security_groups: [], assign_public_ip: false, workdir: '/workspace',
                  }] })
                }}><Plus size={14} aria-hidden="true" />{t('sessionEnvironments.add')}</Btn>
              </div>
            </SettingsField>
          )}
        </div>
        <div hidden={provider !== 'sprites'} className="space-y-3">
          {/* Mounted only on its own tab, so an older gateway shows one notice. */}
          {config.sprites_profiles === undefined ? (provider === 'sprites' && <ErrorNotice message={t('sessionEnvironments.unavailable')} askAgent />) : (
            <SettingsField configKey="session_environments.sprites_profiles" label={t('sessionEnvironments.profiles')}
              description={t('sessionEnvironments.profilesSpritesHint')}>
              <div className="min-w-0 space-y-3">
                {spritesProfiles.map((profile, index) => (
                  <ProfileDisclosure key={index} title={profile.name || profile.id} subtitle={profile.token_secret}
                    open={!!openRows.sprites[index]} onToggle={() => toggleRow('sprites', index)}>
                    {spritesFields.map(field => <SettingsInput key={field.key} label={field.label} value={profile[field.key]} onChange={value => updateSprites(index, { [field.key]: value })} />)}
                    <SpritesTokenField secretName={profile.token_secret}
                      profileSaved={savedSpritesProfiles.some(saved => saved.id === profile.id)} />
                    <Btn type="button" onClick={() => {
                      removeRow('sprites', index)
                      const remaining = spritesProfiles.filter((_, i) => i !== index)
                      // A never-saved profile has no config to wait on, so its
                      // token goes now; a saved one's goes when the save lands.
                      if (!savedSpritesProfiles.some(saved => saved.id === profile.id)) {
                        void deleteUnusedTokens([profile.token_secret], [...remaining, ...savedSpritesProfiles])
                      }
                      change(withoutDefault({ ...config, sprites_profiles: remaining }, profile.id))
                    }}>
                      <Trash2 size={14} aria-hidden="true" />{t('sessionEnvironments.remove')}
                    </Btn>
                  </ProfileDisclosure>
                ))}
                <Btn type="button" disabled={spritesProfiles.length >= 20} onClick={() => {
                  let n = 1
                  while (spritesProfiles.some(profile => profile.id === `sprites-${n}`)) n++
                  addRow('sprites', spritesProfiles.length)
                  change({ ...config, sprites_profiles: [...spritesProfiles, {
                    // A name is required to save; start from one so a new
                    // profile saves as-is and can be renamed.
                    id: `sprites-${n}`,
                    name: n === 1 ? t('sessionEnvironments.sprites') : `${t('sessionEnvironments.sprites')} ${fmtNumber(n)}`,
                    token_secret: 'SPRITES_TOKEN', workdir: '/home/sprite/workspace',
                  }] })
                }}><Plus size={14} aria-hidden="true" />{t('sessionEnvironments.add')}</Btn>
              </div>
            </SettingsField>
          )}
        </div>
        <div hidden={provider !== 'boxd'} className="space-y-3">
          {/* Mounted only on its own tab, so an older gateway shows one notice. */}
          {config.boxd_profiles === undefined ? (provider === 'boxd' && <ErrorNotice message={t('sessionEnvironments.unavailable')} askAgent />) : (
            <SettingsField configKey="session_environments.boxd_profiles" label={t('sessionEnvironments.profiles')}
              description={t('sessionEnvironments.profilesBoxdHint')}>
              <div className="min-w-0 space-y-3">
                {boxdProfiles.map((profile, index) => (
                  <ProfileDisclosure key={index} title={profile.name || profile.id} subtitle={profile.snapshot}
                    open={!!openRows.boxd[index]} onToggle={() => toggleRow('boxd', index)}>
                    {boxdFields.map(field => <SettingsInput key={field.key} label={field.label} value={profile[field.key]} onChange={value => updateBoxd(index, { [field.key]: value })} />)}
                    {/* A snapshot keeps its own size, so the size applies only without one. */}
                    <SettingsSelect label={t('sessionEnvironments.boxdSize')} description={t('sessionEnvironments.boxdSizeHint')}
                      value={String(profile.snapshot ? 0 : profile.vcpu)} disabled={!!profile.snapshot}
                      options={BOXD_VCPU_CHOICES.map(String)}
                      optionLabels={BOXD_VCPU_CHOICES.map(vcpu => vcpu === 0
                        ? t('sessionEnvironments.boxdSizeDefault')
                        : t('sessionEnvironments.boxdSizeVcpu', { vcpu: fmtNumber(vcpu), memory: fmtNumber(vcpu * 4) }))}
                      onChange={value => updateBoxd(index, { vcpu: Number(value) })} />
                    <Btn type="button" onClick={() => {
                      removeRow('boxd', index)
                      change(withoutDefault({ ...config, boxd_profiles: boxdProfiles.filter((_, i) => i !== index) }, profile.id))
                    }}>
                      <Trash2 size={14} aria-hidden="true" />{t('sessionEnvironments.remove')}
                    </Btn>
                  </ProfileDisclosure>
                ))}
                <Btn type="button" disabled={boxdProfiles.length >= 20} onClick={() => {
                  let n = 1
                  while (boxdProfiles.some(profile => profile.id === `boxd-${n}`)) n++
                  addRow('boxd', boxdProfiles.length)
                  change({ ...config, boxd_profiles: [...boxdProfiles, {
                    id: `boxd-${n}`,
                    name: n === 1 ? t('sessionEnvironments.boxd') : `${t('sessionEnvironments.boxd')} ${fmtNumber(n)}`,
                    snapshot: '', vcpu: 0, workdir: '/home/boxd/workspace',
                  }] })
                }}><Plus size={14} aria-hidden="true" />{t('sessionEnvironments.add')}</Btn>
              </div>
            </SettingsField>
          )}
        </div>
        {/* Sticks to the bottom of the panel while there are unsaved edits, so
          * Save stays in view below a long profile list instead of scrolling
          * out of sight under the default and retention controls. */}
        <div data-testid="session-environments-save-bar"
          className={`flex flex-wrap items-center gap-2 ${draft ? 'sticky bottom-0 z-10 -mx-1 border-t border-border bg-bg px-1 py-2' : ''}`}>
          <Btn type="submit" primary={!!draft} disabled={!draft || mutation.isPending}>{t('sessionEnvironments.save')}</Btn>
          <Btn type="button" disabled={!draft || mutation.isPending} onClick={() => {
            setDraft(null)
            setSaved(false)
            setOpenRows({ coder: [], fargate: [], sprites: [], boxd: [] })
            mutation.reset()
          }}>{t('sessionEnvironments.discard')}</Btn>
          {/* A saved pool size starts billed compute in the background, so the
              confirmation says so instead of promising none was started. */}
          {saved && <span role="status" className="text-[13px] text-muted">
            {pooled && query.data?.session_environments?.coder_url
              ? t('sessionEnvironments.savedPoolStarting')
              : t('sessionEnvironments.saved')}
          </span>}
        </div>
      </fieldset>
      {/* No hand-off: navigating would discard the unsaved environment draft. */}
      <ErrorNotice
        title={t('sessionEnvironments.saveError')}
        message={mutation.isError ? mutation.error.message || t('sessionEnvironments.saveError') : null}
      />
      </section>
      <section aria-label={t('sessionEnvironments.verifyStep')} className="space-y-3 pt-3">
        <PanelSectionHeader label={t('sessionEnvironments.verifyStep')} />
        <p id={`${controlId}-verification`} className="text-[13px] text-muted">
          {draft ? t('sessionEnvironments.checkSaveFirst')
            : provider === 'coder' ? t('sessionEnvironments.verifyHint')
              : provider === 'fargate' ? t('sessionEnvironments.verifyHintFargate')
                : provider === 'sprites' ? t('sessionEnvironments.verifyHintSprites')
                  : t('sessionEnvironments.verifyHintBoxd')}
        </p>
        <Btn type="button" disabled={!!draft || !anythingSaved || checkMutation.isPending}
          aria-describedby={`${controlId}-verification`} onClick={() => checkMutation.mutate()}>
          {checkMutation.isPending ? t('sessionEnvironments.checkRunning') : t('sessionEnvironments.check')}
        </Btn>
        {checkMutation.data && !draft && <CheckResults result={checkMutation.data} provider={provider} profiles={savedProfiles}
          fargateProfiles={savedFargateProfiles} spritesProfiles={savedSpritesProfiles}
          boxdProfiles={savedBoxdProfiles} coderSaved={!!savedCoderUrl} />}
        <ErrorNotice askAgent message={checkMutation.isError ? t('sessionEnvironments.checkError') : null} />
      </section>
      <section aria-label={t('sessionEnvironments.manageStep')} className="space-y-3 pt-3">
        <PanelSectionHeader label={t('sessionEnvironments.manageStep')} />
        {/* The Manage step follows the provider tab: each one only lists and
          * explains its own workspaces. */}
        <p className="text-[13px] text-muted">
          {provider === 'coder' ? t('sessionEnvironments.managementHint')
            : provider === 'fargate' ? t('sessionEnvironments.managementHintFargate')
              : provider === 'sprites' ? t('sessionEnvironments.managementHintSprites')
                : t('sessionEnvironments.managementHintBoxd')}
        </p>
        {anythingSaved && <WorkspaceList provider={provider} />}
        {provider === 'coder' && savedCoderUrl && (
          <a href={savedCoderUrl} target="_blank" rel="noopener noreferrer"
            className="inline-flex items-center gap-1 text-[13px] text-accent hover:underline">
            {t('sessionEnvironments.openCoder')}<ExternalLink size={12} className="lucide-inline" aria-hidden="true" />
          </a>
        )}
      </section>
    </form>
  )
}
