/**
 * ChatEmbed hands a host's `onFileOpen` / `onArtifactOpen` straight to the
 * transcript, so a tool row's file chip and an artifact link can open inside
 * the host app. Without them the transcript gets nothing, as before.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, act, waitFor } from '@testing-library/react'
import React from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const mockGet = vi.fn()
const mockPost = vi.fn()

vi.mock('../app-sdk/index', () => ({
  useAppApi: () => ({ get: mockGet, post: mockPost }),
  useAppInfo: () => ({ name: 'test-app', version: '0', permissions: { api: ['/api/chat'], events: [] } }),
}))

const { listProps } = vi.hoisted(() => ({ listProps: vi.fn() }))
vi.mock('../app-sdk/ChatMessageList', () => ({
  default: (props: Record<string, unknown>) => {
    listProps(props)
    return <div data-testid="chat-message-list" />
  },
}))

import ChatEmbed from '../app-sdk/ChatEmbed'

beforeEach(() => {
  vi.clearAllMocks()
  Element.prototype.scrollIntoView = vi.fn()
  mockGet.mockResolvedValue({ messages: [], running: false, title: '' })
  mockPost.mockResolvedValue({})
})

async function mount(ui: React.ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  await act(async () => {
    render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>)
  })
  await waitFor(() => expect(listProps).toHaveBeenCalled())
  return listProps.mock.calls.at(-1)![0] as Record<string, unknown>
}

describe('ChatEmbed open handlers', () => {
  it('passes onFileOpen and onArtifactOpen to the transcript', async () => {
    const onFileOpen = vi.fn()
    const onArtifactOpen = vi.fn()
    const props = await mount(<ChatEmbed slotKey="s" onFileOpen={onFileOpen} onArtifactOpen={onArtifactOpen} />)
    expect(props.onFileOpen).toBe(onFileOpen)
    expect(props.onArtifactOpen).toBe(onArtifactOpen)
  })

  it('passes neither when the host supplies none', async () => {
    const props = await mount(<ChatEmbed slotKey="s" />)
    expect(props.onFileOpen).toBeUndefined()
    expect(props.onArtifactOpen).toBeUndefined()
  })
})
