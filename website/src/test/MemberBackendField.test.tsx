import { useState } from 'react'
import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import type { KiroCrewAgent } from '../components/AgentSelector'
import { MemberBackendField, useMemberBackend } from '../components/crew/MemberBackendField'
import type { SimpleSelectProps } from '../components/SimpleSelect'
import CrewProfileSettings from '../pages/members/CrewProfileSettings'
import { i18nT } from '../i18n/t'
import { renderHookWithProviders, renderWithProviders } from './helpers'

vi.mock('../components/SimpleSelect', () => ({
  default: ({ id, options, optionLabels, value, onChange, 'aria-label': label }: SimpleSelectProps) =>
    <select id={id} aria-label={label} value={value} onChange={event => onChange(event.target.value)}>
      {options.map((option, index) => <option key={option} value={option}>{optionLabels?.[index] ?? option}</option>)}
    </select>,
}))

afterEach(() => vi.restoreAllMocks())

function member(backend: string | null = 'codex'): KiroCrewAgent {
  return {
    name: 'worker', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default',
    acp_backend: backend, model: 'saved-choice', reasoning_effort: 'high', description: '', source: 'kirocrew',
  }
}

describe('member backend draft', () => {
  it('reports a guarded profile save as neutral status without changing the thread', async () => {
    vi.spyOn(api, 'agentResolvedModel').mockResolvedValue({ resolved_acp_backend: 'codex', model: 'saved-choice' })
    vi.spyOn(api, 'models').mockResolvedValue([{ model_name: 'new-choice' }, { model_name: 'saved-choice' }])
    const save = vi.spyOn(api, 'updateKirocrewAgent').mockResolvedValue({ ok: true })
    const liveModel = vi.spyOn(api, 'chatSlotModel').mockResolvedValue({ ok: false, applied: false })
    renderWithProviders(<CrewProfileSettings
      member={{ name: 'worker', slug: 'worker', slot_key: 'member-worker', running: false, model: 'saved-choice' }}
      slotKey="member-worker"
    />)
    // agentResolvedModel enables the backend's models query; wait for its option to render.
    await waitFor(() => {
      expect(api.models).toHaveBeenCalledWith('codex')
      expect(screen.getByRole('option', { name: 'new-choice' })).toBeInTheDocument()
    }, { timeout: 5000 })
    const modelPicker = screen.getByRole('combobox', { name: i18nT('pages.kiroCrewAgentsPage.edit_model') })
    fireEvent.change(modelPicker, { target: { value: 'new-choice' } })
    await waitFor(() => expect(save).toHaveBeenCalledWith('worker', { model: 'new-choice' }))
    // Saving the record precedes the guarded slot write that publishes this status.
    const status = await screen.findByText(i18nT('pages.membersPage.profile_settings_saved_for_new_sessions'), undefined, { timeout: 5000 })
    expect(liveModel).toHaveBeenCalledWith('member-worker', 'new-choice', 'codex')
    expect(status).toHaveAttribute('role', 'status')
    expect(status).toHaveClass('text-muted')
    expect(screen.queryByText(i18nT('pages.kiroCrewAgentsPage.ai_app_hint'))).not.toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(liveModel).toHaveBeenCalledTimes(1)

    save.mockRejectedValueOnce(new Error('save refused'))
    fireEvent.change(modelPicker, { target: { value: 'saved-choice' } })
    const error = await screen.findByRole('alert')
    expect(error).toHaveAttribute('data-testid', 'crew-profile-settings-error')
    expect(error).toHaveTextContent('save refused')
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(liveModel).toHaveBeenCalledTimes(1)
  })

  it('keeps Kiro distinct from inheritance and restores the opening choices', async () => {
    vi.spyOn(api, 'acpBackends').mockResolvedValue({
      backends: ['', 'kas', 'codex'].map(id => ({
        id, policy_id: id || 'kiro', selectable: true, independent_setup: false,
        installed: 'installed', missing_components: [], install_command: '', restart_required: false,
      })),
    })
    vi.spyOn(api, 'models').mockImplementation(async backend => [
      { model_name: `${backend || 'kiro'}-choice` },
    ])
    const row = member()
    const { result } = renderHookWithProviders(() => {
      const [model, setModel] = useState(row.model || 'auto')
      const [effort, setEffort] = useState(row.reasoning_effort || '')
      return { model, effort, draft: useMemberBackend(row.name, row, 'kas', setModel, setEffort) }
    })
    await waitFor(() => expect(result.current.draft.models.data.map(row => row.name)).toContain('codex-choice'))
    const editor = renderWithProviders(<MemberBackendField draft={result.current.draft} />)
    const kasName = i18nT('pages.developer.agentBackendTab.kas_kiro_agent')
    await screen.findByRole('option', { name: kasName })
    const defaultOption = screen.getByRole('option', {
      name: i18nT('pages.kiroCrewAgentsPage.inherited_ai_app', { backend: kasName }),
    }) as HTMLOptionElement
    expect(defaultOption.textContent).not.toMatch(/\([^)]*\(/)
    expect(screen.getByText(i18nT('pages.kiroCrewAgentsPage.ai_app_hint'))).toBeInTheDocument()
    const picker = screen.getByLabelText(i18nT('pages.kiroCrewAgentsPage.ai_app'))
    fireEvent.change(picker, { target: { value: '' } })
    await waitFor(() => expect(result.current.draft.models.data.map(row => row.name)).toContain('kiro-choice'))
    expect(result.current.model).toBe('auto')
    expect(result.current.effort).toBe('')
    expect(result.current.draft.backend).toBe('')
    expect(result.current.draft.dirty).toBe(true)
    editor.rerender(<MemberBackendField draft={result.current.draft} />)
    fireEvent.change(picker, { target: { value: defaultOption.value } })
    expect(result.current.draft.backend).toBeNull()
    editor.rerender(<MemberBackendField draft={result.current.draft} />)
    fireEvent.change(picker, { target: { value: 'kas' } })
    expect(result.current.draft.backend).toBe('kas')
    editor.rerender(<MemberBackendField draft={result.current.draft} />)
    fireEvent.change(picker, { target: { value: 'codex' } })
    expect(result.current.model).toBe('saved-choice')
    expect(result.current.effort).toBe('high')
    expect(result.current.draft.dirty).toBe(false)
  })

  it('keeps a pending backend when a roster refetch replaces the record', async () => {
    vi.spyOn(api, 'models').mockResolvedValue([{ model_name: 'available-choice' }])
    const { result } = renderHookWithProviders(() => {
      const [row, setRow] = useState(member())
      const [model, setModel] = useState(row.model || 'auto')
      const [effort, setEffort] = useState(row.reasoning_effort || '')
      return { setRow, model, effort, draft: useMemberBackend(row.name, row, '', setModel, setEffort) }
    })
    await waitFor(() => expect(result.current.draft.backend).toBe('codex'))
    act(() => result.current.draft.change(''))
    act(() => result.current.setRow({ ...member(), model: 'refetched-choice' }))
    expect(result.current.draft.backend).toBe('')
    act(() => result.current.draft.change('codex'))
    expect(result.current.model).toBe('saved-choice')
  })

  it('keeps its last catalog through a failed refresh without clearing pins', async () => {
    const fetcher = vi.spyOn(api, 'models').mockResolvedValue([{ model_name: 'saved-choice' }])
    const row = member()
    const setModel = vi.fn()
    const setEffort = vi.fn()
    const { result } = renderHookWithProviders(() => useMemberBackend(row.name, row, '', setModel, setEffort))
    await waitFor(() => expect(result.current.models.data.map(row => row.name)).toContain('saved-choice'))
    fetcher.mockRejectedValue(new Error('offline'))
    await act(async () => { await result.current.models.refetch() })
    await waitFor(() => expect(result.current.models.isDegraded).toBe(true))
    expect(result.current.models.data.map(row => row.name)).toContain('saved-choice')
    expect(setModel).not.toHaveBeenCalled()
    expect(setEffort).not.toHaveBeenCalled()
  })
})
