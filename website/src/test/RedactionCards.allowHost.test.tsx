import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, fireEvent, waitFor, act, within } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { api } from '../api/client'
import { queryClient } from '../api/queryClient'

/**
 * Allowing a blocked link's host is one flow wherever it is offered: the same
 * confirmation, the same owner-only route, and a refresh of the Settings list
 * (`redaction-allowed-hosts`) so a loaded Allowed link hosts card shows the
 * host it can be revoked from.
 */

const PH = (d: string) => `[REDACTED: suspicious URL to ${d}]`

const link = (over: Record<string, unknown> = {}) => ({
  domain: 'reviews.corp.example',
  rule: 'exfil_query_length',
  path: '/reviews',
  query_chars: 290,
  url: 'https://reviews.corp.example/reviews?filter=abc',
  url_withheld: null,
  ...over,
})

const ALLOWED_HOSTS = ['redaction-allowed-hosts']

function invalidatedKeys(spy: ReturnType<typeof vi.spyOn>): unknown[] {
  return spy.mock.calls.map(call => (call[0] as { queryKey?: unknown } | undefined)?.queryKey)
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('Allow for this host from the details card', () => {
  it('refreshes the Settings allowed-hosts list once the host is saved', async () => {
    vi.spyOn(api, 'redactionAllowHost').mockResolvedValue({ ok: true, workspace: 'default' })
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
    const { getByTestId } = render(<MarkdownRenderer content={PH('reviews.corp.example')} blockedLinks={[link()]} slotKey="s1" />)
    fireEvent.click(getByTestId('blocked-link-inspect'))
    fireEvent.click(getByTestId('blocked-link-allow'))
    expect(invalidatedKeys(invalidate)).not.toContainEqual(ALLOWED_HOSTS)
    fireEvent.click(getByTestId('blocked-link-allow-confirmed'))
    await waitFor(() => expect(invalidatedKeys(invalidate)).toContainEqual(ALLOWED_HOSTS))
  })

  it('refreshes the Settings list again when the allow is undone', async () => {
    vi.spyOn(api, 'redactionAllowHost').mockResolvedValue({ ok: true, workspace: 'default' })
    vi.spyOn(api, 'redactionRevokeHost').mockResolvedValue({ ok: true, removed: true })
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
    const { getByTestId } = render(<MarkdownRenderer content={PH('reviews.corp.example')} blockedLinks={[link()]} slotKey="s1" />)
    fireEvent.click(getByTestId('blocked-link-inspect'))
    fireEvent.click(getByTestId('blocked-link-allow'))
    fireEvent.click(getByTestId('blocked-link-allow-confirmed'))
    await waitFor(() => expect(getByTestId('blocked-link-undo')).toBeTruthy())
    invalidate.mockClear()
    fireEvent.click(getByTestId('blocked-link-undo'))
    await waitFor(() => expect(invalidatedKeys(invalidate)).toContainEqual(ALLOWED_HOSTS))
  })

  it('does not refresh the list when saving the host fails', async () => {
    vi.spyOn(api, 'redactionAllowHost').mockRejectedValue(new Error('403'))
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
    const { getByTestId } = render(<MarkdownRenderer content={PH('reviews.corp.example')} blockedLinks={[link()]} slotKey="s1" />)
    fireEvent.click(getByTestId('blocked-link-inspect'))
    fireEvent.click(getByTestId('blocked-link-allow'))
    fireEvent.click(getByTestId('blocked-link-allow-confirmed'))
    await waitFor(() => expect(getByTestId('blocked-link-error')).toBeTruthy())
    expect(invalidatedKeys(invalidate)).not.toContainEqual(ALLOWED_HOSTS)
  })

  it('links the allowed state to the Settings list where the host is revoked', async () => {
    vi.spyOn(api, 'redactionAllowHost').mockResolvedValue({ ok: true, workspace: 'default' })
    const { getByTestId } = render(<MarkdownRenderer content={PH('reviews.corp.example')} blockedLinks={[link()]} slotKey="s1" />)
    fireEvent.click(getByTestId('blocked-link-inspect'))
    fireEvent.click(getByTestId('blocked-link-allow'))
    fireEvent.click(getByTestId('blocked-link-allow-confirmed'))
    await waitFor(() => expect(getByTestId('blocked-link-manage')).toBeTruthy())
    expect(getByTestId('blocked-link-manage').getAttribute('href')).toBe('/settings/security/redaction')
  })

  it('offers no Allow when the address was withheld for a credential', () => {
    const { getByTestId, queryByTestId } = render(
      <MarkdownRenderer content={PH('reviews.corp.example')} blockedLinks={[link({ url: null, url_withheld: 'credential', rule: 'exfil_query_length' })]} slotKey="s1" />,
    )
    fireEvent.click(getByTestId('blocked-link-inspect'))
    expect(queryByTestId('blocked-link-allow')).toBeNull()
  })

  it('offers no Allow without a slot to scope the host to', () => {
    const { getByTestId, queryByTestId } = render(<MarkdownRenderer content={PH('reviews.corp.example')} blockedLinks={[link()]} />)
    fireEvent.click(getByTestId('blocked-link-inspect'))
    expect(queryByTestId('blocked-link-allow')).toBeNull()
  })
})

describe('Allow for this host from the hover preview', () => {
  /** Keyboard focus on the marker shows its preview at once, with no hover delay. */
  function focusPreview(getByTestId: (id: string) => HTMLElement) {
    act(() => { getByTestId('blocked-link-inspect').focus() })
  }

  it('hands off to the card confirmation, which alone saves the host', async () => {
    const allowHost = vi.spyOn(api, 'redactionAllowHost').mockResolvedValue({ ok: true, workspace: 'default' })
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
    const { getByTestId, queryByTestId } = render(<MarkdownRenderer content={PH('reviews.corp.example')} blockedLinks={[link()]} slotKey="s1" />)
    focusPreview(getByTestId)
    fireEvent.click(getByTestId('blocked-link-preview-allow'))
    expect(queryByTestId('blocked-link-preview')).toBeNull()
    expect(allowHost).not.toHaveBeenCalled()
    expect(getByTestId('blocked-link-allow-confirm').textContent).toContain('Allow long queries on reviews.corp.example?')
    expect(document.activeElement?.textContent).toBe('Cancel')
    fireEvent.click(getByTestId('blocked-link-allow-confirmed'))
    await waitFor(() => expect(allowHost).toHaveBeenCalledWith('s1', 'reviews.corp.example'))
    await waitFor(() => expect(invalidatedKeys(invalidate)).toContainEqual(ALLOWED_HOSTS))
    expect(allowHost).toHaveBeenCalledTimes(1)
  })

  it('cancelling the handed-off confirmation saves nothing', () => {
    const allowHost = vi.spyOn(api, 'redactionAllowHost').mockResolvedValue({ ok: true, workspace: 'default' })
    const { getByTestId } = render(<MarkdownRenderer content={PH('reviews.corp.example')} blockedLinks={[link()]} slotKey="s1" />)
    focusPreview(getByTestId)
    fireEvent.click(getByTestId('blocked-link-preview-allow'))
    fireEvent.click(getByTestId('blocked-link-allow-confirm').querySelector('button')!)
    expect(allowHost).not.toHaveBeenCalled()
  })

  it('shows the saved state, Undo and the Settings link in the card it handed off to', async () => {
    vi.spyOn(api, 'redactionAllowHost').mockResolvedValue({ ok: true, workspace: 'default' })
    const { getByTestId } = render(<MarkdownRenderer content={PH('reviews.corp.example')} blockedLinks={[link()]} slotKey="s1" messageTs="1700000000.000100" />)
    focusPreview(getByTestId)
    fireEvent.click(getByTestId('blocked-link-preview-allow'))
    fireEvent.click(getByTestId('blocked-link-allow-confirmed'))
    await waitFor(() => expect(getByTestId('blocked-link-manage')).toBeTruthy())
    expect(getByTestId('blocked-link-entry').textContent).toContain('Allowed reviews.corp.example for this workspace')
    expect(getByTestId('blocked-link-undo')).toBeTruthy()
    expect(getByTestId('blocked-link-manage').getAttribute('href')).toBe('/settings/security/redaction')
  })

  it('offers no Allow when the address was withheld for a credential', () => {
    const { getByTestId, queryByTestId } = render(
      <MarkdownRenderer content={PH('reviews.corp.example')} blockedLinks={[link({ url: null, url_withheld: 'credential' })]} slotKey="s1" />,
    )
    focusPreview(getByTestId)
    expect(getByTestId('blocked-link-preview')).toBeTruthy()
    expect(queryByTestId('blocked-link-preview-allow')).toBeNull()
  })

  it('offers no Allow for a rule an allowed host does not relax', () => {
    const { getByTestId, queryByTestId } = render(
      <MarkdownRenderer content={PH('reviews.corp.example')} blockedLinks={[link({ rule: 'credential_in_url' })]} slotKey="s1" />,
    )
    focusPreview(getByTestId)
    expect(getByTestId('blocked-link-preview')).toBeTruthy()
    expect(queryByTestId('blocked-link-preview-allow')).toBeNull()
  })

  it('offers no Allow without an owner slot to save it against', () => {
    const { getByTestId, queryByTestId } = render(<MarkdownRenderer content={PH('reviews.corp.example')} blockedLinks={[link()]} />)
    focusPreview(getByTestId)
    expect(getByTestId('blocked-link-preview')).toBeTruthy()
    expect(queryByTestId('blocked-link-preview-allow')).toBeNull()
    // Without Allow the row holds Open once alone.
    const actions = within(getByTestId('redaction-preview-actions'))
    expect(actions.getAllByRole('button').map(b => b.textContent)).toEqual(['Open once'])
  })
})
