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
import { useCallback, useContext, useEffect, useMemo, useRef, useState, type ClipboardEvent, type DragEvent } from 'react'
import type { AppApi } from './scopedApi'
import { AppSdkContext, apiPathAllowed } from './scopedApi'
import { carriesFiles } from '../lib/fileDrag'
import { hasPlainClipboardText } from '../components/composerPastePolicy'
import type { ResizeInfo } from '../utils/resizeImage'
import { COMPOSER_UPLOAD_PATH, uploadComposerFiles } from '../utils/composerUpload'
import { i18nT } from '../i18n/t'

/** The composer upload endpoint. A host opts in by declaring it. */
export const EMBED_UPLOAD_PATH = COMPOSER_UPLOAD_PATH

export interface EmbedAttachments {
  /** The host declared the upload path, so files can be attached. */
  granted: boolean
  /** Server paths of the staged files, in staging order. */
  files: string[]
  resizedInfo: Record<string, ResizeInfo>
  /** Uploads in flight; a send waits until this is 0. */
  uploading: boolean
  /** Names of the files still uploading, in pick order, so the composer can
   *  show what is on its way before the server returns their paths. */
  uploadingNames: string[]
  /** A refusal or failure to show above the composer; '' for none. */
  notice: string
  dismissNotice: () => void
  addFiles: (files: File[]) => void
  removeFile: (path: string) => void
  /** Drop the staged files (after a send took them). */
  clear: () => void
  /** Put a failed send's files back in front of anything staged since. */
  restore: (paths: string[]) => void
  onPaste: (e: ClipboardEvent<HTMLTextAreaElement>) => void
  onDragOver: (e: DragEvent<HTMLElement>) => void
  onDrop: (e: DragEvent<HTMLElement>) => void
}

const NO_PATHS: readonly string[] = []

export function useEmbedAttachments(api: AppApi, slotKey: string): EmbedAttachments {
  // The host's declared API paths. Read off the context directly (not through
  // useAppInfo, which throws without a provider) so a surface with no provider
  // fails CLOSED: no declared paths, no attach control.
  const allowedPaths = useContext(AppSdkContext)?.info.permissions.api ?? NO_PATHS
  const granted = apiPathAllowed(allowedPaths, EMBED_UPLOAD_PATH)
  const [files, setFiles] = useState<string[]>([])
  const [resizedInfo, setResizedInfo] = useState<Record<string, ResizeInfo>>({})
  // One entry per upload request still running, with the names it carries.
  const [inFlight, setInFlight] = useState<Array<{ id: number; names: string[] }>>([])
  const nextUploadId = useRef(0)
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
    setInFlight([])
    return () => { live.forEach(c => c.abort()); live.clear() }
  }, [slotKey])

  const addFiles = useCallback((list: File[]) => {
    if (!list.length) return
    if (!granted) {
      // Name the dashboard page that accepts files by its own rail label
      // (`nav.sessions`, the `/chat` surface), so the notice and the sidebar
      // agree in every language. Text only: a link would navigate away from
      // this embed's unsaved draft.
      setNotice(i18nT('appSdk.chatEmbed.attachments_not_allowed', { page: i18nT('nav.sessions') }))
      return
    }
    setNotice('')
    const gen = generation.current
    const controller = new AbortController()
    controllers.current.add(controller)
    const uploadId = ++nextUploadId.current
    setInFlight(prev => [...prev, { id: uploadId, names: list.map(f => f.name) }])
    void (async () => {
      try {
        // `raw` forwards the multipart body untouched (no JSON content type),
        // through the same declared-path check as every other scoped call.
        // It throws on an HTTP failure; the catch below reads the refusal.
        const res = await uploadComposerFiles(list, body =>
          api.raw(EMBED_UPLOAD_PATH, { method: 'POST', body, signal: controller.signal }))
        if (gen !== generation.current) return
        if (res.error) throw new Error(res.error)
        const paths = res.paths.filter((p): p is string => typeof p === 'string' && p !== '')
        const info = res.resizedByPath
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
        if (gen === generation.current) setInFlight(prev => prev.filter(u => u.id !== uploadId))
      }
    })()
  }, [api, granted])

  const uploadingNames = useMemo(() => inFlight.flatMap(u => u.names), [inFlight])

  const removeFile = useCallback((path: string) => {
    setFiles(prev => prev.filter(p => p !== path))
  }, [])

  const clear = useCallback(() => { setFiles([]); setResizedInfo({}) }, [])
  const restore = useCallback((paths: string[]) => {
    setFiles(prev => [...paths, ...prev.filter(p => !paths.includes(p))])
  }, [])
  const dismissNotice = useCallback(() => setNotice(''), [])

  const onPaste = useCallback((e: ClipboardEvent<HTMLTextAreaElement>) => {
    const data = e.clipboardData
    const pasted = Array.from(data?.files ?? [])
    // A paste carrying text keeps the textarea's own behaviour, as in the main
    // chat: a macOS Office text copy also carries an image of the selection,
    // and taking that image would throw the text away.
    if (!pasted.length || (data && hasPlainClipboardText(data))) return
    e.preventDefault()
    addFiles(pasted)
  }, [addFiles])

  const onDragOver = useCallback((e: DragEvent<HTMLElement>) => {
    if (carriesFiles(e.dataTransfer)) e.preventDefault()
  }, [])

  const onDrop = useCallback((e: DragEvent<HTMLElement>) => {
    if (!carriesFiles(e.dataTransfer)) return
    e.preventDefault()
    addFiles(Array.from(e.dataTransfer.files ?? []))
  }, [addFiles])

  return {
    granted, files, resizedInfo, uploading: inFlight.length > 0, uploadingNames, notice, dismissNotice,
    addFiles, removeFile, clear, restore, onPaste, onDragOver, onDrop,
  }
}
