import { useCallback, useEffect, useRef, useState, type ClipboardEvent, type DragEvent } from 'react'
import { carriesFiles } from '../../lib/fileDrag'
import { hasPlainClipboardText } from '../../components/composerPastePolicy'
import type { ResizeInfo } from '../../utils/resizeImage'
import type { ComposerUploadResult } from '../../utils/composerUpload'
import { i18nT } from '../../i18n/t'

/** The host owns transport and permission checks; no dashboard client fallback. */
export type ComposerAttachmentUpload = (files: File[], signal: AbortSignal) => Promise<ComposerUploadResult>

/** Staged attachments for a composer whose host owns send and recovery.
 *  Passing no upload keeps file gestures observable but performs no request.
 *  Slot changes cancel uploads; send recovery calls restore with the paths it
 *  took, so retries do not upload a second copy. */
export function useComposerAttachments({ slotKey, upload, deniedNotice }: {
  slotKey: string
  upload?: ComposerAttachmentUpload
  deniedNotice: string
}) {
  const [files, setFiles] = useState<string[]>([])
  const [resizedInfo, setResizedInfo] = useState<Record<string, ResizeInfo>>({})
  const [inFlight, setInFlight] = useState(0)
  const [notice, setNotice] = useState('')
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
    if (!upload) {
      setNotice(deniedNotice)
      return
    }
    setNotice('')
    const gen = generation.current
    const controller = new AbortController()
    controllers.current.add(controller)
    setInFlight(n => n + 1)
    void (async () => {
      try {
        const res = await upload(list, controller.signal)
        if (controller.signal.aborted || gen !== generation.current) return
        if (res.error) throw new Error(res.error)
        const paths = res.paths.filter((p): p is string => typeof p === 'string' && p !== '')
        setFiles(prev => [...prev, ...paths.filter(p => !prev.includes(p))])
        setResizedInfo(prev => ({ ...prev, ...res.resizedByPath }))
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
  }, [upload, deniedNotice])

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
    // Text-plus-image clipboard payloads retain the editor's text behavior.
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
    files, resizedInfo, uploading: inFlight > 0, notice, dismissNotice,
    addFiles, removeFile, clear, restore, onPaste, onDragOver, onDrop,
  }
}
