import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { MemberRosterRow } from '../../api/client'
import { createTestStore } from '../../test/helpers'

/* The crewmate's own settings on its profile card: permission, model, effort.
 * What is pinned here:
 *   - an unset permission reads as Trust (the default the thread opens in), and
 *     a stored choice is shown as stored, never replaced by the default;
 *   - each pick writes the crew record AND the live DM slot, so the open thread
 *     follows at once;
 *   - "Inherited" goes over the wire as '' (the record's inherit spelling).
 * SimpleSelect is a plain listbox stub (see KiroCrewAgentsPage.reasoningEffort
 * .test.tsx for why Radix cannot be driven inside act()). */

const mockApi = vi.hoisted(() => ({
  updateKirocrewAgent: vi.fn(),
  chatMode: vi.fn(),
  chatSlotModel: vi.fn(),
  chatSlotReasoningEffort: vi.fn(),
  agentResolvedModel: vi.fn(),
  memberDashboard: vi.fn(),
  memberDashboardLock: vi.fn(),
}))
vi.mock('../../api/client', () => ({ api: mockApi }))
const modelsState = vi.hoisted(() => ({ isDegraded: false }))
vi.mock('../../hooks/useAvailableModels', () => ({
  useAvailableModelsQuery: () => ({ data: [{ name: 'auto' }, { name: 'claude-opus-5' }, { name: 'claude-haiku-4' }], isDegraded: modelsState.isDegraded, error: null }),
}))
vi.mock('../../components/SimpleSelect', () => ({
  default: ({ options, value, onChange, optionLabels, 'aria-label': ariaLabel }: {
    options: string[]; value: string; onChange: (v: string) => void; optionLabels?: string[]; 'aria-label'?: string
  }) => (
    <div data-testid={`select-${ariaLabel}`}>
      <span data-testid={`value-${ariaLabel}`}>{optionLabels?.[options.indexOf(value)] ?? value}</span>
      {options.map((o, i) => (
        <button key={o || 'inherit'} type="button" role="option" aria-selected={o === value} onClick={() => onChange(o)}>
          {optionLabels?.[i] ?? o}
        </button>
      ))}
    </div>
  ),
}))

import CrewProfileSettings from './CrewProfileSettings'
import { isTrustRootTarget } from '../../guide/findTargetPolicy'
import { ApiError } from '../../api/apiError'
import { MEMBERS_ROSTER_QUERY_KEY } from '../../api/membersQuery'
import { MEMBER_DASHBOARD_QUERY_PREFIX } from '../../hooks/useWebSocket'

let lastQc: QueryClient

const member = (overrides: Partial<MemberRosterRow> = {}): MemberRosterRow => ({
  name: 'oncall', slug: 'oncall', slot_key: 'member-oncall', running: false, model: '',
  ...overrides,
} as MemberRosterRow)

function setup(m: MemberRosterRow, slotKey: string | null = 'member-oncall', waiting = false) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  qc.setQueryData(MEMBERS_ROSTER_QUERY_KEY, [m])
  lastQc = qc
  render(
    <QueryClientProvider client={qc}>
      <Provider store={createTestStore()}>
        <CrewProfileSettings member={m} slotKey={slotKey} waiting={waiting} />
      </Provider>
    </QueryClientProvider>,
  )
}
const pick = (label: string, option: string) =>
  fireEvent.click(within(screen.getByTestId(`select-${label}`)).getByRole('option', { name: option }))

beforeEach(() => {
  vi.clearAllMocks()
  modelsState.isDegraded = false
  mockApi.memberDashboard.mockResolvedValue({ structure_locked: false })
  mockApi.memberDashboardLock.mockResolvedValue({ ok: true, structure_locked: true })
  mockApi.updateKirocrewAgent.mockResolvedValue({ ok: true })
  mockApi.chatMode.mockResolvedValue({ ok: true })
  mockApi.chatSlotModel.mockResolvedValue({ ok: true })
  mockApi.chatSlotReasoningEffort.mockResolvedValue({ ok: true })
  mockApi.agentResolvedModel.mockResolvedValue({ model: 'claude-opus-5' })
})

describe('CrewProfileSettings permission', () => {
  it('reads Trust when the crewmate never chose a permission', () => {
    setup(member())
    expect(screen.getByTestId('value-Permissions')).toHaveTextContent('Trust')
  })

  it.each([['normal', 'Normal'], ['trust_reads', 'Reads']])('shows a stored %s as stored, not the default', (mode, label) => {
    setup(member({ approval_mode: mode }))
    expect(screen.getByTestId('value-Permissions')).toHaveTextContent(label)
  })

  it('keeps the permission picker out of reach of a name-only guide', () => {
    setup(member())
    // `members.permissions` is denied by id; a guide that asks for "Permissions"
    // by name must be refused too, so the picker sits in a trust-root region.
    expect(isTrustRootTarget(screen.getByTestId('select-Permissions'), 'Permissions')).toBe(true)
  })

  it('says under the picker what the chosen mode lets the crewmate do', () => {
    setup(member())
    expect(screen.getByTestId('crew-profile-settings')).toHaveTextContent('oncall runs tools without asking you first, in every chat with it. Pick Normal to approve each step.')
  })

  it('sends one compare-and-set write; the server moves the live thread', async () => {
    setup(member())
    pick('Permissions', 'Normal')
    await waitFor(() => expect(mockApi.updateKirocrewAgent).toHaveBeenCalledWith('oncall', { approval_mode: 'normal', expected_approval_mode: '' }))
    // The browser never writes the thread's permission itself.
    expect(mockApi.chatMode).not.toHaveBeenCalled()
    expect(screen.getByTestId('value-Permissions')).toHaveTextContent('Normal')
  })

  it('writes only the record when the crewmate has no thread yet', async () => {
    setup(member(), null)
    pick('Permissions', 'Reads')
    await waitFor(() => expect(mockApi.updateKirocrewAgent).toHaveBeenCalledWith('oncall', { approval_mode: 'trust_reads', expected_approval_mode: '' }))
    expect(mockApi.chatMode).not.toHaveBeenCalled()
  })

  it('takes one save at a time, so a slow Trust cannot land after a later Normal', async () => {
    let finish!: () => void
    mockApi.updateKirocrewAgent.mockReturnValueOnce(new Promise((r) => { finish = () => r({ ok: true }) }))
    setup(member({ approval_mode: 'normal' }))
    pick('Permissions', 'Trust')
    await waitFor(() => expect(mockApi.updateKirocrewAgent).toHaveBeenCalledTimes(1))
    pick('Permissions', 'Normal')
    // Refused on the spot: the first save is still in flight.
    expect(mockApi.updateKirocrewAgent).toHaveBeenCalledTimes(1)
    finish()
    await waitFor(() => expect(screen.getByTestId('value-Permissions')).toHaveTextContent('Trust'))
    expect(mockApi.updateKirocrewAgent).toHaveBeenCalledTimes(1)
  })

  it('writes a saved choice into the cached roster, so a reopen never shows the old one', async () => {
    setup(member())
    pick('Permissions', 'Normal')
    await waitFor(() => expect(lastQc.getQueryData<MemberRosterRow[]>(MEMBERS_ROSTER_QUERY_KEY)?.[0].approval_mode).toBe('normal'))
  })

  it('saves nothing while the thread is still being confirmed', async () => {
    setup(member(), null, true)
    expect(screen.getByTestId('crew-profile-settings').querySelector('fieldset')).toBeDisabled()
    expect(screen.getByTestId('crew-profile-settings-waiting')).toHaveTextContent('These unlock once the chat with this crewmate is open.')
    pick('Permissions', 'Normal')
    expect(mockApi.updateKirocrewAgent).not.toHaveBeenCalled()
    expect(screen.getByTestId('value-Permissions')).toHaveTextContent('Trust')
  })

  it('shows the current value when the server refuses a stale pick', async () => {
    mockApi.updateKirocrewAgent.mockRejectedValueOnce(
      new ApiError(409, 'conflict', JSON.stringify({ code: 'approval_mode_conflict', approval_mode: 'trust_reads' })),
    )
    setup(member({ approval_mode: 'normal' }))
    pick('Permissions', 'Trust')
    await waitFor(() => expect(screen.getByTestId('crew-profile-settings-notice')).toHaveTextContent('The permission changed elsewhere'))
    expect(screen.getByTestId('value-Permissions')).toHaveTextContent('Reads')
    expect(lastQc.getQueryData<MemberRosterRow[]>(MEMBERS_ROSTER_QUERY_KEY)?.[0].approval_mode).toBe('trust_reads')
  })

  it('puts the stored value back and says why when the save fails', async () => {
    mockApi.updateKirocrewAgent.mockRejectedValueOnce(new Error('disk full'))
    setup(member({ approval_mode: 'normal' }))
    pick('Permissions', 'Trust')
    await waitFor(() => expect(screen.getByTestId('crew-profile-settings-error')).toHaveTextContent('disk full'))
    expect(screen.getByTestId('value-Permissions')).toHaveTextContent('Normal')
    expect(mockApi.chatMode).not.toHaveBeenCalled()
  })
})

describe('CrewProfileSettings model and effort', () => {
  it('pins a model on the record and the thread', async () => {
    setup(member())
    pick('Edit default model', 'claude-opus-5')
    await waitFor(() => expect(mockApi.chatSlotModel).toHaveBeenCalledWith('member-oncall', 'claude-opus-5'))
    expect(mockApi.updateKirocrewAgent).toHaveBeenCalledWith('oncall', { model: 'claude-opus-5' })
  })

  it('sends Inherited as the empty pin', async () => {
    setup(member({ model: 'claude-opus-5' }))
    pick('Edit default model', 'Inherited')
    await waitFor(() => expect(mockApi.chatSlotModel).toHaveBeenCalledWith('member-oncall', ''))
    expect(mockApi.updateKirocrewAgent).toHaveBeenCalledWith('oncall', { model: '' })
  })

  it('offers effort on a model that reasons and writes both sides', async () => {
    setup(member({ model: 'claude-opus-5' }))
    pick('Edit reasoning effort', 'High')
    await waitFor(() => expect(mockApi.chatSlotReasoningEffort).toHaveBeenCalledWith('member-oncall', 'high'))
    expect(mockApi.updateKirocrewAgent).toHaveBeenCalledWith('oncall', { reasoning_effort: 'high' })
  })

  it('names the model an inherited pin resolves to', async () => {
    setup(member())
    expect(await screen.findByText('Using claude-opus-5, the crew default')).toBeInTheDocument()
  })

  it('shows a failed resolve instead of silently hiding effort', async () => {
    mockApi.agentResolvedModel.mockRejectedValue(new Error('resolver down'))
    setup(member())
    await waitFor(() => expect(screen.getByTestId('crew-profile-settings-resolve-error')).toHaveTextContent('resolver down'))
  })

  it('says so when the model list failed to load', () => {
    modelsState.isDegraded = true
    setup(member())
    expect(screen.getByTestId('crew-profile-settings-models-error')).not.toBeEmptyDOMElement()
  })

  it('always says what an effort level trades', () => {
    setup(member({ model: 'claude-opus-5' }))
    expect(screen.getByTestId('crew-profile-settings')).toHaveTextContent('Higher levels think longer, answer slower, and cost more.')
  })

  it('hides effort on a model that does not reason', () => {
    setup(member({ model: 'claude-haiku-4' }))
    expect(screen.queryByTestId('select-Edit reasoning effort')).toBeNull()
  })
})

/* The person's lock on the crewmate's dynamic dashboard lives HERE, on the
 * crewmate's card, as one labelled switch with a line of help. The Dashboard tab
 * only shows a badge (CrewDynamicDashboard.test.tsx pins that it has no control). */
describe('CrewProfileSettings dashboard lock', () => {
  const SWITCH = 'Lock dashboard layout'

  it('offers the switch and reads lock state', async () => {
    setup(member())
    const sw = await screen.findByRole('switch', { name: SWITCH })
    await waitFor(() => expect(sw).toHaveAttribute('aria-checked', 'false'))
    expect(screen.getByTestId('crew-profile-dashboard-lock')).toHaveTextContent(SWITCH)
    expect(screen.getByTestId('crew-profile-settings')).toHaveTextContent(
      "While locked, oncall can't replace its dashboard with a different layout or restore an older one.",
    )
  })

  it('reads the lock off the dashboard body, so it shows what the gateway holds', async () => {
    mockApi.memberDashboard.mockResolvedValue({ structure_locked: true })
    setup(member())
    const sw = await screen.findByRole('switch', { name: SWITCH })
    await waitFor(() => expect(sw).toHaveAttribute('aria-checked', 'true'))
    expect(mockApi.memberDashboard).toHaveBeenCalledWith('oncall', 'oncall')
  })

  it('is held until the read answers: a switch that moves first shows a state nothing holds', async () => {
    let answer: (v: { structure_locked: boolean }) => void = () => {}
    mockApi.memberDashboard.mockReturnValue(new Promise((r) => { answer = r }))
    setup(member())
    const sw = await screen.findByRole('switch', { name: SWITCH })
    expect(sw).toHaveAttribute('aria-disabled', 'true')
    fireEvent.click(sw)
    expect(mockApi.memberDashboardLock).not.toHaveBeenCalled()
    answer({ structure_locked: false })
    await waitFor(() => expect(sw).not.toHaveAttribute('aria-disabled'))
  })

  it('flips the lock through the owner route, then re-reads every dashboard query for the crewmate', async () => {
    setup(member())
    const sw = await screen.findByRole('switch', { name: SWITCH })
    await waitFor(() => expect(sw).not.toHaveAttribute('aria-disabled'))
    const spy = vi.spyOn(lastQc, 'invalidateQueries')
    fireEvent.click(sw)
    await waitFor(() => expect(mockApi.memberDashboardLock).toHaveBeenCalledWith('oncall', 'oncall', true))
    await waitFor(() => expect(spy).toHaveBeenCalledWith({ queryKey: [MEMBER_DASHBOARD_QUERY_PREFIX, 'oncall'] }))
    // The tab's frame reads under the same prefix, so it is refreshed by that one call.
    expect(spy.mock.calls.some(([arg]) => (arg as { queryKey: string[] }).queryKey[0] === MEMBER_DASHBOARD_QUERY_PREFIX)).toBe(true)
  })

  it('says which change failed and what stayed the same', async () => {
    mockApi.memberDashboard.mockResolvedValue({ structure_locked: true })
    mockApi.memberDashboardLock.mockRejectedValue(new Error('403'))
    setup(member())
    const sw = await screen.findByRole('switch', { name: SWITCH })
    await waitFor(() => expect(sw).not.toHaveAttribute('aria-disabled'))
    fireEvent.click(sw)
    await waitFor(() => expect(mockApi.memberDashboardLock).toHaveBeenCalledWith('oncall', 'oncall', false))
    expect(await screen.findByTestId('crew-profile-dashboard-lock-error')).toHaveTextContent(
      "Couldn't unlock the dashboard. It's still locked. Try again.",
    )
  })

  it('hides the switch and shows error + hint when the lock cannot be read', async () => {
    mockApi.memberDashboard.mockRejectedValue(new Error('500'))
    setup(member())
    expect(await screen.findByTestId('crew-profile-dashboard-lock-error')).toHaveTextContent(
      "Couldn't read whether the dashboard is locked.",
    )
    // Switch is hidden when the state is unknown (showing a dim off switch
    // for an unknown state would look identical to "unlocked" and mislead),
    // but the hint text is still shown so the section is not context-free.
    expect(screen.queryByRole('switch', { name: SWITCH })).toBeNull()
    expect(screen.getByTestId('crew-profile-settings')).toHaveTextContent("can't replace")
  })
})
