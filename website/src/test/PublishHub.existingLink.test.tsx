import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { PublishHub, deploySiteIdFor, findLiveSite } from '../components/PublishHub'
import { PREVIEW_ARTIFACT_DEPLOY } from '../utils/previewFlags'
import type { Artifact } from '../types'

/**
 * An artifact already on the public web keeps its link in the AWS account, not on
 * the artifact. The panel reads the deploy's site list when it opens and, for a
 * site under this artifact's id, shows the link and turns the public-web row into
 * an update of that same site. Anything short of a match leaves the panel as it
 * is with no lookup at all.
 */

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  )
}

const artifact: Artifact = {
  slug: 'launch-notes',
  name: 'Launch Notes',
  kind: 'widget',
  description: '',
  content: '',
  version: 1,
  created_at: '',
  updated_at: '',
  tags: [],
}

const publicWebRow = (over: Record<string, unknown> = {}) => ({
  id: 'deploy-web',
  label: 'Public Web',
  icon: 'Globe',
  kinds: [],
  configured: true,
  setupRoute: '/deploy',
  endpoint: '/api/deploy/deploy',
  ...over,
})

const LIVE_URL = 'https://d111111abcdef8.cloudfront.net/'

interface Route {
  appProviders?: Record<string, unknown>[]
  /** The body `/api/deploy/list` answers with, or an HTTP status to fail with. */
  sites?: Record<string, unknown> | number
}

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

/** Answers every request by URL, and records what was asked for. */
function routeFetch(spy: ReturnType<typeof vi.spyOn>, route: Route) {
  const calls: { url: string; body?: Record<string, unknown> }[] = []
  spy.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const body = typeof init?.body === 'string' ? JSON.parse(init.body) : undefined
    calls.push({ url, body })
    if (url.includes('/api/artifacts/publish-providers')) return json({ providers: [], kind: artifact.kind })
    if (url.includes('/api/publish-providers')) return json({ providers: route.appProviders ?? [publicWebRow()] })
    if (url.includes('/api/deploy/list')) {
      return typeof route.sites === 'number'
        ? json({ error: 'list failed' }, route.sites)
        : json(route.sites ?? { sites: [], configured: true })
    }
    if (url.includes('/api/deploy/deploy')) {
      return body?.confirm
        ? json({ url: LIVE_URL, site_id: artifact.slug })
        : json({ requires_confirm: true, message: 'Ready to publish', content_digest: 'd1', profile: body?.profile ?? 'default', region: 'us-west-2' })
    }
    return json({})
  })
  return calls
}

const listCalls = (calls: { url: string }[]) => calls.filter(c => c.url.includes('/api/deploy/list')).length

beforeEach(() => { localStorage.setItem(PREVIEW_ARTIFACT_DEPLOY, '1') })
afterEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
})

describe('deploySiteIdFor', () => {
  it('keeps an ordinary slug as it is', () => {
    expect(deploySiteIdFor('launch-notes')).toBe('launch-notes')
  })

  it('cuts a long slug to the 64 characters the deploy stores', () => {
    const slug = 'a'.repeat(70)
    expect(deploySiteIdFor(slug)).toBe('a'.repeat(64))
  })

  it('normalizes case and characters the deploy does not keep', () => {
    expect(deploySiteIdFor(' My Notes.v2_ ')).toBe('my-notes-v2')
  })
})

describe('findLiveSite', () => {
  const site = { site_id: 'launch-notes', url: LIVE_URL, status: 'Deployed', profile: 'default' }

  it('finds the site listed under the artifact id', () => {
    expect(findLiveSite({ sites: [{ site_id: 'other' }, site], configured: true }, 'launch-notes')).toEqual(site)
  })

  it('answers null for no listing, an unconfigured deploy, or no matching site', () => {
    expect(findLiveSite(undefined, 'launch-notes')).toBeNull()
    expect(findLiveSite({ sites: [site], configured: false }, 'launch-notes')).toBeNull()
    expect(findLiveSite({ sites: [{ site_id: 'launch-notes-2' }], configured: true }, 'launch-notes')).toBeNull()
  })
})

describe('PublishHub — an existing public link', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>
  beforeEach(() => { fetchSpy = vi.spyOn(globalThis, 'fetch') })

  it('shows the live link and an update in place of the public-web row', async () => {
    routeFetch(fetchSpy, {
      sites: { sites: [{ site_id: 'launch-notes', url: LIVE_URL, status: 'Deployed', profile: 'team' }], configured: true },
    })
    render(<PublishHub artifact={artifact} />, { wrapper })

    expect(await screen.findByText('Already live on the public web')).toBeDefined()
    expect(screen.getByText(LIVE_URL)).toBeDefined()
    const open = screen.getByRole('link', { name: /Open live page/ })
    expect(open.getAttribute('href')).toBe(LIVE_URL)
    expect(open.getAttribute('target')).toBe('_blank')
    expect(screen.getByRole('button', { name: /Update \(same URL\)/ })).toBeDefined()
    // The update stands in for the row, so the destination is not offered twice.
    expect(screen.queryByText('Public Web')).toBeNull()
  })

  it('updates the live site through the account that holds it', async () => {
    const calls = routeFetch(fetchSpy, {
      sites: { sites: [{ site_id: 'launch-notes', url: LIVE_URL, status: 'Deployed', profile: 'team' }], configured: true },
    })
    render(<PublishHub artifact={artifact} />, { wrapper })

    fireEvent.click(await screen.findByRole('button', { name: /Update \(same URL\)/ }))
    expect(screen.getByText('This replaces the live page. Its URL stays the same.')).toBeDefined()

    const publish = screen.getAllByRole('button')
      .find(b => b.textContent?.includes('Publish') && !b.textContent?.includes('Close'))
    fireEvent.click(publish!)
    await screen.findByText(/Ready to publish/)

    fireEvent.click(screen.getByText('Confirm & Publish'))
    fireEvent.click(await screen.findByRole('button', { name: 'I understand, publish publicly' }))
    await screen.findByText('Published!')

    const deploys = calls.filter(c => c.url.includes('/api/deploy/deploy'))
    expect(deploys).toHaveLength(2)
    for (const d of deploys) {
      expect(d.body?.site_id).toBe('launch-notes')
      expect(d.body?.profile).toBe('team')
    }
    expect(deploys[1].body?.expected_profile).toBe('team')
  })

  it('reads the site list again after a publish, so the next open shows the new link', async () => {
    const calls = routeFetch(fetchSpy, {})
    render(<PublishHub artifact={artifact} />, { wrapper })

    fireEvent.click(await screen.findByText('Public Web'))
    await waitFor(() => expect(listCalls(calls)).toBe(1))
    const publish = screen.getAllByRole('button')
      .find(b => b.textContent?.includes('Publish') && !b.textContent?.includes('Close'))
    fireEvent.click(publish!)
    await screen.findByText(/Ready to publish/)
    fireEvent.click(screen.getByText('Confirm & Publish'))
    fireEvent.click(await screen.findByRole('button', { name: 'I understand, publish publicly' }))
    await screen.findByText('Published!')

    await waitFor(() => expect(listCalls(calls)).toBe(2))
    // A first publish goes out with no profile: the backend picks its default.
    const deploys = calls.filter(c => c.url.includes('/api/deploy/deploy'))
    expect(deploys.every(d => d.body?.profile === undefined)).toBe(true)
  })

  it('keeps the other destinations listed beside the live link', async () => {
    routeFetch(fetchSpy, {
      appProviders: [
        publicWebRow(),
        { id: 'drive', label: 'Team Drive', icon: 'Upload', kinds: [], configured: true, setupRoute: '', endpoint: '/api/apps/drive/publish' },
      ],
      sites: { sites: [{ site_id: 'launch-notes', url: LIVE_URL }], configured: true },
    })
    render(<PublishHub artifact={artifact} />, { wrapper })

    await screen.findByText('Already live on the public web')
    expect(screen.getByText('Team Drive')).toBeDefined()
  })

  it('shows the site but no link when its URL is not http(s)', async () => {
    routeFetch(fetchSpy, {
      sites: { sites: [{ site_id: 'launch-notes', url: 'javascript:alert(1)' }], configured: true },
    })
    render(<PublishHub artifact={artifact} />, { wrapper })

    await screen.findByText('Already live on the public web')
    expect(screen.queryByRole('link', { name: /Open live page/ })).toBeNull()
    expect(screen.queryByText('javascript:alert(1)')).toBeNull()
    expect(screen.getByRole('button', { name: /Update \(same URL\)/ })).toBeDefined()
  })

  it.each([
    ['no site under this artifact', { sites: [{ site_id: 'someone-else', url: LIVE_URL }], configured: true }],
    ['an unconfigured deploy', { sites: [], configured: false }],
    ['a failed listing', 500],
  ] as const)('offers the destination list as it is for %s', async (_label, sites) => {
    const calls = routeFetch(fetchSpy, { sites: sites as Route['sites'] })
    render(<PublishHub artifact={artifact} />, { wrapper })

    await screen.findByText('Public Web')
    await waitFor(() => expect(listCalls(calls)).toBe(1))
    await waitFor(() => expect(screen.queryByText(/Checking for an existing public link/)).toBeNull())
    expect(screen.queryByText('Already live on the public web')).toBeNull()
    expect(screen.queryByRole('button', { name: /Update \(same URL\)/ })).toBeNull()
  })

  it('does not look anything up while the public-web destination is not offered', async () => {
    localStorage.removeItem(PREVIEW_ARTIFACT_DEPLOY)
    const calls = routeFetch(fetchSpy, {
      appProviders: [
        publicWebRow(),
        { id: 'drive', label: 'Team Drive', icon: 'Upload', kinds: [], configured: true, setupRoute: '', endpoint: '/api/apps/drive/publish' },
      ],
    })
    render(<PublishHub artifact={artifact} />, { wrapper })

    await screen.findByText('Team Drive')
    expect(screen.queryByText('Public Web')).toBeNull()
    expect(listCalls(calls)).toBe(0)
  })

  it('does not look anything up while the public-web destination needs setup', async () => {
    const calls = routeFetch(fetchSpy, { appProviders: [publicWebRow({ configured: false })] })
    render(<PublishHub artifact={artifact} />, { wrapper })

    await screen.findByText('Public Web')
    expect(screen.getByText('Setup required')).toBeDefined()
    expect(listCalls(calls)).toBe(0)
  })
})
