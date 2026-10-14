import React, { createRef } from 'react'
import { act, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { ComposerAttach } from './Attach'
import { useComposerAttachments, type ComposerAttachmentUpload } from './useComposerAttachments'
import type { ComposerUploadResult } from '../../utils/composerUpload'

const file = () => new File(['example'], 'example.pdf', { type: 'application/pdf' })
const resultFor = (paths: string[]): ComposerUploadResult => ({ paths, resized: [], resizedByPath: {} })
function pendingUpload() {
  let settle!: (result: ComposerUploadResult) => void
  const promise = new Promise<ComposerUploadResult>(resolve => { settle = resolve })
  const upload = vi.fn<ComposerAttachmentUpload>().mockReturnValue(promise)
  return { upload, settle }
}

describe('ComposerAttach', () => {
  it('forwards the real input ref and keeps host hints and identity', () => {
    const ref = createRef<HTMLInputElement>()
    const onFiles = vi.fn()
    render(<ComposerAttach ref={ref} onFiles={onFiles} id="picker" aria-label="Files" accept=".pdf" multiple className="sr-only" />)
    expect(ref.current).toBe(screen.getByLabelText('Files'))
    expect(ref.current).toHaveAttribute('type', 'file')
    expect(ref.current).toHaveAttribute('accept', '.pdf')
    expect(ref.current).toHaveAttribute('multiple')
    expect(ref.current).toHaveAttribute('id', 'picker')
    expect(ref.current).toHaveClass('sr-only')
    fireEvent.change(ref.current!, { target: { files: [] } })
    expect(onFiles).not.toHaveBeenCalled()
    const picked = file()
    Object.defineProperty(ref.current, 'value', { configurable: true, writable: true, value: 'selected-file' })
    fireEvent.change(ref.current!, { target: { files: [picked] } })
    expect(onFiles).toHaveBeenCalledWith([picked])
    expect(ref.current!.value).toBe('')
    fireEvent.change(ref.current!, { target: { files: [picked] } })
    expect(onFiles).toHaveBeenCalledTimes(2)
  })
})

describe('shared attachment staging', () => {
  it('has no fallback upload when the host supplies none', () => {
    const { result } = renderHook(() => useComposerAttachments({ slotKey: 'one', deniedNotice: 'Not allowed' }))
    act(() => result.current.addFiles([file()]))
    expect(result.current.notice).toBe('Not allowed')
    expect(result.current.uploading).toBe(false)
    expect(result.current.files).toEqual([])
    act(() => result.current.dismissNotice())
    expect(result.current.notice).toBe('')
  })

  it('keeps the upload hold until every concurrent upload settles', async () => {
    const first = pendingUpload()
    const second = pendingUpload()
    const upload = vi.fn<ComposerAttachmentUpload>()
      .mockImplementationOnce(first.upload).mockImplementationOnce(second.upload)
    const { result } = renderHook(() => useComposerAttachments({ slotKey: 'one', upload, deniedNotice: '' }))
    act(() => { result.current.addFiles([file()]); result.current.addFiles([file()]) })
    expect(result.current.uploading).toBe(true)
    await act(async () => first.settle(resultFor(['/up/first.pdf'])))
    expect(result.current.uploading).toBe(true)
    await act(async () => second.settle(resultFor(['/up/second.pdf'])))
    expect(result.current.uploading).toBe(false)
    expect(result.current.files).toEqual(['/up/first.pdf', '/up/second.pdf'])
    act(() => result.current.clear())
    act(() => result.current.restore(['/up/second.pdf']))
    act(() => result.current.restore(['/up/first.pdf', '/up/second.pdf']))
    expect(result.current.files).toEqual(['/up/first.pdf', '/up/second.pdf'])
    act(() => result.current.removeFile('/up/first.pdf'))
    expect(result.current.files).toEqual(['/up/second.pdf'])
  })

  it('aborts the old slot and ignores its late result without releasing the new hold', async () => {
    const old = pendingUpload()
    const next = pendingUpload()
    const upload = vi.fn<ComposerAttachmentUpload>().mockImplementationOnce(old.upload).mockImplementationOnce(next.upload)
    const { result, rerender } = renderHook(({ slotKey }) => useComposerAttachments({ slotKey, upload, deniedNotice: '' }), { initialProps: { slotKey: 'one' } })
    act(() => result.current.addFiles([file()]))
    const oldSignal = old.upload.mock.calls[0][1]
    rerender({ slotKey: 'two' })
    expect(oldSignal.aborted).toBe(true)
    act(() => result.current.addFiles([file()]))
    await act(async () => old.settle(resultFor(['/up/old.pdf'])))
    expect(result.current.files).toEqual([])
    expect(result.current.uploading).toBe(true)
    await act(async () => next.settle(resultFor(['/up/new.pdf'])))
    expect(result.current.files).toEqual(['/up/new.pdf'])
    expect(result.current.uploading).toBe(false)
  })

  it('aborts when unmounted, even if the transport resolves afterward', async () => {
    const pending = pendingUpload()
    const { result, unmount } = renderHook(() => useComposerAttachments({ slotKey: 'one', upload: pending.upload, deniedNotice: '' }))
    act(() => result.current.addFiles([file()]))
    const signal = pending.upload.mock.calls[0][1]
    unmount()
    expect(signal.aborted).toBe(true)
    await act(async () => pending.settle(resultFor(['/up/late.pdf'])))
  })

  it('keeps staged files on an upload failure and accepts no new files after opt-out', async () => {
    const upload = vi.fn<ComposerAttachmentUpload>().mockResolvedValueOnce(resultFor(['/up/kept.pdf']))
      .mockRejectedValueOnce(Object.assign(new Error('API 413'), { status: 413, body: '{"error":"file too large"}' }))
    const { result, rerender } = renderHook(({ allowed }) => useComposerAttachments({ slotKey: 'one', upload: allowed ? upload : undefined, deniedNotice: 'Not allowed' }), { initialProps: { allowed: true } })
    act(() => result.current.addFiles([file()]))
    await waitFor(() => expect(result.current.files).toEqual(['/up/kept.pdf']))
    act(() => result.current.addFiles([file()]))
    await waitFor(() => expect(result.current.notice).toContain('file too large'))
    expect(result.current.files).toEqual(['/up/kept.pdf'])
    rerender({ allowed: false })
    act(() => result.current.addFiles([file()]))
    expect(upload).toHaveBeenCalledTimes(2)
    expect(result.current.notice).toBe('Not allowed')
  })
})
