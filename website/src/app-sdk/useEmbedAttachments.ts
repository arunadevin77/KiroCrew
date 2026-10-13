/**
 * Attachments for the ChatEmbed composer: paste, drop and a file picker stage
 * files through the gateway's composer upload (`POST /api/upload/file`), and
 * the staged paths ride the send the same way the main chat's do (image lines
 * plus `meta.files`, built by chat-core's `buildOutgoingTurn`).
 *
 * The upload writes to the gateway's disk, so it is an explicit host grant:
 * the request goes through the app's scoped client, and `granted` is true only
 * when the host's declared API paths cover the upload path. Nothing grants it
 * by default. Without the grant the attach control is hidden, and a pasted or
 * dropped file shows `notice` instead of failing silently -- a drop is always
 * swallowed, because the browser's default for a file dropped on a textarea is
 * to navigate the page to that file.
 */
import { useCallback, useContext, useEffect, useRef, useState, type ClipboardEvent, type DragEvent } from 'react'
import type { AppApi } from './scopedApi'
import { AppSdkContext, apiPathAllowed } from './scopedApi'
import { resizeImageForModel, type ResizeInfo } from '../utils/resizeImage'
import { i18nT } from '../i18n/t'

/** The composer upload endpoint. A host opts in by declaring it. */
export const EMBED_UPLOAD_PATH = '/api/upload/file'

export interface EmbedAttachments {
  /** The host declared the upload path, so files can be attached. */
  granted: boolean
  /** Server paths of the staged files, in staging order. */
  files: string[]
  resizedInfo: Record<string, ResizeInfo>
  /** Uploads in flight; a send waits until this is 0. */
  uploading: boolean
  /** A refusal or failure to show above the composer; '' for none. */
  notice: string
  dismissNotice: () => void
  addFiles: (files: File[]) => void
  removeFile: (path: string) => void
  /** Drop the staged files (after a send took them). */
  clear: () => void
  onPaste: (e: ClipboardEvent<HTMLTextAreaElement>) => void
  onDragOver: (e: DragEvent<HTMLElement>) => void
  onDrop: (e: DragEvent<HTMLElement>) => void
}

const hasFiles = (dt: DataTransfer | null | undefined): boolean =>
  !!dt && Array.from(dt.types ?? []).includes('Files')

const NO_PATHS: readonly string[] = []

export function useEmbedAttachments(api: AppApi, slotKey: string): EmbedAttachments {
  // The host's declared API paths. Read off the context directly (not through
  // useAppInfo, which throws without a provider) so a surface with no provider
  // fails CLOSED: no declared paths, no attach control.
  const allowedPaths = useContext(AppSdkContext)?.info.permissions.api ?? NO_PATHS
  const granted = apiPathAllowed(allowedPaths, EMBED_UPLOAD_PATH)
  const [files, setFiles] = useState<string[]>([])
  const [resizedInfo, setResizedInfo] = useState<Record<string, ResizeInfo>>({})
  const [inFlight, setInFlight] = useState(0)
  const [notice, setNotice] = useState('')
  // Uploads started for the current slot. A slot switch or unmount aborts
  // them, and a late result for an earlier slot is dropped rather than staged
  // on a different session's composer.
  const controllers = useRef(new Set<AbortController>())
  const generation = useRef(0)

  useEffect(() => {
    const live = controllers.current
    generation.current += 1
    setFiles([])
    setResizedInfo({})
    setNotice('')
    setInFlight(0)
    return () => { live.forEach(c => c.abort()); live.clear() }
  }, [slotKey])

  const addFiles = useCallback((list: File[]) => {
    if (!list.length) return
    if (!granted) {
      setNotice(i18nT('appSdk.chatEmbed.attachments_not_allowed'))
      return
    }
    setNotice('')
    const gen = generation.current
    const controller = new AbortController()
    controllers.current.add(controller)
    setInFlight(n => n + 1)
    void (async () => {
      try {
        const prepared = await Promise.all(list.map(f => resizeImageForModel(f)))
        const fd = new FormData()
        prepared.forEach(p => fd.append('file', p.file))
        // `raw` forwards the multipart body untouched (no JSON content type),
        // through the same declared-path check as every other scoped call.
        const res = await api.raw(EMBED_UPLOAD_PATH, { method: 'POST', body: fd, signal: controller.signal })
        let body: { paths?: unknown; error?: string } = {}
        try { body = await res.json() } catch { /* empty or non-JSON body */ }
        if (gen !== generation.current) return
        if (body.error) throw new Error(body.error)
        if (!Array.isArray(body.paths)) throw new Error(i18nT('api.client.unexpected_server_response'))
        const paths = (body.paths as unknown[]).filter((p): p is string => typeof p === 'string' && p !== '')
        const info: Record<string, ResizeInfo> = {}
        prepared.forEach((p, i) => { if (p.info && paths[i]) info[paths[i]] = p.info })
        setFiles(prev => [...prev, ...paths.filter(p => !prev.includes(p))])
        setResizedInfo(prev => ({ ...prev, ...info }))
      } catch (err) {
        if (controller.signal.aborted || gen !== generation.current) return
        const status = (err as { status?: number }).status
        const raw = (err as { body?: string }).body
        let reason = err instanceof Error ? err.message : String(err)
        if (typeof status === 'number' && raw) {
          try { reason = (JSON.parse(raw) as { error?: string }).error || reason } catch { /* keep message */ }
        }
        setNotice(i18nT('pages.chatPage.upload_failed_error', { error: reason }))
      } finally {
        controllers.current.delete(controller)
        if (gen === generation.current) setInFlight(n => Math.max(0, n - 1))
      }
    })()
  }, [api, granted])

  const removeFile = useCallback((path: string) => {
    setFiles(prev => prev.filter(p => p !== path))
  }, [])

  const clear = useCallback(() => { setFiles([]); setResizedInfo({}) }, [])
  const dismissNotice = useCallback(() => setNotice(''), [])

  const onPaste = useCallback((e: ClipboardEvent<HTMLTextAreaElement>) => {
    const pasted = Array.from(e.clipboardData?.files ?? [])
    // Plain text pastes keep the textarea's own behaviour.
    if (!pasted.length) return
    e.preventDefault()
    addFiles(pasted)
  }, [addFiles])

  const onDragOver = useCallback((e: DragEvent<HTMLElement>) => {
    if (hasFiles(e.dataTransfer)) e.preventDefault()
  }, [])

  const onDrop = useCallback((e: DragEvent<HTMLElement>) => {
    if (!hasFiles(e.dataTransfer)) return
    e.preventDefault()
    addFiles(Array.from(e.dataTransfer.files ?? []))
  }, [addFiles])

  return {
    granted, files, resizedInfo, uploading: inFlight > 0, notice, dismissNotice,
    addFiles, removeFile, clear, onPaste, onDragOver, onDrop,
  }
}
