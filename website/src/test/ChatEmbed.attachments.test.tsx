import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, act, fireEvent, waitFor } from '@testing-library/react'
import React from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const mockGet = vi.fn()
const mockPost = vi.fn()
const mockRaw = vi.fn()

vi.mock('../app-sdk/index', () => ({
  useAppApi: () => ({ get: mockGet, post: mockPost, raw: mockRaw }),
}))

vi.mock('../app-sdk/ChatMessageList', () => ({
  default: () => <div data-testid="chat-message-list" />,
}))

import ChatEmbed from '../app-sdk/ChatEmbed'
import { AppSdkContext, apiPathAllowed, type AppSdkContextValue } from '../app-sdk/scopedApi'
import { EMBED_UPLOAD_PATH } from '../app-sdk/useEmbedAttachments'

let queryClient: QueryClient

/** Mount the embed under a host context that declares `api`. */
function mount(api: string[], props: Partial<React.ComponentProps<typeof ChatEmbed>> = {}) {
  const ctx = {
    api: { get: mockGet, post: mockPost, raw: mockRaw },
    info: { name: 'host', version: '0', permissions: { api, events: [] } },
    subscribe: () => () => {},
    navigate: () => {},
    notify: () => {},
  } as unknown as AppSdkContextValue
  return render(
    <QueryClientProvider client={queryClient}>
      <AppSdkContext.Provider value={ctx}>
        <ChatEmbed slotKey="s1" {...props} />
      </AppSdkContext.Provider>
    </QueryClientProvider>,
  )
}

const png = () => new File(['x'], 'shot.png', { type: 'image/png' })
const pdf = () => new File(['x'], 'spec.pdf', { type: 'application/pdf' })
const uploadReturns = (paths: string[]) =>
  mockRaw.mockResolvedValueOnce(new Response(JSON.stringify({ paths }), { status: 200 }))

const GRANTED = ['/api/chat', '/api/chat/slots', EMBED_UPLOAD_PATH]
const UNGRANTED = ['/api/chat', '/api/chat/slots']

beforeEach(() => {
  vi.clearAllMocks()
  // A Once-queued response a failed test never consumed must not leak.
  mockRaw.mockReset()
  Element.prototype.scrollIntoView = vi.fn()
  mockGet.mockResolvedValue({ messages: [], running: false, title: '' })
  mockPost.mockResolvedValue({})
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
})

describe('apiPathAllowed', () => {
  it('matches the scoped client rules and grants nothing implicitly', () => {
    expect(apiPathAllowed([EMBED_UPLOAD_PATH], EMBED_UPLOAD_PATH)).toBe(true)
    expect(apiPathAllowed(['/api/upload/*'], EMBED_UPLOAD_PATH)).toBe(true)
    expect(apiPathAllowed(['/api/chat', '/api/chat/slots'], EMBED_UPLOAD_PATH)).toBe(false)
    expect(apiPathAllowed([], EMBED_UPLOAD_PATH)).toBe(false)
    expect(apiPathAllowed([EMBED_UPLOAD_PATH], '/api/upload/file/../../secret')).toBe(false)
    expect(apiPathAllowed(['/api/*'], '//evil.example/api/upload/file')).toBe(false)
  })
})

describe('ChatEmbed attachments without the upload grant', () => {
  it('hides the attach control and uploads nothing', async () => {
    await act(async () => { mount(UNGRANTED) })
    expect(screen.queryByLabelText('Attach files')).toBeNull()
    expect(screen.queryByTestId('chat-embed-file-input')).toBeNull()
  })

  it('explains a pasted file instead of dropping it silently', async () => {
    await act(async () => { mount(UNGRANTED) })
    const box = screen.getByLabelText('Chat message')
    const notPrevented = fireEvent.paste(box, { clipboardData: { files: [png()], types: ['Files'] } })
    expect(notPrevented).toBe(false)
    expect(await screen.findByTestId('chat-embed-attach-notice')).toHaveTextContent("This app can't attach files")
    expect(mockRaw).not.toHaveBeenCalled()
  })

  it('swallows a dropped file so the page does not navigate to it, and explains', async () => {
    await act(async () => { mount(UNGRANTED) })
    const box = screen.getByLabelText('Chat message')
    const notPrevented = fireEvent.drop(box, { dataTransfer: { files: [pdf()], types: ['Files'] } })
    expect(notPrevented).toBe(false)
    expect(await screen.findByTestId('chat-embed-attach-notice')).toBeInTheDocument()
    expect(mockRaw).not.toHaveBeenCalled()
  })

  it('fails closed with no host context at all', async () => {
    await act(async () => {
      render(<QueryClientProvider client={queryClient}><ChatEmbed slotKey="s1" /></QueryClientProvider>)
    })
    expect(screen.queryByLabelText('Attach files')).toBeNull()
  })
})

describe('ChatEmbed attachments with the upload grant', () => {
  it('leaves a plain-text paste to the textarea', async () => {
    await act(async () => { mount(GRANTED) })
    const box = screen.getByLabelText('Chat message')
    const notPrevented = fireEvent.paste(box, { clipboardData: { files: [], types: ['text/plain'] } })
    expect(notPrevented).toBe(true)
    expect(mockRaw).not.toHaveBeenCalled()
  })

  it('uploads a pasted image through the scoped client and sends it inline', async () => {
    uploadReturns(['/up/shot.png'])
    await act(async () => { mount(GRANTED) })
    expect(screen.getByLabelText('Attach files')).toBeInTheDocument()
    const box = screen.getByLabelText('Chat message')
    fireEvent.paste(box, { clipboardData: { files: [png()], types: ['Files'] } })
    await waitFor(() => expect(mockRaw).toHaveBeenCalledTimes(1))
    const [path, init] = mockRaw.mock.calls[0]
    expect(path).toBe(EMBED_UPLOAD_PATH)
    expect(init.method).toBe('POST')
    expect(init.body).toBeInstanceOf(FormData)
    expect((init.body as FormData).getAll('file')).toHaveLength(1)

    // An attachment alone is a sendable turn.
    const sendBtn = screen.getByLabelText('Send message')
    await waitFor(() => expect(sendBtn).not.toBeDisabled())
    await act(async () => { fireEvent.click(sendBtn) })
    const chat = mockPost.mock.calls.find(c => c[0] === '/api/chat')
    expect(chat).toBeTruthy()
    expect(chat![1].message).toContain('/up/shot.png')
  })

  it('sends a picked non-image file as meta.files with its marker', async () => {
    uploadReturns(['/up/spec.pdf'])
    await act(async () => { mount(GRANTED) })
    const input = screen.getByTestId('chat-embed-file-input') as HTMLInputElement
    fireEvent.change(input, { target: { files: [pdf()] } })
    await waitFor(() => expect(mockRaw).toHaveBeenCalledTimes(1))
    const box = screen.getByLabelText('Chat message')
    fireEvent.change(box, { target: { value: 'review this' } })
    const sendBtn = screen.getByLabelText('Send message')
    await waitFor(() => expect(sendBtn).not.toBeDisabled())
    await act(async () => { fireEvent.click(sendBtn) })
    const chat = mockPost.mock.calls.find(c => c[0] === '/api/chat')!
    expect(chat[1].meta).toEqual({ files: ['/up/spec.pdf'] })
    expect(chat[1].message).toContain('[attached_file 1]')
    expect(chat[1].message).toContain('review this')
  })

  it('hands meta to a host onSend', async () => {
    uploadReturns(['/up/spec.pdf'])
    const onSend = vi.fn().mockResolvedValue(undefined)
    await act(async () => { mount(GRANTED, { onSend }) })
    fireEvent.drop(screen.getByLabelText('Chat message'), { dataTransfer: { files: [pdf()], types: ['Files'] } })
    const sendBtn = screen.getByLabelText('Send message')
    await waitFor(() => expect(sendBtn).not.toBeDisabled())
    await act(async () => { fireEvent.click(sendBtn) })
    expect(onSend).toHaveBeenCalledWith(expect.stringContaining('[attached_file 1]'), { files: ['/up/spec.pdf'] })
  })

  it('shows the server refusal when an upload fails', async () => {
    mockRaw.mockRejectedValueOnce(Object.assign(new Error('API 413'), { status: 413, body: '{"error":"file too large"}' }))
    await act(async () => { mount(GRANTED) })
    fireEvent.paste(screen.getByLabelText('Chat message'), { clipboardData: { files: [pdf()], types: ['Files'] } })
    expect(await screen.findByTestId('chat-embed-attach-notice')).toHaveTextContent('Upload failed: file too large')
  })
})
