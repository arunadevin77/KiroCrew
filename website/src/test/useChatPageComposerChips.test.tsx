/**
 * The composer chips' model pin write (pages/chat/page/composerChips.ts): the
 * model picker's "set as this agent's default" row, driven as a hook.
 *
 * The dropdown closes the moment the row is clicked, so the outcome has to be
 * visible elsewhere:
 *  - success refreshes the dashboard and invalidates every resolved-model query,
 *    so a slot showing an inherited model picks the new pin up without a reload;
 *  - failure is said on the page (the action-error notice, titled) AND as a
 *    critical agent notification, both naming the agent and the server's message.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { http, HttpResponse } from 'msw'
import { server } from '../../integration/mocks/server'

const apiMock = vi.hoisted(() => ({ updateKirocrewAgent: vi.fn(), projectGit: vi.fn(), kirocrewConfig: vi.fn() }))
vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return { ...actual, api: { ...actual.api, ...apiMock } }
})

import { useComposerChips } from '../pages/chat/page/composerChips'
import { AcpAdapter } from '../providers/adapters/acp'
import { triggerRefresh } from '../store/dashboardSlice'
import type { ChatSlot } from '../types'

type Opts = Parameters<typeof useComposerChips>[0]

function harness(
  overrides: Partial<Opts> = {},
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } }),
) {
  const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
  const provider = {
    id: 'test-provider',
    capabilities: { reasoningEffort: false },
    resolveModel: vi.fn().mockResolvedValue('m-1'),
    resolveDefaultEffort: vi.fn().mockResolvedValue(''),
  }
  const opts: Opts = {
    currentSlot: { key: 'slot-a', agent: 'builder', model: 'm-2' } as ChatSlot,
    defaultAgent: 'default',
    pendingAgent: '',
    installedAgents: [],
    provider: provider as never,
    availableModels: [],
    codexPairModels: false,
    selectionCapabilities: undefined,
    selectionCapabilitiesQ: { isError: false },
    dispatch: vi.fn() as never,
    queryClient,
    showActionError: vi.fn(),
    ...overrides,
  }
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  )
  const hook = renderHook((p: Opts) => useComposerChips(p), { initialProps: opts, wrapper })
  return { opts, hook, invalidate }
}

beforeEach(() => {
  apiMock.updateKirocrewAgent.mockReset()
  apiMock.projectGit.mockReset()
  apiMock.kirocrewConfig.mockReset()
  apiMock.kirocrewConfig.mockResolvedValue({ agent: { model: 'global-choice' } })
})

describe('resolving the model for the actual conversation', () => {
  const globalModel = 'global-choice'
  const memberModel = 'member-choice'
  const settledTimeout = { timeout: 10_000 }

  it.each([
    { label: 'ordinary member', key: 'slot-a', kind: 'member', linked: '', expected: globalModel },
    { label: 'explicit template', key: 'slot-a', kind: 'template', linked: '', expected: globalModel },
    { label: 'member DM', key: 'member-default', kind: 'member', linked: '', expected: memberModel },
    { label: 'linked channel', key: 'member-default', kind: 'member', linked: 'slack:probe', expected: globalModel },
  ] as const)('shows the $label model through the real adapter and API client', async ({ key, kind, linked, expected }) => {
    const requests: URLSearchParams[] = []
    server.use(http.get('/api/agents/resolved-model', ({ request }) => {
      const query = new URL(request.url).searchParams
      requests.push(query)
      // These are the disjoint API answers from the backend regression: an
      // omitted context previews the member DM, not an ordinary conversation.
      const model = !query.has('session_key') || query.get('session_key') === 'member-default'
        ? memberModel : globalModel
      return HttpResponse.json({ model, pinned: true })
    }))
    const { hook } = harness({
      currentSlot: {
        key, agent: 'default', agent_kind: kind, model: '', linked_session_key: linked,
      } as ChatSlot,
      provider: new AcpAdapter() as never,
      availableModels: [{ name: 'auto' }, { name: expected }] as never,
    })
    // The adapter's API response and the hook's React Query render must both
    // settle; the initial Auto chip proves neither.
    await waitFor(() => expect(hook.result.current.shownModel).toBe(expected), settledTimeout)
    expect(requests.some(query =>
      query.get('agent') === 'default' &&
      query.get('session_key') === (linked || key) &&
      query.get('selection_kind') === kind,
    )).toBe(true)
  })

  it('keeps same-named member and template answers in separate query entries', async () => {
    server.use(http.get('/api/agents/resolved-model', ({ request }) => {
      const query = new URL(request.url).searchParams
      return HttpResponse.json({
        model: query.get('selection_kind') === 'template' ? globalModel : memberModel,
        pinned: query.get('selection_kind') !== 'template',
      })
    }))
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
    const { opts, hook } = harness({
      currentSlot: { key: 'slot-a', agent: 'default', agent_kind: 'member', model: '' } as ChatSlot,
      provider: new AcpAdapter() as never,
      availableModels: [{ name: globalModel }, { name: memberModel }] as never,
    }, queryClient)
    // Wait for the first API answer before changing only the selection kind.
    await waitFor(() => expect(hook.result.current.shownModel).toBe(memberModel), settledTimeout)
    hook.rerender({ ...opts, currentSlot: { ...opts.currentSlot!, agent_kind: 'template' } })
    // A fresh query must replace the indefinitely fresh same-name member answer.
    await waitFor(() => expect(hook.result.current.shownModel).toBe(globalModel), settledTimeout)
  })

  it('keeps ordinary and DM routes in separate query entries for the same member', async () => {
    server.use(http.get('/api/agents/resolved-model', ({ request }) => {
      const query = new URL(request.url).searchParams
      return HttpResponse.json({
        model: query.get('session_key') === 'slot-a' ? globalModel : memberModel,
        pinned: true,
      })
    }))
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
    const { opts, hook } = harness({
      currentSlot: { key: 'slot-a', agent: 'default', agent_kind: 'member', model: '' } as ChatSlot,
      provider: new AcpAdapter() as never,
      availableModels: [{ name: globalModel }, { name: memberModel }] as never,
    }, queryClient)
    // The API answer must settle before switching only the conversation key.
    await waitFor(() => expect(hook.result.current.shownModel).toBe(globalModel), settledTimeout)
    hook.rerender({ ...opts, currentSlot: { ...opts.currentSlot!, key: 'member-default' } })
    await waitFor(() => expect(hook.result.current.shownModel).toBe(memberModel), settledTimeout)
  })
})

describe('pinning the shown model to the agent', () => {
  it('writes agents.<name>.model, then refreshes and invalidates resolved models', async () => {
    apiMock.updateKirocrewAgent.mockResolvedValue({})
    const { opts, hook, invalidate } = harness()
    expect(hook.result.current._modelPinAgent).toBe('builder')
    act(() => { hook.result.current.pinModelToAgentMut.mutate({ agent: 'builder', model: 'm-2' }) })
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: ['resolved-model'] }))
    expect(apiMock.updateKirocrewAgent).toHaveBeenCalledWith('builder', { model: 'm-2' })
    expect(opts.dispatch).toHaveBeenCalledWith(triggerRefresh())
    expect(opts.showActionError).not.toHaveBeenCalled()
  })

  it('says a failed write on the page and as a critical notification', async () => {
    apiMock.updateKirocrewAgent.mockRejectedValue(new Error('config.json is read-only'))
    const { opts, hook } = harness()
    act(() => { hook.result.current.pinModelToAgentMut.mutate({ agent: 'builder', model: 'm-2' }) })
    await waitFor(() => expect(opts.showActionError).toHaveBeenCalledTimes(1))
    const [body, title] = (opts.showActionError as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(body).toBe('builder: config.json is read-only')
    expect(title).toMatch(/./)
    const note = (opts.dispatch as ReturnType<typeof vi.fn>).mock.calls.map(c => c[0]).find(a => a.type?.endsWith('addNotification'))
    expect(note.payload).toMatchObject({ kind: 'agent', priority: 'critical', title, body })
    expect(typeof note.payload.ts).toBe('string')
  })
})

describe('effort control gate on the capability read', () => {
  it('keeps effort available while the read 404s for a not-yet-registered slot (#14817)', () => {
    const { opts, hook } = harness()
    const base: Opts = {
      ...opts,
      provider: { ...(opts.provider as object), capabilities: { reasoningEffort: true } } as never,
      selectionCapabilities: { known: true, effort_supported: true, effort_levels: ['low', 'high'] } as never,
    }
    const notFound = Object.assign(new Error('not found'), { status: 404 })
    hook.rerender({ ...base, selectionCapabilitiesQ: { isError: true, error: notFound } })
    expect(hook.result.current.effortSupported).toBe(true)
    const peerDown = Object.assign(new Error('peer unavailable'), { status: 503 })
    hook.rerender({ ...base, selectionCapabilitiesQ: { isError: true, error: peerDown } })
    expect(hook.result.current.effortSupported).toBe(false)
  })
})
