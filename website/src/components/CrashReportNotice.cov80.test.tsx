import { screen, waitFor, fireEvent, within, act } from '@testing-library/react'
import { renderWithProviders } from '../test/helpers'
import CrashReportNotice from './CrashReportNotice'

type Summary = { newCount: number; nativeLogOff?: string }

const get = vi.fn<[], Promise<Summary>>()
const reveal = vi.fn<[], Promise<{ ok: boolean; error?: string }>>()

/** Installs (or removes) the preload bridge the component reads off `window`. */
function bridge(present: boolean) {
  const w = window as unknown as { crashReportsAPI?: unknown }
  if (present) w.crashReportsAPI = { get, reveal }
  else delete w.crashReportsAPI
}

function summary(over: Partial<Summary> = {}): Summary {
  return { newCount: 2, ...over }
}

describe('CrashReportNotice', () => {
  beforeEach(() => {
    get.mockReset()
    reveal.mockReset()
    get.mockResolvedValue(summary())
    reveal.mockResolvedValue({ ok: true })
    bridge(true)
  })
  afterEach(() => bridge(false))

  it('announces a crash count as a status region', async () => {
    renderWithProviders(<CrashReportNotice />)
    const notice = await screen.findByRole('status')
    expect(notice).toHaveTextContent(/closed unexpectedly/i)
    expect(notice).toHaveTextContent(/2/)
  })

  it('never renders without the desktop bridge, and never probes for one', async () => {
    bridge(false)
    renderWithProviders(<CrashReportNotice />)
    await waitFor(() => expect(get).not.toHaveBeenCalled())
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('stays silent when nothing new crashed', async () => {
    get.mockResolvedValue(summary({ newCount: 0 }))
    renderWithProviders(<CrashReportNotice />)
    await waitFor(() => expect(get).toHaveBeenCalled())
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  // A rejection is what a connection window pointed at a remote gateway gets
  // from the sender gate. That is the correct answer, so it must not surface as
  // a broken banner or an unhandled rejection.
  it('stays silent when the sender gate refuses the request', async () => {
    get.mockRejectedValue(new Error('crash-reports:get is restricted to the local dashboard'))
    renderWithProviders(<CrashReportNotice />)
    await waitFor(() => expect(get).toHaveBeenCalled())
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('asks the main process to reveal the ledger, naming no path itself', async () => {
    renderWithProviders(<CrashReportNotice />)
    await screen.findByRole('status')
    fireEvent.click(screen.getByRole('button', { name: /show diagnostics/i }))
    expect(reveal).toHaveBeenCalledWith()
  })

  // The banner's one action must fail LOUD, not silent: a swallowed failure
  // leaves the user told to attach files they now cannot find.
  it('surfaces a rejected reveal through an error notice with the fallback path', async () => {
    reveal.mockRejectedValue(new Error('no crash log'))
    renderWithProviders(<CrashReportNotice />)
    await screen.findByRole('status')
    fireEvent.click(screen.getByRole('button', { name: /show diagnostics/i }))
    const err = await screen.findByTestId('crash-notice-reveal-error')
    expect(err).toHaveTextContent(/crashes\.log/i)
    expect(screen.getByRole('status')).toBeInTheDocument()
  })

  // `{ ok: false }` is the other failure shape (log removed since the scan) and
  // was discarded exactly like a rejection.
  it('surfaces an ok:false reveal result, not only a rejection', async () => {
    reveal.mockResolvedValue({ ok: false, error: 'no crash log' })
    renderWithProviders(<CrashReportNotice />)
    await screen.findByRole('status')
    fireEvent.click(screen.getByRole('button', { name: /show diagnostics/i }))
    const err = await screen.findByTestId('crash-notice-reveal-error')
    expect(err).toHaveTextContent(/crashes\.log/i)
  })

  it('lets the reveal error be dismissed on its own', async () => {
    reveal.mockResolvedValue({ ok: false })
    renderWithProviders(<CrashReportNotice />)
    await screen.findByRole('status')
    fireEvent.click(screen.getByRole('button', { name: /show diagnostics/i }))
    const err = await screen.findByTestId('crash-notice-reveal-error')
    fireEvent.click(within(err).getByRole('button', { name: /dismiss/i }))
    expect(screen.queryByTestId('crash-notice-reveal-error')).not.toBeInTheDocument()
    // the crash banner itself is still there — only the error line cleared
    expect(screen.getByRole('status')).toBeInTheDocument()
  })

  it('shows no error notice when the reveal succeeds', async () => {
    renderWithProviders(<CrashReportNotice />)
    await screen.findByRole('status')
    fireEvent.click(screen.getByRole('button', { name: /show diagnostics/i }))
    await waitFor(() => expect(reveal).toHaveBeenCalled())
    expect(screen.queryByTestId('crash-notice-reveal-error')).not.toBeInTheDocument()
  })

  // The crash notice is where the user already knows something broke, so the
  // report starts here: the shared Report a Problem dialog, with the crash
  // context the banner holds (the count) already in the note.
  it('opens the report dialog with the crash pre-filled in the note', async () => {
    renderWithProviders(<CrashReportNotice />)
    await screen.findByRole('status')
    fireEvent.click(screen.getByRole('button', { name: /report issue/i }))
    const dialog = await screen.findByRole('dialog')
    expect(dialog).toHaveTextContent(/report a problem/i)
    const note = within(dialog).getByRole('textbox', { name: /what happened/i })
    expect((note as HTMLTextAreaElement).value).toMatch(/closed unexpectedly/i)
    expect((note as HTMLTextAreaElement).value).toMatch(/2/)
    expect(reveal).not.toHaveBeenCalled()
  })

  it('keeps the crash banner when the report dialog is cancelled', async () => {
    renderWithProviders(<CrashReportNotice />)
    await screen.findByRole('status')
    fireEvent.click(screen.getByRole('button', { name: /report issue/i }))
    const dialog = await screen.findByRole('dialog')
    fireEvent.click(within(dialog).getByRole('button', { name: /cancel/i }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(screen.getByRole('status')).toBeInTheDocument()
  })

  it('dismisses for the rest of the session', async () => {
    renderWithProviders(<CrashReportNotice />)
    await screen.findByRole('status')
    fireEvent.click(screen.getByRole('button', { name: /dismiss/i }))
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  // The file sink being off is machine state the reporter cannot see: without
  // this line the log they pass on reads as an ordinary capture. The line is
  // written in the reporter's words: what is not being recorded, what that means
  // for the rest of the session, what to say when reporting, then the cause as a
  // plain sentence.
  it('tells the reporter the detailed log is not being recorded and what to say', async () => {
    get.mockResolvedValue(summary({ nativeLogOff: 'unreadable' }))
    renderWithProviders(<CrashReportNotice />)
    const line = await screen.findByTestId('crash-notice-native-log-off')
    expect(line).toHaveTextContent("The detailed app log isn't being recorded this session, so it won't show anything that happens now.")
    expect(line).toHaveTextContent('When you report the problem, mention that the log was off.')
    expect(line).toHaveTextContent('Technical detail: the previous log could not be read.')
    // The code's own terms (the subsystem, the rotation step, the filename) stay out.
    expect(line).not.toHaveTextContent(/native file logging|rotated|chromium\.log/i)
    // The summary pairs crashes from earlier launches with the sink state of THIS
    // launch, so the line states only the second and claims nothing about whether
    // the file on disk covers the crash.
    expect(line).not.toHaveTextContent(/describe this crash|out of date|stale/i)
    expect(screen.getByRole('status')).toContainElement(line)
  })

  // Every code the main process can send reads as one plain sentence, and the
  // code itself is never on screen. The list mirrors NATIVE_LOG_OFF_REASONS in
  // electron/native-logging.js; a value outside it reads as the not-recorded
  // sentence, like the main process's own `unknown` fallback.
  const OFF_REASONS: Array<[code: string, cause: string]> = [
    ['unsupported-fs', 'this system could not process the previous log'],
    ['not-a-regular-file', 'the previous log is not an ordinary file, and it could not be removed'],
    ['unreadable', 'the previous log could not be read'],
    ['replace-failed', 'the previous log could not be replaced'],
    ['error', 'something went wrong while the previous log was being processed'],
    ['rotate-failed', 'the previous log could not be moved to make room for a new one'],
    ['unknown', 'the cause was not recorded'],
    ['a-code-from-a-newer-build', 'the cause was not recorded'],
  ]
  it.each(OFF_REASONS)('shows %s as a plain sentence and never as the raw code', async (code, cause) => {
    get.mockResolvedValue(summary({ nativeLogOff: code }))
    renderWithProviders(<CrashReportNotice />)
    const line = await screen.findByTestId('crash-notice-native-log-off')
    expect(line.textContent).not.toContain(code)
    expect(line).toHaveTextContent(`Technical detail: ${cause}.`)
  })

  // A previous log that could not be redacted or rotated is a failed outcome, so
  // the line is the shared error notice with the agent hand-off, like the reveal
  // error beside it, and not muted status text.
  it('renders the native-logging line as an error notice with the agent hand-off', async () => {
    get.mockResolvedValue(summary({ nativeLogOff: 'unreadable' }))
    renderWithProviders(<CrashReportNotice />)
    const line = await screen.findByTestId('crash-notice-native-log-off')
    expect(line).toHaveAttribute('role', 'alert')
    expect(within(line).getByRole('button', { name: /ask the agent/i })).toBeInTheDocument()
    // The line leaves with the banner; it has no dismiss of its own.
    expect(within(line).queryByRole('button', { name: /dismiss/i })).not.toBeInTheDocument()
  })

  it('adds no native-logging line while the sink is armed', async () => {
    renderWithProviders(<CrashReportNotice />)
    await screen.findByRole('status')
    expect(screen.queryByTestId('crash-notice-native-log-off')).not.toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  // The line lives inside the crash banner; it never raises one by itself.
  it('stays silent when logging is off but nothing new crashed', async () => {
    get.mockResolvedValue(summary({ newCount: 0, nativeLogOff: 'rotate-failed' }))
    renderWithProviders(<CrashReportNotice />)
    await waitFor(() => expect(get).toHaveBeenCalled())
    // Settle the component's own continuation of get() before asserting absence.
    await act(async () => { await get.mock.results[0].value })
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(screen.queryByTestId('crash-notice-native-log-off')).not.toBeInTheDocument()
  })

  it('keeps the native-logging line while the reveal error is shown and after it clears', async () => {
    get.mockResolvedValue(summary({ nativeLogOff: 'replace-failed' }))
    reveal.mockResolvedValue({ ok: false })
    renderWithProviders(<CrashReportNotice />)
    await screen.findByTestId('crash-notice-native-log-off')
    fireEvent.click(screen.getByRole('button', { name: /show diagnostics/i }))
    const err = await screen.findByTestId('crash-notice-reveal-error')
    expect(screen.getByTestId('crash-notice-native-log-off')).toBeInTheDocument()
    fireEvent.click(within(err).getByRole('button', { name: /dismiss/i }))
    expect(screen.getByTestId('crash-notice-native-log-off')).toBeInTheDocument()
  })
})
