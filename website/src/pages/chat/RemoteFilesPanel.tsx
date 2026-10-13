import { useCallback, useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { ChevronDown, ChevronRight, Cloud, FileText, Folder, RefreshCw, Save, TerminalSquare } from 'lucide-react'
import { api, type RemoteFileContent } from '../../api/client'
import { ApiError } from '../../api/apiError'
import ErrorNotice from '../../components/ErrorNotice'
import { Btn, IconButton } from '../../components/ui'
import { PierreEditor } from '../../pierre'

/** Query-key root for every remote listing/read of one slot. */
export const remoteKey = (slot: string) => ['remote-files', slot] as const

export function errorCode(error: unknown): string {
  if (error instanceof ApiError) {
    try {
      const parsed = JSON.parse(error.body) as { code?: unknown }
      return typeof parsed.code === 'string' ? parsed.code : ''
    } catch {
      return ''
    }
  }
  return ''
}

function joinPath(dir: string, name: string): string {
  return dir === '.' || dir === '' ? name : `${dir}/${name}`
}

function describe(t: (key: string) => string, error: unknown): string {
  switch (errorCode(error)) {
    case 'workspace_stopped': return t('sessionEnvironments.remoteFiles.stopped')
    case 'path_refused': return t('sessionEnvironments.remoteFiles.refused')
    case 'file_changed': return t('sessionEnvironments.remoteFiles.conflict')
    default: return t('sessionEnvironments.remoteFiles.error')
  }
}

/** The Files tab for a session on a REMOTE environment.
 *
 *  The local file browser and file tabs read the gateway's disk, which a remote
 *  session's agent cannot see, so this lists, reads and saves the guest workspace
 *  through the owner-only remote endpoints. Directories load lazily; a file opens
 *  in the same code editor the file tabs use, below the tree. Edits are kept per
 *  file until saved, so moving between files never discards one, and a save is
 *  refused (not applied) when the file changed under it since it was opened. */
export default function RemoteFilesPanel({ slot, name, workdir, onOpenTerminal }: {
  slot: string; name: string; workdir: string
  /** Opens a terminal in this remote workspace; withheld when terminals are off. */
  onOpenTerminal?: () => void
}) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const [selected, setSelected] = useState<string | null>(null)
  /** Unsaved buffers by path; absent means "the file as last read". */
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  const file = useQuery({
    queryKey: [...remoteKey(slot), 'read', selected],
    queryFn: () => api.chatSlotEnvironmentRead(slot, selected ?? ''),
    enabled: !!selected,
    retry: false,
    staleTime: Infinity,
  })
  const save = useMutation({
    mutationFn: (v: { path: string; content: string; expected: string }) =>
      api.chatSlotEnvironmentWrite(slot, v.path, v.content, v.expected),
    onSuccess: (_r, v) => {
      queryClient.setQueryData<RemoteFileContent>([...remoteKey(slot), 'read', v.path], (old) =>
        old ? { ...old, content: v.content } : old,
      )
      setDrafts((d) => { const next = { ...d }; delete next[v.path]; return next })
    },
  })
  const refresh = () => { void queryClient.invalidateQueries({ queryKey: remoteKey(slot) }) }

  const data = file.data
  // A file the guest cut at its read cap, or could not decode, must never be
  // saved back: the buffer is not the whole file.
  const editable = !!data && !data.binary && !data.truncated
  const draft = selected ? drafts[selected] : undefined
  const content = draft ?? data?.content ?? ''
  const dirty = draft !== undefined && draft !== data?.content
  const editorFile = useMemo(
    () => ({ name: selected ?? '', contents: content }),
    // `content` changes on every keystroke; the editor owns its own buffer, so
    // only a file switch or a fresh read re-seeds it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [selected, data?.content],
  )
  const doSave = useCallback(() => {
    if (!selected || !data || !editable || !dirty || save.isPending) return
    save.mutate({ path: selected, content, expected: data.content })
  }, [selected, data, editable, dirty, content, save])

  return (
    <div className="h-full flex flex-col min-h-0" data-testid="remote-files-panel">
      <div className="flex items-center gap-2 px-3 h-9 border-b border-border shrink-0">
        <Cloud size={14} className="shrink-0 text-accent" aria-hidden />
        <span className="text-[12.5px] text-text truncate" title={workdir}>
          {t('sessionEnvironments.remoteFiles.title', { name })}
        </span>
        <span className="text-[11.5px] text-muted font-mono truncate min-w-0 flex-1" title={workdir}>{workdir}</span>
        {onOpenTerminal && (
          <IconButton aria-label={t('sessionEnvironments.remoteFiles.openTerminal')} title={t('sessionEnvironments.remoteFiles.openTerminal')} onClick={onOpenTerminal}>
            <TerminalSquare className="lucide-inline" />
          </IconButton>
        )}
        <IconButton aria-label={t('sessionEnvironments.remoteFiles.refresh')} title={t('sessionEnvironments.remoteFiles.refresh')} onClick={refresh}>
          <RefreshCw className="lucide-inline" />
        </IconButton>
      </div>
      <div className="flex-1 min-h-0 overflow-auto py-1" role="tree" aria-label={t('sessionEnvironments.remoteFiles.title', { name })}>
        <RemoteDir slot={slot} path="." depth={0} selected={selected} dirtyPaths={drafts} onSelect={setSelected} />
      </div>
      <div className="h-[55%] min-h-[160px] border-t border-border flex flex-col min-h-0">
        {!selected ? (
          <div className="flex-1 flex items-center justify-center px-6 text-center text-[12.5px] text-muted">
            {t('sessionEnvironments.remoteFiles.selectHint')}
          </div>
        ) : file.isError ? (
          <div className="p-3">
            <ErrorNotice message={describe(t, file.error)} askAgent />
          </div>
        ) : data ? (
          <>
            <div className="flex items-center gap-2 px-3 h-8 border-b border-border shrink-0">
              <span className="text-[11.5px] text-muted font-mono truncate min-w-0 flex-1" title={data.path}>
                {data.path}
                {dirty ? ' •' : ''}
                {data.truncated ? ` · ${t('sessionEnvironments.remoteFiles.truncatedFile')}` : ''}
              </span>
              {editable && (
                <Btn
                  type="button"
                  className="h-6 px-2 gap-1 text-[12px]"
                  disabled={!dirty || save.isPending}
                  onClick={doSave}
                  title={t('sessionEnvironments.remoteFiles.save')}
                >
                  <Save className="lucide-inline" aria-hidden />
                  {save.isPending ? t('sessionEnvironments.remoteFiles.saving') : t('sessionEnvironments.remoteFiles.save')}
                </Btn>
              )}
            </div>
            {save.isError && (
              <div className="px-3 py-2 border-b border-border">
                {/* No hand-off: the editor below holds an unsaved buffer. */}
                <ErrorNotice variant="inline" message={describe(t, save.error)} onDismiss={() => save.reset()} />
              </div>
            )}
            {data.binary ? (
              <div className="p-3 text-[12.5px] text-muted">{t('sessionEnvironments.remoteFiles.binary')}</div>
            ) : editable ? (
              <div className="flex-1 min-h-0 overflow-hidden">
                <PierreEditor
                  file={editorFile}
                  onChange={(next) => setDrafts((d) => ({ ...d, [selected]: next }))}
                  onSave={doSave}
                  className="h-full"
                />
              </div>
            ) : (
              <pre className="flex-1 min-h-0 overflow-auto m-0 p-3 text-[12px] font-mono text-text whitespace-pre">{data.content}</pre>
            )}
          </>
        ) : (
          <div className="p-3 text-[12.5px] text-muted" aria-busy="true">{t('sessionEnvironments.remoteFiles.loading')}</div>
        )}
      </div>
    </div>
  )
}

function RemoteDir({ slot, path, depth, selected, dirtyPaths, onSelect }: {
  slot: string; path: string; depth: number; selected: string | null
  dirtyPaths: Record<string, string>; onSelect: (path: string) => void
}) {
  const { t } = useTranslation()
  const [open, setOpen] = useState<Set<string>>(() => new Set())
  const listing = useQuery({
    queryKey: [...remoteKey(slot), 'list', path],
    queryFn: () => api.chatSlotEnvironmentList(slot, path),
    retry: false,
    staleTime: 15_000,
  })
  const pad = { paddingLeft: 8 + depth * 14 }
  if (listing.isError) {
    return (
      <div className="px-3 py-2" style={pad}>
        <ErrorNotice variant="inline" message={describe(t, listing.error)} askAgent />
      </div>
    )
  }
  if (!listing.data) {
    return <div className="py-1 text-[12px] text-muted" style={pad} aria-busy="true">{t('sessionEnvironments.remoteFiles.loading')}</div>
  }
  if (listing.data.entries.length === 0) {
    return <div className="py-1 text-[12px] text-muted" style={pad}>{t('sessionEnvironments.remoteFiles.empty')}</div>
  }
  const toggle = (child: string) =>
    setOpen((prev) => {
      const next = new Set(prev)
      if (next.has(child)) next.delete(child); else next.add(child)
      return next
    })
  return (
    <div role="group">
      {listing.data.entries.map((entry) => {
        const child = joinPath(path, entry.name)
        const expanded = open.has(child)
        return (
          <div key={child}>
            <Btn
              type="button"
              role="treeitem"
              aria-expanded={entry.dir ? expanded : undefined}
              aria-selected={!entry.dir && selected === child}
              onClick={() => (entry.dir ? toggle(child) : onSelect(child))}
              className={`w-full justify-start gap-1.5 h-7 rounded-none border-none bg-transparent text-[12.5px] font-normal hover:bg-bg-hover ${!entry.dir && selected === child ? 'bg-bg-hover text-text' : 'text-text'}`}
              style={pad}
              title={child}
            >
              {entry.dir
                ? (expanded ? <ChevronDown className="lucide-inline shrink-0" aria-hidden /> : <ChevronRight className="lucide-inline shrink-0" aria-hidden />)
                : <span className="w-[1em] shrink-0" aria-hidden />}
              {entry.dir
                ? <Folder className="lucide-inline shrink-0 text-muted" aria-hidden />
                : <FileText className="lucide-inline shrink-0 text-muted" aria-hidden />}
              <span className="truncate">{entry.name}{dirtyPaths[child] !== undefined ? ' •' : ''}</span>
            </Btn>
            {entry.dir && expanded && (
              <RemoteDir slot={slot} path={child} depth={depth + 1} selected={selected} dirtyPaths={dirtyPaths} onSelect={onSelect} />
            )}
          </div>
        )
      })}
      {listing.data.truncated && (
        <div className="py-1 text-[11.5px] text-muted" style={pad}>{t('sessionEnvironments.remoteFiles.truncatedDir')}</div>
      )}
    </div>
  )
}
