import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { render, fireEvent, act, within } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { PREVIEW_CLOSE_MS, PREVIEW_OPEN_MS, REVEAL_MS } from '../components/RedactionCards'

/**
 * The hover/focus preview on a redaction marker: the rule in plain words, and
 * for a kept blocked link an *Open once* that runs through the card's own
 * confirmation. A press never opens it, so a tap keeps opening the card.
 */

const PH = (d: string) => `[REDACTED: suspicious URL to ${d}]`
const CRED = '[REDACTED: credential]'

const link = (over: Record<string, unknown> = {}) => ({
  domain: 'reviews.corp.example',
  rule: 'exfil_query_length',
  path: '/reviews',
  query_chars: 290,
  url: 'https://reviews.corp.example/reviews?filter=abc',
  url_withheld: null,
  ...over,
})

const cred = (over: Record<string, unknown> = {}) => ({
  ordinal: 0,
  rule: 'aws_secret_access_key',
  label: 'aws_secret_access_key = ',
  source: { type: 'file', path: '~/.aws/credentials', section: 'default' },
  view_command: 'aws configure get aws_secret_access_key --profile default',
  profile_command: null,
  ...over,
})

const mouse = { pointerType: 'mouse' }
const preview = (testId: string) => document.querySelector(`[data-testid="${testId}"]`) as HTMLElement | null

function hover(el: Element) {
  fireEvent.pointerOver(el, mouse)
  act(() => { vi.advanceTimersByTime(PREVIEW_OPEN_MS) })
}

beforeEach(() => { vi.useFakeTimers() })
afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('Blocked link preview', () => {
  it('opens after the mouse rests on the chip and states the reason without the address', () => {
    const { getByTestId } = render(<MarkdownRenderer content={`See ${PH('reviews.corp.example')}`} blockedLinks={[link()]} slotKey="s1" />)
    const chip = getByTestId('blocked-link-chip')
    fireEvent.pointerOver(chip, mouse)
    act(() => { vi.advanceTimersByTime(PREVIEW_OPEN_MS - 1) })
    expect(preview('blocked-link-preview')).toBeNull()
    act(() => { vi.advanceTimersByTime(1) })
    const panel = preview('blocked-link-preview')!
    expect(panel.getAttribute('role')).toBe('dialog')
    expect(panel.textContent).toContain('Link to reviews.corp.example blocked')
    expect(panel.textContent).toContain('Reason: the query has 290 characters.')
    // The full address appears only inside the card, after the reader asks.
    expect(panel.textContent).not.toContain('filter=abc')
    const actions = within(getByTestId('redaction-preview-actions'))
    expect(actions.getAllByRole('button').map(b => b.textContent)).toEqual(['Open once', 'Allow for this host'])
    expect(getByTestId('blocked-link-inspect').getAttribute('aria-describedby')).toBe(panel.id)
  })

  it('Open once hands off to the card confirmation, which alone opens the link', () => {
    const open = vi.spyOn(window, 'open').mockReturnValue(null)
    const { getByTestId } = render(<MarkdownRenderer content={PH('reviews.corp.example')} blockedLinks={[link()]} slotKey="s1" />)
    hover(getByTestId('blocked-link-chip'))
    fireEvent.click(getByTestId('blocked-link-preview-open-once'))
    expect(preview('blocked-link-preview')).toBeNull()
    expect(open).not.toHaveBeenCalled()
    const confirm = getByTestId('blocked-link-open-confirm')
    expect(confirm.textContent).toContain('Open reviews.corp.example one time?')
    expect(document.activeElement?.textContent).toBe('Cancel')
    fireEvent.click(getByTestId('blocked-link-open-confirmed'))
    expect(open).toHaveBeenCalledWith('https://reviews.corp.example/reviews?filter=abc', '_blank', 'noopener,noreferrer')
  })

  it('a press in the panel keeps focus on the marker, so the click still lands', () => {
    const { getByTestId } = render(<MarkdownRenderer content={PH('reviews.corp.example')} blockedLinks={[link()]} />)
    const inspect = getByTestId('blocked-link-inspect')
    act(() => { inspect.focus() })
    const openOnce = getByTestId('blocked-link-preview-open-once')
    expect(fireEvent.mouseDown(openOnce)).toBe(false)
    expect(document.activeElement).toBe(inspect)
    fireEvent.click(openOnce)
    expect(getByTestId('blocked-link-open-confirm')).toBeTruthy()
  })

  it('Tab past the last action skips a link taken out of the tab order', () => {
    const { getByTestId, getByText } = render(
      <>
        <MarkdownRenderer content={PH('reviews.corp.example')} blockedLinks={[link()]} />
        <a href="#skip" tabIndex={-1}>skipped</a>
        <button type="button">after</button>
      </>,
    )
    act(() => { getByTestId('blocked-link-inspect').focus() })
    act(() => { getByTestId('blocked-link-preview-open-once').focus() })
    fireEvent.keyDown(document.activeElement!, { key: 'Tab' })
    expect(document.activeElement).toBe(getByText('after'))
  })

  it('the panel repeats no action the marker already has', () => {
    const { getByTestId, queryByTestId } = render(<MarkdownRenderer content={PH('reviews.corp.example')} blockedLinks={[link()]} slotKey="s1" />)
    hover(getByTestId('blocked-link-chip'))
    expect(queryByTestId('blocked-link-preview-details')).toBeNull()
    fireEvent.click(getByTestId('blocked-link-inspect'))
    expect(getByTestId('blocked-link-card')).toBeTruthy()
    expect(queryByTestId('blocked-link-open-confirm')).toBeNull()
  })

  it('a withheld address says why and offers no way to open it', () => {
    const rec = link({ url: null, url_withheld: 'credential', rule: 'exfil_credential' })
    const { getByTestId, queryByTestId } = render(<MarkdownRenderer content={PH('reviews.corp.example')} blockedLinks={[rec]} slotKey="s1" />)
    hover(getByTestId('blocked-link-chip'))
    const panel = preview('blocked-link-preview')!
    expect(getByTestId('blocked-link-preview-withheld').textContent).toContain('opening it would send that secret')
    // The withheld sentence is the reason; the rule's sentence is not repeated.
    expect(panel.textContent!.split('secret').length - 1).toBe(2)
    expect(queryByTestId('blocked-link-preview-open-once')).toBeNull()
    expect(queryByTestId('redaction-preview-actions')).toBeNull()
  })

  it('several links to one host point to the card, which lists each', () => {
    const recs = [link(), link({ path: '/other', url: 'https://reviews.corp.example/other?x=1' })]
    const { getByTestId, queryByTestId } = render(<MarkdownRenderer content={PH('reviews.corp.example')} blockedLinks={recs} slotKey="s1" />)
    hover(getByTestId('blocked-link-chip'))
    expect(preview('blocked-link-preview')!.textContent).toContain('Several links to this site were blocked.')
    expect(queryByTestId('blocked-link-preview-open-once')).toBeNull()
    expect(queryByTestId('redaction-preview-actions')).toBeNull()
  })

  it('stays while the mouse crosses into the panel and closes after it leaves both', () => {
    const { getByTestId } = render(<MarkdownRenderer content={PH('reviews.corp.example')} blockedLinks={[link()]} slotKey="s1" />)
    const chip = getByTestId('blocked-link-chip')
    hover(chip)
    const panel = preview('blocked-link-preview')!
    fireEvent.pointerOut(chip, { ...mouse, relatedTarget: panel })
    fireEvent.pointerOver(panel, { ...mouse, relatedTarget: chip })
    act(() => { vi.advanceTimersByTime(PREVIEW_CLOSE_MS * 2) })
    expect(preview('blocked-link-preview')).not.toBeNull()
    fireEvent.pointerOut(panel, { ...mouse, relatedTarget: document.body })
    act(() => { vi.advanceTimersByTime(PREVIEW_CLOSE_MS) })
    expect(preview('blocked-link-preview')).toBeNull()
  })

  it('a touch tap never shows the preview and keeps opening the card', () => {
    const { getByTestId } = render(<MarkdownRenderer content={PH('reviews.corp.example')} blockedLinks={[link()]} slotKey="s1" />)
    const inspect = getByTestId('blocked-link-inspect')
    fireEvent.pointerOver(inspect, { pointerType: 'touch' })
    fireEvent.pointerDown(inspect, { pointerType: 'touch' })
    act(() => { vi.advanceTimersByTime(PREVIEW_OPEN_MS) })
    // The tap's focus lands after the touch ends.
    act(() => { inspect.focus() })
    fireEvent.click(inspect)
    expect(preview('blocked-link-preview')).toBeNull()
    expect(getByTestId('blocked-link-card')).toBeTruthy()
  })

  it('keyboard focus shows it at once, Arrow Down enters it and Escape returns to the marker', () => {
    const { getByTestId } = render(
      <>
        <MarkdownRenderer content={PH('reviews.corp.example')} blockedLinks={[link()]} slotKey="s1" />
        <button type="button">after</button>
      </>,
    )
    const inspect = getByTestId('blocked-link-inspect')
    act(() => { inspect.focus() })
    expect(preview('blocked-link-preview')).not.toBeNull()
    fireEvent.keyDown(inspect, { key: 'ArrowDown' })
    expect(document.activeElement).toBe(getByTestId('blocked-link-preview-open-once'))
    fireEvent.keyDown(document.activeElement!, { key: 'Escape' })
    expect(preview('blocked-link-preview')).toBeNull()
    expect(document.activeElement).toBe(inspect)
  })

  it('Escape closes only the preview, not a drawer or dialog listening on the document', () => {
    const outer = vi.fn()
    document.addEventListener('keydown', outer)
    try {
      const { getByTestId } = render(<MarkdownRenderer content={PH('reviews.corp.example')} blockedLinks={[link()]} slotKey="s1" />)
      const inspect = getByTestId('blocked-link-inspect')
      act(() => { inspect.focus() })
      fireEvent.keyDown(inspect, { key: 'Escape' })
      expect(preview('blocked-link-preview')).toBeNull()
      expect(outer).not.toHaveBeenCalled()
      // With the preview closed, the next Escape reaches the outer listener.
      fireEvent.keyDown(inspect, { key: 'Escape' })
      expect(outer).toHaveBeenCalledTimes(1)
    } finally {
      document.removeEventListener('keydown', outer)
    }
  })

  it('a preview opened by hover alone closes on Escape and lets it reach the focused field', () => {
    const outer = vi.fn()
    document.addEventListener('keydown', outer)
    const field = document.createElement('textarea')
    document.body.appendChild(field)
    try {
      const { getByTestId } = render(<MarkdownRenderer content={PH('reviews.corp.example')} blockedLinks={[link()]} slotKey="s1" />)
      act(() => { field.focus() })
      hover(getByTestId('blocked-link-inspect'))
      expect(preview('blocked-link-preview')).not.toBeNull()
      fireEvent.keyDown(field, { key: 'Escape' })
      expect(preview('blocked-link-preview')).toBeNull()
      expect(outer).toHaveBeenCalledTimes(1)
      expect(document.activeElement).toBe(field)
    } finally {
      document.removeEventListener('keydown', outer)
      field.remove()
    }
  })

  it('Tab past the last action moves on to the control after the marker', () => {
    const { getByTestId, getByText } = render(
      <>
        <MarkdownRenderer content={PH('reviews.corp.example')} blockedLinks={[link()]} />
        <button type="button">after</button>
      </>,
    )
    const inspect = getByTestId('blocked-link-inspect')
    act(() => { inspect.focus() })
    act(() => { getByTestId('blocked-link-preview-open-once').focus() })
    fireEvent.keyDown(document.activeElement!, { key: 'Tab' })
    expect(document.activeElement).toBe(getByText('after'))
    expect(preview('blocked-link-preview')).toBeNull()
  })

  it('plain Tab on the marker is left to the browser, so a marker adds no stops', () => {
    const { getByTestId } = render(<MarkdownRenderer content={PH('reviews.corp.example')} blockedLinks={[link()]} slotKey="s1" />)
    const inspect = getByTestId('blocked-link-inspect')
    act(() => { inspect.focus() })
    expect(fireEvent.keyDown(inspect, { key: 'Tab' })).toBe(true)
    expect(document.activeElement).toBe(inspect)
  })

  it('stays hidden while the card is open, and the focus the card returns does not reopen it', () => {
    const { getByTestId } = render(<MarkdownRenderer content={PH('reviews.corp.example')} blockedLinks={[link()]} />)
    const inspect = getByTestId('blocked-link-inspect')
    act(() => { inspect.focus() })
    fireEvent.click(getByTestId('blocked-link-preview-open-once'))
    act(() => { vi.advanceTimersByTime(REVEAL_MS) })
    hover(getByTestId('blocked-link-chip'))
    expect(preview('blocked-link-preview')).toBeNull()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(document.activeElement).toBe(inspect)
    expect(preview('blocked-link-preview')).toBeNull()
  })

  it('the focus a card returns does not reopen it after focus left the card for the page', () => {
    const { getByTestId } = render(<MarkdownRenderer content={PH('reviews.corp.example')} blockedLinks={[link()]} slotKey="s1" />)
    const inspect = getByTestId('blocked-link-inspect')
    fireEvent.click(inspect)
    act(() => { vi.advanceTimersByTime(REVEAL_MS + 2000) })
    // A click on the transcript text leaves focus on the body, with no relatedTarget.
    act(() => { (document.activeElement as HTMLElement | null)?.blur() })
    expect(document.activeElement).toBe(document.body)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(document.activeElement).toBe(inspect)
    expect(preview('blocked-link-preview')).toBeNull()
  })
})

describe('Credential preview', () => {
  it('names the rule and says the value cannot be shown, with no reveal', () => {
    const { getByTestId, queryByTestId } = render(<MarkdownRenderer content={`key: ${CRED}`} redactions={[cred()]} slotKey="s1" />)
    hover(getByTestId('credential-tag'))
    const panel = preview('credential-preview')!
    expect(panel.textContent).toContain('Credential removed before saving')
    expect(panel.textContent).toContain('Matched: a value after an aws_secret_access_key label.')
    expect(getByTestId('credential-preview-floor').textContent).toBe('The value was never saved, so nothing here can show it.')
    expect(queryByTestId('redaction-preview-actions')).toBeNull()
  })

  it('names a session token as one', () => {
    const { getByTestId } = render(<MarkdownRenderer content={`token: ${CRED}`} redactions={[cred({ rule: 'aws_session_token', label: '' })]} slotKey="s1" />)
    hover(getByTestId('credential-tag'))
    expect(preview('credential-preview')!.textContent).toContain('Session token removed before saving')
  })

  it('a click on the tag opens the credential card and toggles it shut', () => {
    const { getByTestId, queryByTestId } = render(<MarkdownRenderer content={`key: ${CRED}`} redactions={[cred()]} slotKey="s1" />)
    const tag = getByTestId('credential-tag')
    hover(tag)
    fireEvent.pointerDown(tag, mouse)
    fireEvent.click(tag)
    expect(getByTestId('credential-card')).toBeTruthy()
    fireEvent.pointerDown(tag, mouse)
    fireEvent.click(tag)
    expect(tag.getAttribute('aria-expanded')).toBe('false')
    expect(queryByTestId('credential-preview')).toBeNull()
  })
})
