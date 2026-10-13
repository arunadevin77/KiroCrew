import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import React from 'react'

import SessionEnvironmentChip, { slotEnvironmentQueryKey, useEnvironmentStartLabel } from '../components/SessionEnvironmentChip'
import { api, type SlotEnvironment } from '../api/client'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import '../i18n'

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
  })
  return { store, ...render(<Provider store={store}><QueryClientProvider client={qc}>{ui}</QueryClientProvider></Provider>) }
}

const DEV = { id: 'dev', name: 'Coder MicroVM', provider: 'coder', workdir: '/home/coder/crew-session' }

function envResponse(profile: string | null, profiles = [DEV]): SlotEnvironment {
  return { profile, profiles }
}

beforeEach(() => {
  vi.restoreAllMocks()
})

describe('SessionEnvironmentChip', () => {
  it('renders nothing when no profile is configured', async () => {
    const get = vi.spyOn(api, 'chatSlotEnvironment').mockResolvedValue(envResponse(null, []))
    wrap(<SessionEnvironmentChip slot="chat-1" />)
    await waitFor(() => expect(get).toHaveBeenCalledWith('chat-1'))
    expect(screen.queryByTestId('composer-environment-chip')).toBeNull()
  })

  it('renders nothing when the endpoint refuses (an app caller is not the owner)', async () => {
    const get = vi.spyOn(api, 'chatSlotEnvironment').mockRejectedValue(new Error('owner_only'))
    wrap(<SessionEnvironmentChip slot="chat-1" />)
    await waitFor(() => expect(get).toHaveBeenCalled())
    expect(screen.queryByTestId('composer-environment-chip')).toBeNull()
  })

  it('shows where the session runs and binds it to a profile', async () => {
    vi.spyOn(api, 'chatSlotEnvironment').mockResolvedValue(envResponse(null))
    const put = vi.spyOn(api, 'setChatSlotEnvironment').mockResolvedValue({ profile: 'dev', applies: 'next_start' })
    const u = userEvent.setup()
    wrap(<SessionEnvironmentChip slot="chat-1" />)
    const chip = await screen.findByRole('button', { name: 'Runs on This machine' })
    await u.click(chip)
    await u.click(await screen.findByRole('menuitemradio', { name: /Coder MicroVM/ }))
    expect(put).toHaveBeenCalledWith('chat-1', 'dev')
    expect(await screen.findByRole('button', { name: 'Runs on Coder MicroVM' })).toBeInTheDocument()
  })

  it("shows each profile's provider mark, and the chosen one on the chip", async () => {
    const fly = { id: 'fly', name: 'Fly sprite', provider: 'sprites', workdir: '/home/sprite/workspace' }
    const aws = { id: 'aws', name: 'AWS task', provider: 'fargate', workdir: '/workspace' }
    const old = { id: 'old', name: 'Older gateway', provider: '', workdir: '/w' }
    vi.spyOn(api, 'chatSlotEnvironment').mockResolvedValue(envResponse('fly', [DEV, aws, fly, old]))
    const u = userEvent.setup()
    wrap(<SessionEnvironmentChip slot="chat-1" />)
    const chip = await screen.findByRole('button', { name: 'Runs on Fly sprite' })
    expect(chip.querySelector('[data-testid="provider-mark-sprites"]')).not.toBeNull()
    await u.click(chip)
    const item = (name: RegExp) => screen.getByRole('menuitemradio', { name })
    await screen.findByRole('menuitemradio', { name: /Fly sprite/ })
    expect(item(/Coder MicroVM/).querySelector('[data-testid="provider-mark-coder"]')).not.toBeNull()
    expect(item(/AWS task/).querySelector('[data-testid="provider-mark-fargate"]')).not.toBeNull()
    // An unknown provider keeps the generic cloud rather than guessing a brand.
    expect(item(/Older gateway/).querySelector('[data-testid^="provider-mark-"]')).toBeNull()
  })

  it('unbinds back to this machine with null, and re-picking the current one sends nothing', async () => {
    vi.spyOn(api, 'chatSlotEnvironment').mockResolvedValue(envResponse('dev'))
    const put = vi.spyOn(api, 'setChatSlotEnvironment').mockResolvedValue({ profile: null, applies: 'next_start' })
    const u = userEvent.setup()
    wrap(<SessionEnvironmentChip slot="chat-1" />)
    await u.click(await screen.findByRole('button', { name: 'Runs on Coder MicroVM' }))
    await u.click(await screen.findByRole('menuitemradio', { name: /Coder MicroVM/ }))
    expect(put).not.toHaveBeenCalled()
    await u.click(await screen.findByRole('button', { name: 'Runs on Coder MicroVM' }))
    await u.click(await screen.findByRole('menuitemradio', { name: /This machine/ }))
    expect(put).toHaveBeenCalledWith('chat-1', null)
  })

  it('shows the ready count for a pooled profile on the chip and in the menu', async () => {
    const pooled = { ...DEV, pool: { target: 1, ready: 1, warming: 0 } }
    const get = vi.spyOn(api, 'chatSlotEnvironment').mockResolvedValue(envResponse('dev', [pooled]))
    const u = userEvent.setup()
    wrap(<SessionEnvironmentChip slot="chat-1" />)
    const chip = await screen.findByRole('button', { name: 'Runs on Coder MicroVM (Ready: 1)' })
    expect(screen.getByTestId('environment-pool')).toHaveTextContent('Ready: 1')
    const calls = get.mock.calls.length
    await u.click(chip)
    // Opening the menu re-reads the count so it is never a stale promise.
    await waitFor(() => expect(get.mock.calls.length).toBeGreaterThan(calls))
    expect(await screen.findByRole('menuitemradio', { name: /Coder MicroVM.*Ready: 1/ })).toBeInTheDocument()
  })

  it('says a pool is warming up or empty, and shows nothing once the session is fixed', async () => {
    const warming = { ...DEV, pool: { target: 1, ready: 0, warming: 1 } }
    const get = vi.spyOn(api, 'chatSlotEnvironment').mockResolvedValue(envResponse('dev', [warming]))
    const { unmount } = wrap(<SessionEnvironmentChip slot="chat-1" />)
    expect(await screen.findByTestId('environment-pool')).toHaveTextContent('Warming up: 1')
    unmount()
    get.mockResolvedValue(envResponse('dev', [{ ...DEV, pool: { target: 1, ready: 0, warming: 0 } }]))
    const second = wrap(<SessionEnvironmentChip slot="chat-2" />)
    expect(await screen.findByTestId('environment-pool')).toHaveTextContent('None ready yet')
    second.unmount()
    get.mockResolvedValue({ ...envResponse('dev', [{ ...DEV, pool: { target: 1, ready: 1, warming: 0 } }]), locked: true })
    wrap(<SessionEnvironmentChip slot="chat-3" />)
    await screen.findByTestId('composer-environment-chip')
    expect(screen.queryByTestId('environment-pool')).toBeNull()
  })

  it('is disabled while a turn runs, and says why', async () => {
    vi.spyOn(api, 'chatSlotEnvironment').mockResolvedValue(envResponse(null))
    wrap(<SessionEnvironmentChip slot="chat-1" disabled />)
    const chip = await screen.findByRole('button', { name: 'Stop the current response to switch environments' })
    expect(chip).toBeDisabled()
  })

  it('shows the start stage and elapsed time while a remote start runs, then the name again', async () => {
    const get = vi.spyOn(api, 'chatSlotEnvironment').mockResolvedValue({
      ...envResponse('dev'),
      starting: { stage: 'provisioning', stage_elapsed_s: 30, elapsed_s: 42 },
    })
    wrap(<SessionEnvironmentChip slot="chat-1" disabled />)
    const chip = await screen.findByTestId('composer-environment-chip')
    // The stage only: the running step in the transcript carries the one timer.
    await waitFor(() => expect(chip).toHaveTextContent(/^Starting session environment$/))
    expect(chip).toHaveAccessibleName(/Starting Coder MicroVM: Starting session environment \(3\d/)
    // The stage alone is announced.
    expect(screen.getByRole('status')).toHaveTextContent('Starting session environment')
    // Polls while starting.
    await waitFor(() => expect(get.mock.calls.length).toBeGreaterThan(1), { timeout: 2500 })
    // The backend clears the stage and the runtime attaches; the next poll reverts.
    get.mockResolvedValue({ ...envResponse('dev'), starting: null, attached: true })
    await waitFor(() => expect(screen.getByTestId('composer-environment-chip')).toHaveTextContent('Coder MicroVM'), { timeout: 2500 })
  })

  it('shows setup, not nothing, before the first stage is reported', async () => {
    vi.spyOn(api, 'chatSlotEnvironment').mockResolvedValue({ ...envResponse('dev'), starting: null, attached: false })
    wrap(<SessionEnvironmentChip slot="chat-1" disabled />)
    await waitFor(() => expect(screen.getByTestId('composer-environment-chip')).toHaveTextContent('Checking environment'))
  })

  it('never shows a start stage for a local session, and does not poll', async () => {
    const get = vi.spyOn(api, 'chatSlotEnvironment').mockResolvedValue({
      ...envResponse(null),
      starting: { stage: 'provisioning', stage_elapsed_s: 1, elapsed_s: 1 },
    })
    wrap(<SessionEnvironmentChip slot="chat-1" disabled />)
    const chip = await screen.findByTestId('composer-environment-chip')
    expect(chip).toHaveTextContent('This machine')
    await new Promise((r) => setTimeout(r, 1300))
    expect(get).toHaveBeenCalledTimes(1)
  })

  it('is a fixed status, not a picker, once the session has started', async () => {
    vi.spyOn(api, 'chatSlotEnvironment').mockResolvedValue({ ...envResponse('dev'), locked: true })
    const put = vi.spyOn(api, 'setChatSlotEnvironment')
    const u = userEvent.setup()
    wrap(<SessionEnvironmentChip slot="chat-1" />)
    const chip = await screen.findByTestId('composer-environment-chip')
    expect(chip).toHaveTextContent('Coder MicroVM')
    expect(chip).toHaveAccessibleName('Runs on Coder MicroVM. Fixed for this session.')
    // It opens the metrics card, never the picker.
    vi.spyOn(api, 'chatSlotEnvironmentHealth').mockResolvedValue({ state: 'stopped', workspace: 'w' })
    await u.click(chip)
    expect(screen.queryByRole('menuitemradio')).toBeNull()
    expect(put).not.toHaveBeenCalled()
  })

  it('shows the remote environment metrics when hovering the fixed chip', async () => {
    vi.spyOn(api, 'chatSlotEnvironment').mockResolvedValue({ ...envResponse('dev'), locked: true })
    const health = vi.spyOn(api, 'chatSlotEnvironmentHealth').mockResolvedValue({
      state: 'running',
      workspace: 'crewenv-devuser-abc',
      workdir: '/home/coder/crew-session',
      stats: {
        os: 'Linux', arch: 'aarch64', python: '3.9.25', cpus: 2, load: [0.1, 0.2, 0.3],
        memory: { total_bytes: 4_000_000_000, available_bytes: 3_000_000_000 },
        disk: { total_bytes: 20_000_000_000, free_bytes: 15_000_000_000 },
        uptime_s: 3700,
      },
    })
    const u = userEvent.setup()
    wrap(<SessionEnvironmentChip slot="chat-1" />)
    await u.hover(await screen.findByTestId('composer-environment-chip'))
    const card = await screen.findByTestId('environment-health-card')
    expect(health).toHaveBeenCalledWith('chat-1')
    await waitFor(() => expect(card).toHaveTextContent('Running'))
    expect(card).toHaveTextContent('crewenv-devuser-abc')
    expect(card).toHaveTextContent('aarch64')
    expect(screen.getAllByRole('progressbar', { name: 'Memory' })[0]).toHaveAttribute('aria-valuenow', '25')
  })

  it('reports a failed switch and keeps the last confirmed environment', async () => {
    vi.spyOn(api, 'chatSlotEnvironment').mockResolvedValue(envResponse(null))
    vi.spyOn(api, 'setChatSlotEnvironment').mockRejectedValue(new Error('choices unreadable'))
    const u = userEvent.setup()
    const { store } = wrap(<SessionEnvironmentChip slot="chat-1" />)
    await u.click(await screen.findByRole('button', { name: 'Runs on This machine' }))
    await u.click(await screen.findByRole('menuitemradio', { name: /Coder MicroVM/ }))
    await waitFor(() => expect(store.getState().chat.agentSwitchNotice).toBeTruthy())
    expect(await screen.findByRole('button', { name: 'Runs on This machine' })).toBeInTheDocument()
  })
})

describe('useEnvironmentStartLabel steps', () => {
  function Probe() {
    const { data } = useQuery({
      queryKey: slotEnvironmentQueryKey('chat-1'),
      queryFn: () => api.chatSlotEnvironment('chat-1'),
    })
    return <span>{`probe:${data?.starting?.stage ?? ''}`}</span>
  }

  function Steps({ connectedBefore = false }: { connectedBefore?: boolean }) {
    const start = useEnvironmentStartLabel('chat-1', true, connectedBefore)
    return <ul data-testid="steps">{start?.steps.map(step => <li key={step.key}>{`${step.key}:${step.state}`}</li>)}</ul>
  }

  it('marks every stage before the current one done, run or not needed', async () => {
    // A warm pool start never enters provisioning (the workspace is already up)
    // and connects in under one poll, so the first read says starting_agent.
    // Both earlier stages still achieved their outcome: each shows done.
    vi.spyOn(api, 'chatSlotEnvironment').mockResolvedValue({
      ...envResponse('dev'),
      starting: {
        stage: 'starting_agent', stage_elapsed_s: 1, elapsed_s: 3,
        entered: ['checking', 'connecting', 'starting_agent'],
      },
    })
    wrap(<Steps />)
    expect(await screen.findByText('starting_agent:current')).toBeInTheDocument()
    for (const key of ['checking', 'provisioning', 'connecting']) {
      expect(screen.getByText(`${key}:done`)).toBeInTheDocument()
    }
  })

  it('shows no setup for an active session reconnecting to a running workspace', async () => {
    const get = vi.spyOn(api, 'chatSlotEnvironment').mockResolvedValue({
      ...envResponse('dev'),
      attached: true,
      starting: {
        stage: 'starting_agent', stage_elapsed_s: 1, elapsed_s: 2,
        entered: ['checking', 'connecting', 'starting_agent'],
      },
    })
    // A first-connection twin shares the query, so once IT shows steps the read
    // has landed, and the reconnecting session's empty list is a real verdict.
    wrap(<><Steps connectedBefore /><div data-testid="first"><Steps /></div></>)
    await waitFor(() => expect(get).toHaveBeenCalled())
    expect(await screen.findByText('starting_agent:current')).toBeInTheDocument()
    const [reconnecting] = screen.getAllByTestId('steps')
    expect(reconnecting).toBeEmptyDOMElement()
  })

  it('takes the gateway at its word when it says the start reconnects', async () => {
    // The composer chip has no transcript to consult; the gateway's flag is what
    // keeps it from saying "Starting agent" while the transcript says "Thinking…".
    const get = vi.spyOn(api, 'chatSlotEnvironment').mockResolvedValue({
      ...envResponse('dev'),
      attached: true,
      starting: {
        stage: 'starting_agent', stage_elapsed_s: 1, elapsed_s: 2,
        entered: ['checking', 'connecting', 'starting_agent'], reconnect: true,
      },
    })
    wrap(<><Steps /><Probe /></>)
    await waitFor(() => expect(get).toHaveBeenCalled())
    // The probe reads the same query: once it shows the stage, the hook has the
    // response too, so an empty list here is a real verdict, not an early read.
    expect(await screen.findByText('probe:starting_agent')).toBeInTheDocument()
    expect(screen.getByTestId('steps')).toBeEmptyDOMElement()
  })

  it('still shows setup when a reconnect has to start the workspace again', async () => {
    vi.spyOn(api, 'chatSlotEnvironment').mockResolvedValue({
      ...envResponse('dev'),
      starting: { stage: 'provisioning', stage_elapsed_s: 4, elapsed_s: 5, entered: ['checking', 'provisioning'] },
    })
    wrap(<Steps connectedBefore />)
    expect(await screen.findByText('provisioning:current')).toBeInTheDocument()
  })

  it('shows setup on a first connection even before the gateway reports a stage', async () => {
    vi.spyOn(api, 'chatSlotEnvironment').mockResolvedValue({ ...envResponse('dev'), attached: false })
    wrap(<Steps />)
    expect(await screen.findByText('checking:current')).toBeInTheDocument()
  })

  it('marks later stages pending while an earlier one runs', async () => {
    vi.spyOn(api, 'chatSlotEnvironment').mockResolvedValue({
      ...envResponse('dev'),
      starting: { stage: 'provisioning', stage_elapsed_s: 9, elapsed_s: 10 },
    })
    wrap(<Steps />)
    expect(await screen.findByText('provisioning:current')).toBeInTheDocument()
    expect(screen.getByText('checking:done')).toBeInTheDocument()
    expect(screen.getByText('connecting:pending')).toBeInTheDocument()
    expect(screen.getByText('starting_agent:pending')).toBeInTheDocument()
  })
})
