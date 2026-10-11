import { useEffect, useId, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../../api/client'
import { acpBackendName } from '../../api/acpBackend'
import { i18nT } from '../../i18n/t'
import { useAvailableModelsQuery } from '../../hooks/useAvailableModels'
import type { KiroCrewAgent } from '../AgentSelector'
import SimpleSelect from '../SimpleSelect'
import ErrorNotice from '../ErrorNotice'
import { Btn } from '../ui'

const INHERIT_BACKEND = '__inherit__'

/** Keep the opening selection stable while roster queries refresh. */
export function useMemberBackend(
  key: string, member: KiroCrewAgent | undefined, inherited: string | undefined,
  setModel: (value: string) => void, setEffort: (value: string) => void,
) {
  const [backend, setBackend] = useState<string | null>(null)
  const opening = useRef({ key: '', backend: null as string | null, model: '', effort: '' })
  useEffect(() => {
    if (!key) { opening.current.key = ''; return }
    if (!member || opening.current.key === key) return
    opening.current = { key, backend: member.acp_backend ?? null, model: member.model || '', effort: member.reasoning_effort || '' }
    setBackend(opening.current.backend)
  }, [key, member])
  const resolvedBackend = backend ?? inherited
  const models = useAvailableModelsQuery({ backend: resolvedBackend, enabled: !!key && resolvedBackend !== undefined })
  const change = (next: string | null) => {
    setBackend(next)
    if (next === opening.current.backend) {
      setModel(opening.current.model || 'auto')
      setEffort(opening.current.effort)
    } else if ((next ?? inherited) !== resolvedBackend) {
      setModel('auto')
      setEffort('')
    }
  }
  return { backend, change, inherited, dirty: backend !== opening.current.backend, models }
}

export type MemberBackendDraft = ReturnType<typeof useMemberBackend>

export function MemberBackendField({ draft }: { draft: MemberBackendDraft }) {
  const id = useId()
  const query = useQuery({ queryKey: ['acpBackends'], queryFn: () => api.acpBackends() })
  const rows = (query.data?.backends ?? []).filter(row => row.selectable !== false)
  const selected = draft.backend ?? INHERIT_BACKEND
  const ids = [INHERIT_BACKEND, ...rows.map(row => row.id)]
  if (!ids.includes(selected)) ids.push(selected)
  const labels = ids.map(id => id === INHERIT_BACKEND
    ? i18nT('pages.kiroCrewAgentsPage.inherited_ai_app', { backend: draft.inherited === undefined ? '…' : acpBackendName({ id: draft.inherited }) })
    : acpBackendName(rows.find(row => row.id === id) ?? { id }))
  return (
    <div className="flex flex-col gap-2">
      <label htmlFor={id} className="text-[12px] text-muted">
        {i18nT('pages.kiroCrewAgentsPage.ai_app')}
      </label>
        <SimpleSelect id={id} options={ids} optionLabels={labels} value={selected}
          onChange={value => draft.change(value === INHERIT_BACKEND ? null : value)} />
      <p className="text-[12px] text-muted">{i18nT('pages.kiroCrewAgentsPage.ai_app_hint')}</p>
      {/* No hand-off: leaving here would discard the member editor draft. */}
      <ErrorNotice message={query.error ? i18nT('pages.kiroCrewAgentsPage.ai_apps_load_failed') : null} askAgent={false} />
      <ErrorNotice message={draft.models.isDegraded ? i18nT('pages.chatSidebar.model_list_failed') : null} askAgent={false} />
      {(query.isError || draft.models.isDegraded) && <Btn onClick={() => { void query.refetch(); void draft.models.refetch() }}>{i18nT('apps.designTweak.preview.retry')}</Btn>}
    </div>
  )
}
