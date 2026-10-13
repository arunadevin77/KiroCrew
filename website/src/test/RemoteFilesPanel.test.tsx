import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import React from 'react'

// The real editor is a lazy Shiki/Pierre chunk; a textarea stands in for it so
// the panel's buffer, dirty and save handling are what is under test.
vi.mock('../pierre', () => ({
  PierreEditor: ({ file, onChange, onSave }: { file: { contents: string }; onChange: (s: string) => void; onSave?: () => void }) => (
    <textarea
      aria-label="editor"
      defaultValue={file.contents}
      onChange={(e) => onChange(e.target.value)}
      onKeyDown={(e) => { if (e.key === 's' && e.metaKey) onSave?.() }}
    />
  ),
  PierrePatch: ({ patch }: { patch: string }) => <pre data-testid="patch">{patch}</pre>,
}))

import RemoteFilesPanel from '../pages/chat/RemoteFilesPanel'
import RemoteGitPanel, { parsePorcelain } from '../pages/chat/RemoteGitPanel'
import { api } from '../api/client'
import { ApiError } from '../api/apiError'
import '../i18n'

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

const ROOT = { root: '/home/coder/w', truncated: false }

beforeEach(() => {
  vi.restoreAllMocks()
})

describe('RemoteFilesPanel', () => {
  function mockTree() {
    vi.spyOn(api, 'chatSlotEnvironmentList').mockImplementation(async (_slot, path) =>
      path === '.'
        ? { ...ROOT, path: '.', entries: [{ name: 'src', dir: true }, { name: 'README.md', dir: false }] }
        : { ...ROOT, path: 'src', entries: [{ name: 'main.py', dir: false }] },
    )
    return vi.spyOn(api, 'chatSlotEnvironmentRead').mockResolvedValue({
      ...ROOT, path: 'src/main.py', content: 'print("hi")\n', binary: false,
    })
  }

  it('lists the remote root, expands folders lazily and opens a file in the editor', async () => {
    const read = mockTree()
    const u = userEvent.setup()
    wrap(<RemoteFilesPanel slot="chat-1" name="Coder MicroVM" workdir="/home/coder/w" />)
    expect(await screen.findByText('Files on Coder MicroVM')).toBeInTheDocument()
    await u.click(await screen.findByRole('treeitem', { name: /src/ }))
    await u.click(await screen.findByRole('treeitem', { name: /main\.py/ }))
    expect(read).toHaveBeenCalledWith('chat-1', 'src/main.py')
    expect(await screen.findByLabelText('editor')).toHaveValue('print("hi")\n')
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled()
  })

  it('saves an edit with the opened text as the expected base', async () => {
    mockTree()
    const write = vi.spyOn(api, 'chatSlotEnvironmentWrite').mockResolvedValue({ ok: true, path: 'src/main.py' })
    const u = userEvent.setup()
    wrap(<RemoteFilesPanel slot="chat-1" name="Coder MicroVM" workdir="/home/coder/w" />)
    await u.click(await screen.findByRole('treeitem', { name: /src/ }))
    await u.click(await screen.findByRole('treeitem', { name: /main\.py/ }))
    await u.type(await screen.findByLabelText('editor'), '# edit')
    await u.click(screen.getByRole('button', { name: 'Save' }))
    expect(write).toHaveBeenCalledWith('chat-1', 'src/main.py', 'print("hi")\n# edit', 'print("hi")\n')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled())
  })

  it('reports a stale save instead of overwriting, and keeps the edit', async () => {
    mockTree()
    vi.spyOn(api, 'chatSlotEnvironmentWrite').mockRejectedValue(
      new ApiError(409, 'changed', JSON.stringify({ code: 'file_changed' })),
    )
    const u = userEvent.setup()
    wrap(<RemoteFilesPanel slot="chat-1" name="Coder MicroVM" workdir="/home/coder/w" />)
    await u.click(await screen.findByRole('treeitem', { name: /src/ }))
    await u.click(await screen.findByRole('treeitem', { name: /main\.py/ }))
    await u.type(await screen.findByLabelText('editor'), 'x')
    await u.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByText(/changed in the session environment since you opened it/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled()
  })

  it('never offers to save a truncated file', async () => {
    vi.spyOn(api, 'chatSlotEnvironmentList').mockResolvedValue({ ...ROOT, path: '.', entries: [{ name: 'big.log', dir: false }] })
    vi.spyOn(api, 'chatSlotEnvironmentRead').mockResolvedValue({ ...ROOT, path: 'big.log', content: 'x', binary: false, truncated: true })
    const u = userEvent.setup()
    wrap(<RemoteFilesPanel slot="chat-1" name="Coder MicroVM" workdir="/home/coder/w" />)
    await u.click(await screen.findByRole('treeitem', { name: /big\.log/ }))
    expect(await screen.findByText(/showing the first 256 KB/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Save' })).toBeNull()
  })

  it('says the workspace is stopped instead of a generic failure', async () => {
    vi.spyOn(api, 'chatSlotEnvironmentList').mockRejectedValue(
      new ApiError(409, 'stopped', JSON.stringify({ error: 'stopped', code: 'workspace_stopped' })),
    )
    wrap(<RemoteFilesPanel slot="chat-1" name="Coder MicroVM" workdir="/home/coder/w" />)
    expect(await screen.findByText(/The session environment is stopped/)).toBeInTheDocument()
  })
})

describe('RemoteGitPanel', () => {
  it('parses porcelain status including renames', () => {
    expect(parsePorcelain('## main...origin/main\n M a.py\n?? b.txt\nR  old.py -> new.py\n')).toEqual({
      branch: 'main...origin/main',
      rows: [{ code: ' M', path: 'a.py' }, { code: '??', path: 'b.txt' }, { code: 'R ', path: 'new.py' }],
    })
  })

  it('shows branch, changes and history, and a file diff on click', async () => {
    const git = vi.spyOn(api, 'chatSlotEnvironmentGit').mockImplementation(async (_slot, op, path) => {
      if (op === 'git_status') return { root: '/w', git: true, output: '## main\n M a.py\n' }
      if (op === 'git_log') return { root: '/w', git: true, output: 'abc123\tme\t2 hours ago\tfirst commit' }
      return { root: '/w', git: true, output: `diff --git a/${path} b/${path}\n` }
    })
    const u = userEvent.setup()
    wrap(<RemoteGitPanel slot="chat-1" name="Coder MicroVM" />)
    expect(await screen.findByText('main')).toBeInTheDocument()
    expect(await screen.findByText('first commit')).toBeInTheDocument()
    await u.click(screen.getByRole('button', { name: /a\.py/ }))
    expect(await screen.findByTestId('patch')).toHaveTextContent('diff --git a/a.py')
    expect(git).toHaveBeenCalledWith('chat-1', 'git_diff', 'a.py')
  })

  it('says when the workdir is not a repository', async () => {
    vi.spyOn(api, 'chatSlotEnvironmentGit').mockResolvedValue({ root: '/w', git: false, error_text: 'not a git repository' })
    wrap(<RemoteGitPanel slot="chat-1" name="Coder MicroVM" />)
    expect(await screen.findByText(/not a Git repository/)).toBeInTheDocument()
  })
})
