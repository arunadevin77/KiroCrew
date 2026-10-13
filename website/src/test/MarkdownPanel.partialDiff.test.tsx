/**
 * Diff mode on a file larger than the file-read cap.
 *
 * `/api/file-read` stops at a byte cap and says so (`partial`), while
 * `/api/file-diff` returns the WHOLE HEAD body. Comparing the two drew every
 * HEAD line past the cap as deleted and hid the real edits there. With a
 * partial buffer the panel must draw git's own diff and count its +/- lines.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, within, waitFor, fireEvent, act } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

vi.mock('../pierre', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  PierreFilePair: () => <div data-testid="pierre-diff" />,
  PierrePatch: ({ patch }: { patch: string }) => <div data-testid="pierre-patch" data-value={patch} />,
  PierreEditor: () => <div data-testid="pierre-editor" />,
}))

vi.mock('../api/client', () => ({
  api: {
    artifacts: vi.fn(),
    artifact: vi.fn(),
    createArtifact: vi.fn(),
    updateArtifact: vi.fn(),
    setArtifactPinned: vi.fn(),
    revealPath: vi.fn(),
    fileDiff: vi.fn(),
  },
}))

const { api } = await import('../api/client')
const { default: MarkdownPanel } = await import('../components/MarkdownPanel')

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })

// HEAD: 300 lines. The read cap stops the buffer after line 100. The one real
// edit is at line 250, past the cut.
const head = Array.from({ length: 300 }, (_, i) => `line ${i + 1}`)
const capped = head.slice(0, 100).join('\n') + '\n'
const gitDiff = [
  'diff --git a/big.py b/big.py',
  'index 1111111..2222222 100644',
  '--- a/big.py',
  '+++ b/big.py',
  '@@ -249,3 +249,3 @@',
  ' line 249',
  '-line 250',
  '+line 250 edited',
  ' line 251',
].join('\n')

function Panel({ partial, content, readOnly = true, liveWatch = false }: { partial: boolean; content: string; readOnly?: boolean; liveWatch?: boolean }) {
  return (
    <MemoryRouter>
      <QueryClientProvider client={qc}>
        <div data-testid="tab">
          <MarkdownPanel
            embedded
            active
            readOnly={readOnly}
            liveWatch={liveWatch}
            initialDiffMode
            onContentChange={() => {}}
            onSave={async () => {}}
            onClose={() => {}}
            filePath="/repo/big.py"
            content={content}
            partial={partial}
          />
        </div>
      </QueryClientProvider>
    </MemoryRouter>
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  qc.clear()
  localStorage.clear()
  vi.stubGlobal('fetch', vi.fn(async () => ({
    ok: true, status: 200, headers: { get: () => null },
    json: async () => ({ enabled: false, supported_formats: [] }),
    text: async () => '',
  })))
  vi.mocked(api.artifacts).mockResolvedValue({ artifacts: [] } as never)
  vi.mocked(api.fileDiff).mockResolvedValue({ diff: gitDiff, original: head.join('\n') + '\n', status: 'modified' })
})

describe('MarkdownPanel diff of a partial (capped) read', () => {
  it("draws git's diff and git's counts, not a diff of the cut buffer", async () => {
    render(<Panel partial content={capped} />)
    const patch = await screen.findByTestId('pierre-patch')
    expect(patch.getAttribute('data-value')).toBe(gitDiff)
    expect(screen.queryByTestId('pierre-diff')).toBeNull()
    const tab = within(screen.getByTestId('tab'))
    // Before the fix the header read +0 -200: every line past the cut.
    await waitFor(() => expect(tab.getByText('+1')).toBeTruthy())
    expect(tab.getByText('-1')).toBeTruthy()
    expect(tab.queryByText('-200')).toBeNull()
  })

  it('shows the no-changes notice when git reports a clean partial file', async () => {
    vi.mocked(api.fileDiff).mockResolvedValue({ diff: '', original: head.join('\n') + '\n', status: 'clean' })
    render(<Panel partial content={capped} />)
    // Before the fix the cut buffer never equalled HEAD, so a clean file drew
    // a body-pair diff of phantom deletions instead of this notice.
    await screen.findByText('No changes in this file')
    expect(screen.queryByTestId('pierre-diff')).toBeNull()
    expect(screen.queryByTestId('pierre-patch')).toBeNull()
  })

  it('keeps the editor, not the no-changes notice, when editing a clean partial file', async () => {
    vi.mocked(api.fileDiff).mockResolvedValue({ diff: '', original: head.join('\n') + '\n', status: 'clean' })
    // Not read-only: a code file opens straight in the editor.
    render(<Panel partial readOnly={false} content={capped} />)
    await waitFor(() => expect(api.fileDiff).toHaveBeenCalled())
    await screen.findByTestId('pierre-editor')
    expect(screen.queryByText('No changes in this file')).toBeNull()
    expect(screen.queryByTestId('pierre-patch')).toBeNull()
  })

  it("refetches git's diff when a new partial body is read from disk", async () => {
    // The disk read answers with a different capped body, flagged truncated.
    vi.stubGlobal('fetch', vi.fn(async (url: string) => ({
      ok: true, status: 200,
      headers: { get: (h: string) => (String(url).startsWith('/api/file-read') && h === 'X-Truncated' ? 'true' : null) },
      json: async () => ({ enabled: false, supported_formats: [] }),
      text: async () => 'changed on disk\n' + capped,
    })))
    render(<Panel partial content={capped} />)
    await screen.findByTestId('pierre-patch')
    expect(api.fileDiff).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByTestId('markdown-panel-more-options'))
    fireEvent.click(await screen.findByRole('menuitem', { name: /refresh/i }))
    await waitFor(() => expect(api.fileDiff).toHaveBeenCalledTimes(2))
  })

  it("fetches git's diff once on open, and once more on a Refresh that matches the buffer", async () => {
    // An edit past the cut leaves the capped body unchanged, so the Refresh
    // read equals the buffer; the diff must still be refetched, exactly once.
    // Counts file-read bodies handed back, so the test waits on a read that
    // has LANDED rather than on a guessed delay.
    let readsLanded = 0
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      const isRead = String(url).startsWith('/api/file-read')
      return {
        ok: true, status: 200,
        headers: { get: (h: string) => (isRead && h === 'X-Truncated' ? 'true' : null) },
        json: async () => ({ enabled: false, supported_formats: [] }),
        text: async () => { if (isRead) readsLanded++; return capped },
      }
    }))
    render(<Panel partial liveWatch content={capped} />)
    await screen.findByTestId('pierre-patch')
    // The mount catch-up read lands, and must not refetch the diff.
    await waitFor(() => expect(readsLanded).toBe(1))
    await act(async () => {})
    expect(api.fileDiff).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByTestId('markdown-panel-more-options'))
    fireEvent.click(await screen.findByRole('menuitem', { name: /refresh/i }))
    await waitFor(() => expect(readsLanded).toBe(2))
    await waitFor(() => expect(api.fileDiff).toHaveBeenCalledTimes(2))
    await act(async () => {})
    expect(api.fileDiff).toHaveBeenCalledTimes(2)
  })

  it('keeps the body-pair diff for a whole buffer', async () => {
    const whole = head.map((l, i) => (i === 249 ? 'line 250 edited' : l)).join('\n') + '\n'
    render(<Panel partial={false} content={whole} />)
    await screen.findByTestId('pierre-diff')
    expect(screen.queryByTestId('pierre-patch')).toBeNull()
  })
})
