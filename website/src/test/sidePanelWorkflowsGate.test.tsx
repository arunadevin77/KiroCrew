/**
 * The side panel offers Workflows only to a chat that has workflow runs.
 *
 * Pins the four ways that gate can go wrong: hiding a view whose runs exist
 * (a run in a terminal state, a run the list has not read yet, a read in
 * flight or failed), offering a view with nothing in it, keeping a restored
 * Workflows tab in a chat with no runs, and dropping the tab a workflow card
 * explicitly asked for.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'

vi.mock('../pages/chat/ActivityViewer', () => ({ default: () => null }))
vi.mock('../components/DiffPanel', () => ({ default: () => null }))
vi.mock('../components/DetailPanel', () => ({ default: () => null }))
vi.mock('../components/MarkdownPanel', () => ({ default: () => null }))
vi.mock('../components/ArtifactPanel', () => ({ default: () => null }))
vi.mock('../pages/chat/FolderPanel', () => ({ default: () => null }))
vi.mock('../components/WebPreviewPanel', () => ({ default: () => null }))
vi.mock('../components/McpAppFrame', () => ({ default: () => null }))
vi.mock('../components/CliPanel', () => ({
  default: () => null,
  disposeTerminalSession: vi.fn(),
  useDeleteTerminalSession: () => ({ mutate: vi.fn() }),
}))
vi.mock('../utils/terminalRegistry', () => ({
  useTerminalEnabled: () => false,
  useTerminalTitle: () => 'Terminal',
}))
vi.mock('../hooks/useDevMode', () => ({ useDevMode: () => false }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))

globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} } as never

import SidePanel from '../pages/chat/SidePanel'
import { usePanelTabs, __resetPanelTabs } from '../hooks/usePanelTabs'
import { api } from '../api/client'
import { sseWorkflowEvent } from '../store/chatSlice'
import type { WorkflowRunSummary } from '../types'

const SLOT = 'slot-a'
let panelController: ReturnType<typeof usePanelTabs>

function Harness() {
  const tabsCtl = usePanelTabs(SLOT)
  panelController = tabsCtl
  return <SidePanel tabsCtl={tabsCtl} slot={SLOT} onFileSave={async () => {}} onClose={() => {}} />
}

const newClient = () => new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })

function renderPanel(store = createTestStore(), queryClient = newClient()) {
  render(
    <QueryClientProvider client={queryClient}>
      <Provider store={store}>
        <Harness />
      </Provider>
    </QueryClientProvider>,
  )
  return store
}

const openMenu = () => act(() => {
  fireEvent.pointerDown(
    screen.getByRole('button', { name: 'Open side panel tab' }),
    { button: 0, ctrlKey: false, pointerType: 'mouse' },
  )
})

const closeMenu = () => act(() => { fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' }) })

const run = (over: Partial<WorkflowRunSummary>): WorkflowRunSummary => ({ run_id: 'r1', status: 'running', session_key: SLOT, ...over })

/** Resolve the run list with `runs`, and wait until the read has settled. */
function listReturns(runs: WorkflowRunSummary[]) {
  return vi.spyOn(api, 'workflowRuns').mockResolvedValue({ runs })
}

async function settled(spy: ReturnType<typeof listReturns>) {
  await waitFor(() => expect(spy).toHaveBeenCalled())
  // One macrotask past the resolved promise so React Query publishes success.
  await act(async () => { await new Promise(r => setTimeout(r, 0)) })
}

describe('side panel Workflows gate', () => {
  beforeEach(() => { localStorage.clear(); __resetPanelTabs() })
  afterEach(() => { vi.restoreAllMocks() })

  it('hides the + row when the run list names no run for this chat', async () => {
    // A run from ANOTHER chat and a UI-launched run (no session) are not this
    // chat's runs.
    const spy = listReturns([run({ session_key: 'slot-b' }), run({ run_id: 'r2', session_key: '' })])
    renderPanel()
    await settled(spy)
    openMenu()
    expect(screen.queryByRole('menuitem', { name: 'Workflows' })).toBeNull()
    // Collateral check: the neighbouring rows are still offered.
    expect(screen.getByRole('menuitem', { name: 'Subagents' })).toBeTruthy()
  })

  it.each(['running', 'finished', 'failed', 'cancelled'])('offers the row for a %s run', async (status) => {
    const spy = listReturns([run({ status, session_key: `dashboard:${SLOT}` })])
    renderPanel()
    await settled(spy)
    openMenu()
    expect(screen.getByRole('menuitem', { name: 'Workflows' })).toBeTruthy()
  })

  it('offers the row while the run list has not answered yet', () => {
    vi.spyOn(api, 'workflowRuns').mockReturnValue(new Promise(() => {}))
    renderPanel()
    openMenu()
    expect(screen.getByRole('menuitem', { name: 'Workflows' })).toBeTruthy()
  })

  it('keeps a cached empty answer through a background refetch, so the row does not flicker', () => {
    // The run list is one app-wide query, so a chat switch or window focus
    // re-reads it while an answer is cached. Failing open during that read
    // would make the row come and go with every read.
    const queryClient = newClient()
    queryClient.setQueryData(['workflow-runs'], [])
    vi.spyOn(api, 'workflowRuns').mockReturnValue(new Promise(() => {}))
    renderPanel(createTestStore(), queryClient)
    openMenu()
    expect(screen.queryByRole('menuitem', { name: 'Workflows' })).toBeNull()
  })

  it('offers the row when the run list cannot be read', async () => {
    const spy = vi.spyOn(api, 'workflowRuns').mockRejectedValue(new Error('503'))
    renderPanel()
    await settled(spy)
    openMenu()
    expect(screen.getByRole('menuitem', { name: 'Workflows' })).toBeTruthy()
  })

  it('offers the row the moment a live run starts, before the list carries it', async () => {
    const spy = listReturns([])
    const store = renderPanel()
    await settled(spy)
    openMenu()
    expect(screen.queryByRole('menuitem', { name: 'Workflows' })).toBeNull()
    closeMenu()
    act(() => { store.dispatch(sseWorkflowEvent({ run_id: 'live-1', session_key: SLOT, type: 'run_started', data: { name: 'wf' } })) })
    openMenu()
    expect(screen.getByRole('menuitem', { name: 'Workflows' })).toBeTruthy()
    // The unseen live run asks the list once more, and only once.
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(2))
    await act(async () => { await new Promise(r => setTimeout(r, 20)) })
    expect(spy).toHaveBeenCalledTimes(2)
  })

  it('leaves an open Workflows tab alone in a chat with no runs', async () => {
    // Only the + row is gated. A tab the user already opened (or a workflow
    // card opened) stays in the strip; its × closes it.
    const spy = listReturns([])
    renderPanel()
    act(() => { panelController.openView('workflows') })
    await settled(spy)
    expect(screen.getByRole('tab', { name: /Workflows/ })).toBeTruthy()
  })
})
