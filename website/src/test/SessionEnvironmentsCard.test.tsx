import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { http, HttpResponse } from 'msw'
import { server } from '../../integration/mocks/server'
import { SessionEnvironmentsCard } from '../pages/settings/SessionEnvironmentsCard'
import { FeaturePreviewsSection } from '../pages/settings/FeaturePreviewsSection'
import { PREVIEW_SESSION_ENVIRONMENTS } from '../utils/previewFlags'
import { settingEntryOffered } from '../components/commandPalette/settingsSearchCore'
import { SETTINGS_REGISTRY } from '../components/commandPalette/settingsRegistry.gen'

vi.mock('../pages/settings/DecisionsCard', () => ({ DecisionsCard: () => null }))

const profile = { id: 'dev', name: 'Development', template: 'python-dev', workdir: '/home/coder/repo' }
const fargate = { id: 'aws-dev', name: 'AWS development', aws_profile: 'development', region: 'us-east-1', cluster: 'dev', task_definition: 'session-dev:1', subnets: ['subnet-12345678'], security_groups: ['sg-12345678'], assign_public_ip: false, workdir: '/workspace' }
let stored: { session_environments?: { coder_url: string; profiles: typeof profile[]; fargate_profiles?: typeof fargate[] } }
let reads: number
let writes: unknown[]
let qc: QueryClient

function mount(enabled = true) {
  if (enabled) localStorage.setItem(PREVIEW_SESSION_ENVIRONMENTS, '1')
  qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={qc}><MemoryRouter><SessionEnvironmentsCard enabled={enabled} /></MemoryRouter></QueryClientProvider>)
}

beforeEach(() => {
  localStorage.clear()
  reads = 0
  writes = []
  stored = { session_environments: { coder_url: 'https://coder.example.test', profiles: [{ ...profile }], fargate_profiles: [] } }
  server.use(
    http.get('*/api/config/kirocrew', () => { reads++; return HttpResponse.json(stored) }),
    http.patch('*/api/config/kirocrew', async ({ request }) => {
      const body = await request.json() as { path: string; value: NonNullable<typeof stored.session_environments> }
      writes.push(body)
      stored = { session_environments: body.value }
      return HttpResponse.json(stored)
    }),
  )
})
afterEach(() => { cleanup(); qc?.clear(); localStorage.clear() })

const url = () => screen.getByLabelText('Coder URL')
/** The section's own Save (an unsaved Sprites profile adds a second, same action). */
const save = () => screen.getAllByRole('button', { name: 'Save configuration' }).at(-1)!
const toggle = () => screen.getByRole('switch', { name: 'Session Environments' })
/** Saved profiles start collapsed; open one by its header (name, then subtitle). */
const expand = (title: string) => fireEvent.click(screen.getByRole('button', { name: new RegExp(`^${title}`) }))
const expandAll = () => screen.getAllByRole('button', { expanded: false }).forEach(header => fireEvent.click(header))

describe('Session Environments configuration preview', () => {
  it('indexes the opt-in but hides the URL search result until enabled', () => {
    const governance = { decisionsEnabled: true, tipsEnabled: true }
    const gate = SETTINGS_REGISTRY.find(entry => entry.id === 'developer.session-environments')!
    const connection = SETTINGS_REGISTRY.find(entry => entry.configKey === 'session_environments.coder_url')!
    expect(gate).toBeDefined()
    expect(connection).toBeDefined()
    expect(connection.tab).toBe('session-environments')
    expect(settingEntryOffered(gate, governance)).toBe(true)
    expect(settingEntryOffered(connection, governance)).toBe(false)
    localStorage.setItem(PREVIEW_SESSION_ENVIRONMENTS, '1')
    expect(settingEntryOffered(connection, governance)).toBe(true)
  })

  it('keeps only the opt-in in Developer, without reading or writing configuration', () => {
    qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={qc}><MemoryRouter><FeaturePreviewsSection /></MemoryRouter></QueryClientProvider>)
    expect(toggle()).not.toBeChecked()
    expect(screen.queryByRole('textbox', { name: 'Coder URL' })).not.toBeInTheDocument()
    fireEvent.click(toggle())
    expect(toggle()).toBeChecked()
    expect(screen.queryByRole('textbox', { name: 'Coder URL' })).not.toBeInTheDocument()
    expect(reads).toBe(0)
    expect(writes).toEqual([])
    expect(localStorage.getItem(PREVIEW_SESSION_ENVIRONMENTS)).toBe('1')
  })

  it('does not load the configuration when disabled', () => {
    mount(false)
    expect(reads).toBe(0)
    expect(screen.queryByLabelText('Coder URL')).not.toBeInTheDocument()
  })

  it('writes the complete section only on Save and reloads saved profiles', async () => {
    const first = mount()
    await screen.findByLabelText('Coder URL')
    fireEvent.change(url(), { target: { value: 'https://new.example.test' } })
    expand('Development')
    fireEvent.change(screen.getByLabelText('Coder template'), { target: { value: 'node-dev' } })
    expect(writes).toEqual([])
    fireEvent.click(save())
    expect(await screen.findByText('Configuration saved. No compute was started.')).toBeVisible()
    expect(writes).toEqual([{ path: 'session_environments', value: {
      coder_url: 'https://new.example.test', profiles: [{ ...profile, template: 'node-dev' }], fargate_profiles: [],
    } }])
    expect(save()).toBeDisabled()
    first.unmount(); qc.clear()
    mount()
    expect(await screen.findByLabelText('Coder URL')).toHaveValue('https://new.example.test')
    expand('Development')
    expect(screen.getByLabelText('Coder template')).toHaveValue('node-dev')
  })

  it('sets ready workspaces within 0-5', async () => {
    stored.session_environments!.profiles = [{ ...profile, hot_pool: 0 } as typeof profile]
    mount()
    await screen.findByLabelText('Coder URL')
    expand('Development')
    const increase = screen.getByRole('button', { name: 'Increase' })
    for (let i = 0; i < 7; i++) fireEvent.click(increase)
    fireEvent.click(screen.getByRole('button', { name: 'Decrease' }))
    fireEvent.click(save())
    // A saved pool starts billed compute, so the confirmation must not deny it.
    await screen.findByText(
      'Configuration saved. Ready environments are starting in the background and are billed while they wait.',
    )
    expect(screen.queryByText('Configuration saved. No compute was started.')).not.toBeInTheDocument()
    expect(writes).toEqual([{ path: 'session_environments', value: {
      coder_url: 'https://coder.example.test', profiles: [{ ...profile, hot_pool: 4 }], fargate_profiles: [],
    } }])
  })

  it('keeps archived session environments for 0-7 days and saves it with the section', async () => {
    stored.session_environments = { ...stored.session_environments!, archived_retention_days: 6 } as typeof stored.session_environments
    mount()
    await screen.findByLabelText('Coder URL')
    // Section level, so it is visible without opening a profile.
    const group = screen.getByText('Days to keep archived session environments').closest('div')!.parentElement!
    expect(group).toHaveTextContent('6')
    const increase = screen.getByRole('button', { name: 'Increase' })
    for (let i = 0; i < 4; i++) fireEvent.click(increase)
    fireEvent.click(save())
    await screen.findByText('Configuration saved. No compute was started.')
    expect(writes).toEqual([{ path: 'session_environments', value: {
      coder_url: 'https://coder.example.test', profiles: [{ ...profile }], fargate_profiles: [], archived_retention_days: 7,
    } }])
  })

  it('hides the retention control for a gateway that does not report it', async () => {
    mount()
    await screen.findByLabelText('Coder URL')
    expect(screen.queryByText('Days to keep archived session environments')).not.toBeInTheDocument()
  })

  it('shows the live ready count on a pooled profile header, polling only when pooled', async () => {
    let poolReads = 0
    server.use(http.get('*/api/session-environments/pool', () => {
      poolReads++
      return HttpResponse.json({ profiles: { dev: { target: 2, ready: 1, warming: 1 } } })
    }))
    stored.session_environments!.profiles = [{ ...profile, hot_pool: 2 } as typeof profile]
    const first = mount()
    expect(await screen.findByTestId('profile-pool-status')).toHaveTextContent('Ready: 1 of 2, warming up: 1')
    first.unmount(); qc.clear()
    poolReads = 0
    stored.session_environments!.profiles = [{ ...profile, hot_pool: 0 } as typeof profile]
    mount()
    await screen.findByLabelText('Coder URL')
    expect(screen.queryByTestId('profile-pool-status')).toBeNull()
    expect(poolReads).toBe(0)
  })

  it('omits the ready-workspace control when the gateway does not report it', async () => {
    mount()
    await screen.findByLabelText('Coder URL')
    expand('Development')
    expect(screen.queryByText('Ready environments')).not.toBeInTheDocument()
  })

  it('keeps edits across refetch and discards explicitly', async () => {
    mount()
    await screen.findByLabelText('Coder URL')
    fireEvent.change(url(), { target: { value: 'https://draft.example.test' } })
    act(() => qc.setQueryData(['kirocrewConfig'], { session_environments: { coder_url: 'https://other.example.test', profiles: [] } }))
    expect(url()).toHaveValue('https://draft.example.test')
    fireEvent.click(screen.getByRole('button', { name: 'Discard changes' }))
    expect(url()).toHaveValue('https://other.example.test')
    expect(writes).toEqual([])
  })

  it('adds unique profile drafts and removes them without provisioning', async () => {
    stored.session_environments!.profiles.push({ ...profile, id: 'profile-1' })
    mount()
    await screen.findByLabelText('Coder URL')
    fireEvent.click(screen.getByRole('button', { name: 'Add profile' }))
    expandAll()
    expect(screen.getAllByLabelText('Profile ID').map(input => (input as HTMLInputElement).value)).toEqual(['dev', 'profile-1', 'profile-2'])
    fireEvent.click(screen.getAllByRole('button', { name: 'Remove profile' })[2])
    expect(screen.getAllByLabelText('Profile ID')).toHaveLength(2)
    expect(writes).toEqual([])
  })

  it('retains a rejected draft and shows the backend validation detail', async () => {
    server.use(http.patch('*/api/config/kirocrew', () => HttpResponse.json({ error: 'profiles[0].id is a duplicate' }, { status: 400 })))
    mount()
    await screen.findByLabelText('Coder URL')
    fireEvent.change(url(), { target: { value: 'https://draft.example.test' } })
    fireEvent.click(save())
    expect(await screen.findByText('profiles[0].id is a duplicate')).toBeVisible()
    expect(url()).toHaveValue('https://draft.example.test')
    expect(save()).toBeEnabled()
    expect(screen.queryByText('Configuration saved. No compute was started.')).not.toBeInTheDocument()
  })

  it('does not fabricate settings support on an older gateway', async () => {
    stored = {}
    mount()
    expect(await screen.findByText(/This gateway does not support Session Environments configuration/)).toBeVisible()
    expect(screen.queryByLabelText('Coder URL')).not.toBeInTheDocument()
    expect(writes).toEqual([])
  })

  it('retries loading after a failed configuration read', async () => {
    server.use(http.get('*/api/config/kirocrew', () => HttpResponse.json({ error: 'offline' }, { status: 500 })))
    mount()
    expect(await screen.findByText('Could not load environment settings.')).toBeVisible()
    server.use(http.get('*/api/config/kirocrew', () => HttpResponse.json(stored)))
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await screen.findByLabelText('Coder URL')
    expect(save()).toBeDisabled()
  })

  it('freezes editing during a save and does not submit twice', async () => {
    let finish!: () => void
    const pending = new Promise<void>(resolve => { finish = resolve })
    server.use(http.patch('*/api/config/kirocrew', async ({ request }) => {
      writes.push(await request.json())
      await pending
      return HttpResponse.json(stored)
    }))
    mount()
    await screen.findByLabelText('Coder URL')
    fireEvent.change(url(), { target: { value: 'https://draft.example.test' } })
    fireEvent.click(save())
    await waitFor(() => expect(url()).toBeDisabled())
    expect(save()).toBeDisabled()
    await act(async () => finish())
    await screen.findByText('Configuration saved. No compute was started.')
    expect(writes).toHaveLength(1)
  })
})


describe('Coder and Fargate setup and saved profile management', () => {
  it('shows provider-specific setup without claiming a live connection', async () => {
    mount()
    await screen.findByLabelText('Coder URL')
    // jsdom keeps closed <details> content in the tree, so assert the
    // disclosure state itself: instructions start collapsed.
    const instructions = screen.getByText('Setup instructions', { exact: true }).closest('details')!
    expect(instructions.open).toBe(false)
    instructions.open = true
    expect(screen.getByRole('link', { name: 'Open provider setup documentation' })).toHaveAttribute('href', 'https://coder.com/docs/admin/setup')
    fireEvent.click(screen.getByRole('radio', { name: 'AWS Fargate' }))
    expect(screen.getByRole('radio', { name: 'AWS Fargate' })).toBeChecked()
    expect(screen.getByRole('radio', { name: 'Coder', exact: true })).not.toBeChecked()
    expect(screen.getByRole('link', { name: 'Open provider setup documentation' })).toHaveAttribute('href', 'https://docs.aws.amazon.com/AmazonECS/latest/developerguide/getting-started-fargate.html')
    expect(screen.getByText(/A session runs on a profile once you choose it in the chat composer/)).toBeVisible()
    expect(writes).toEqual([])
  })

  it('preserves both provider drafts while switching and saves them together', async () => {
    stored.session_environments!.fargate_profiles = [{ ...fargate }]
    mount()
    await screen.findByLabelText('Coder URL')
    fireEvent.change(url(), { target: { value: 'https://draft.example.test' } })
    fireEvent.click(screen.getByRole('radio', { name: 'AWS Fargate' }))
    expand('AWS development')
    fireEvent.change(screen.getByRole('textbox', { name: 'ECS cluster name or ARN' }), { target: { value: 'new-cluster' } })
    fireEvent.click(screen.getByRole('radio', { name: 'Coder', exact: true }))
    expect(url()).toHaveValue('https://draft.example.test')
    fireEvent.click(save())
    await screen.findByText('Configuration saved. No compute was started.')
    expect(stored.session_environments!.coder_url).toBe('https://draft.example.test')
    expect(stored.session_environments!.fargate_profiles![0].cluster).toBe('new-cluster')
    expect(writes).toHaveLength(1)
  })

  it('adds private-by-default Fargate profiles, persists networks and removes only configuration', async () => {
    mount()
    await screen.findByLabelText('Coder URL')
    fireEvent.click(screen.getByRole('radio', { name: 'AWS Fargate' }))
    fireEvent.click(screen.getByRole('button', { name: 'Add profile' }))
    expect(screen.getByRole('switch', { name: 'Assign public IP' })).not.toBeChecked()
    const fields = {
      'Profile name': 'AWS development', 'AWS CLI profile': 'development', 'AWS region': 'us-east-1',
      'ECS cluster name or ARN': 'dev', 'Task definition name:revision or ARN': 'session-dev:1',
      'Subnet IDs, comma-separated': 'subnet-12345678, subnet-abcdef12',
      'Security group IDs, comma-separated': 'sg-12345678',
    }
    for (const [name, value] of Object.entries(fields)) fireEvent.change(screen.getByRole('textbox', { name, exact: true }), { target: { value } })
    expect(writes).toEqual([])
    fireEvent.click(save())
    await screen.findByText('Configuration saved. No compute was started.')
    expect(stored.session_environments!.fargate_profiles![0]).toEqual({ ...fargate, id: 'fargate-1', subnets: ['subnet-12345678', 'subnet-abcdef12'] })
    fireEvent.click(screen.getByRole('button', { name: 'Remove profile' }))
    expect(stored.session_environments!.fargate_profiles).toHaveLength(1)
    fireEvent.click(save())
    await waitFor(() => expect(stored.session_environments!.fargate_profiles).toEqual([]))
    expect(stored.session_environments!.profiles).toEqual([profile])
  })

  it('refuses to offer Fargate saves on an older Coder-only backend', async () => {
    delete stored.session_environments!.fargate_profiles
    mount()
    await screen.findByLabelText('Coder URL')
    fireEvent.click(screen.getByRole('radio', { name: 'AWS Fargate' }))
    expect(screen.getByText(/This gateway does not support Session Environments configuration/)).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Add profile' })).not.toBeInTheDocument()
  })
})


it('collapses saved profiles, opens new ones, and keeps edits across collapse', async () => {
  mount()
  await screen.findByLabelText('Coder URL')
  const header = screen.getByRole('button', { name: /^Development/ })
  expect(header).toHaveAttribute('aria-expanded', 'false')
  expect(header).toHaveTextContent('python-dev')
  expect(screen.queryByLabelText('Coder template')).not.toBeInTheDocument()
  fireEvent.click(header)
  expect(header).toHaveAttribute('aria-expanded', 'true')
  fireEvent.change(screen.getByLabelText('Coder template'), { target: { value: 'node-dev' } })
  fireEvent.click(header)
  await waitFor(() => expect(screen.queryByLabelText('Coder template')).not.toBeInTheDocument())
  expect(header).toHaveTextContent('node-dev')
  fireEvent.click(header)
  expect(screen.getByLabelText('Coder template')).toHaveValue('node-dev')
  fireEvent.click(screen.getByRole('button', { name: 'Add profile' }))
  expect(screen.getByRole('button', { name: /^profile-1/ })).toHaveAttribute('aria-expanded', 'true')
  expect(writes).toEqual([])
})

it('keeps configure, verify and manage on one page without treating a save as readiness', async () => {
  mount()
  await screen.findByLabelText('Coder URL')
  expect(screen.getByRole('radio', { name: 'Coder', exact: true })).toBeChecked()
  expect(screen.getByRole('radio', { name: 'AWS Fargate' })).not.toBeChecked()
  // Brand marks are decorative: each choice keeps its text label as its name.
  const fargateChoice = screen.getByRole('radio', { name: 'AWS Fargate' }).closest('label')!
  expect(fargateChoice.querySelector('img[alt=""][aria-hidden="true"]')).not.toBeNull()
  expect(screen.getByRole('radio', { name: 'Coder', exact: true }).closest('label')!.querySelector('span[aria-hidden="true"]')).not.toBeNull()
  expect(screen.queryByRole('tablist')).not.toBeInTheDocument()
  for (const name of ['1. Connect and configure', '2. Verify readiness', '3. Manage']) {
    expect(screen.getByRole('region', { name })).toBeVisible()
  }
  fireEvent.change(url(), { target: { value: 'https://changed.example.test' } })
  // An unsaved draft cannot be checked: the check reads what the gateway saved.
  expect(screen.getByRole('button', { name: 'Check configuration' })).toBeDisabled()
  expect(screen.getByText('Save your changes to check them.')).toBeVisible()
  fireEvent.click(save())
  await screen.findByText('Configuration saved. No compute was started.')
  expect(screen.getByRole('button', { name: 'Check configuration' })).toBeEnabled()
  expect(screen.getByRole('button', { name: 'Check configuration' })).toHaveAttribute('aria-describedby')
  // The controls that did nothing are gone.
  for (const name of ['Start session', 'Manage resources']) {
    expect(screen.queryByRole('button', { name })).not.toBeInTheDocument()
  }
  expect(writes).toHaveLength(1)
})

describe('Session Environments readiness and workspaces', () => {
  it('runs the read-only check and reports Coder, each template and each git host', async () => {
    let checks = 0
    server.use(http.post('*/api/session-environments/check', () => {
      checks++
      return HttpResponse.json({
        coder: { ok: true, owner: 'devuser' },
        profiles: [{ id: 'dev', ok: false, error: 'Coder template is unavailable' }],
        git: { 'github.com': 'signed_in', 'dev.azure.com': 'not_signed_in', 'bitbucket.org': 'unchecked' },
      })
    }))
    mount()
    await screen.findByLabelText('Coder URL')
    fireEvent.click(screen.getByRole('button', { name: 'Check configuration' }))
    const results = await screen.findByRole('list', { name: 'Check results' })
    expect(checks).toBe(1)
    expect(results).toHaveTextContent('Signed in to Coder as devuser')
    expect(results).toHaveTextContent('Development: Coder template is unavailable')
    expect(results).toHaveTextContent('GitHub: signed in on the gateway')
    expect(results).toHaveTextContent('Azure DevOps: not signed in on the gateway')
    expect(results).toHaveTextContent("Bitbucket: uses the gateway's git credential helper, not checked")
    expect(writes).toEqual([])
  })

  it('reports only the selected provider in the check, even with a Coder URL still saved', async () => {
    const fly = { id: 'fly', name: 'Fly.io Sprites', token_secret: 'SPRITES_TOKEN', workdir: '/w' }
    stored = { session_environments: { coder_url: 'https://coder.example.test', profiles: [{ ...profile }], fargate_profiles: [], sprites_profiles: [fly] } as never }
    server.use(http.post('*/api/session-environments/check', () => HttpResponse.json({
      coder: { ok: true, owner: 'devuser' },
      profiles: [{ id: 'dev', ok: true }],
      fargate: [],
      sprites: [{ id: 'fly', ok: true }],
      git: { 'github.com': 'signed_in' },
    })))
    mount()
    await screen.findByLabelText('Coder URL')
    fireEvent.click(screen.getByRole('radio', { name: 'Fly.io Sprites' }))
    expect(screen.getByText(/Sprites API token is accepted/)).toBeVisible()
    expect(screen.queryByText(/signed in to Coder, that each profile/)).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Check configuration' }))
    const results = await screen.findByRole('list', { name: 'Check results' })
    expect(results).toHaveTextContent('Fly.io Sprites: Sprites API token accepted')
    expect(results).toHaveTextContent('GitHub: signed in on the gateway')
    expect(results).not.toHaveTextContent('Coder')
    expect(results).not.toHaveTextContent('template')
    // Switching back to Coder shows the Coder rows from the same result.
    fireEvent.click(screen.getByRole('radio', { name: 'Coder', exact: true }))
    expect(results).toHaveTextContent('Signed in to Coder as devuser')
    expect(results).not.toHaveTextContent('Sprites API token')
  })

  it('reports a check that could not run through the error notice', async () => {
    server.use(http.post('*/api/session-environments/check', () => HttpResponse.json({ error: 'x' }, { status: 500 })))
    mount()
    await screen.findByLabelText('Coder URL')
    fireEvent.click(screen.getByRole('button', { name: 'Check configuration' }))
    expect(await screen.findByText('The check could not run.')).toBeVisible()
  })

  it('lists the workspaces Kiro Crew owns, with when an archived one is deleted', async () => {
    server.use(http.get('*/api/session-environments/workspaces', () => HttpResponse.json({
      workspaces: [
        { workspace: 'crewenv-devuser-aaaa', kind: 'session', slot: 'chat-1', state: 'running', template: 't', created_at: '2026-10-09T10:00:00Z', deletion_due_at: '' },
        { workspace: 'crewenv-devuser-bbbb', kind: 'session', slot: 'chat-2', state: 'running', template: 't', created_at: '2026-10-09T11:00:00Z', deletion_due_at: '2026-10-16T11:00:00Z' },
        { workspace: 'crewenv-devuser-cccc', kind: 'pool', slot: '', state: 'ready', template: 't', created_at: '2026-10-09T12:00:00Z', deletion_due_at: '' },
      ],
    })))
    mount()
    const list = await screen.findByRole('list', { name: 'Session environments' })
    const rows = list.querySelectorAll('li')
    expect(rows).toHaveLength(3)
    expect(rows[0]).toHaveTextContent('crewenv-devuser-aaaa')
    expect(rows[0]).toHaveTextContent('In use by a session')
    expect(rows[0]).not.toHaveTextContent('Will be deleted')
    expect(rows[1]).toHaveTextContent(/Will be deleted/)
    expect(rows[2]).toHaveTextContent('Ready, waiting for a session')
    // The Coder deployment is one click away, opened safely in a new tab.
    const link = screen.getByRole('link', { name: 'Open Coder' })
    expect(link).toHaveAttribute('href', 'https://coder.example.test')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
  })

  it('checks a Fargate-only setup without reporting a Coder line', async () => {
    stored = { session_environments: { coder_url: '', profiles: [], fargate_profiles: [{ ...fargate }, { ...fargate, id: 'aws-two', name: '' }] } }
    server.use(http.post('*/api/session-environments/check', () => HttpResponse.json({
      coder: { ok: false, error: 'no Coder URL is saved' },
      profiles: [],
      fargate: [
        { id: 'aws-dev', ok: true },
        { id: 'aws-two', ok: false, error: 'the AWS Session Manager plugin is not installed on the gateway' },
      ],
      git: {},
    })))
    mount()
    await screen.findByLabelText('Coder URL')
    const check = screen.getByRole('button', { name: 'Check configuration' })
    expect(check).toBeEnabled()
    fireEvent.click(check)
    const results = await screen.findByRole('list', { name: 'Check results' })
    expect(results).not.toHaveTextContent('Coder')
    expect(results).toHaveTextContent('AWS development: task definition session-dev:1 is ready')
    // An unnamed profile falls back to its id.
    expect(results).toHaveTextContent('aws-two: the AWS Session Manager plugin is not installed on the gateway')
  })

  it('adds a Sprites profile that names a token secret, never a token, and checks it', async () => {
    stored = { session_environments: { coder_url: '', profiles: [], fargate_profiles: [], sprites_profiles: [] } as never }
    server.use(http.post('*/api/session-environments/check', () => HttpResponse.json({
      coder: { ok: false }, profiles: [], fargate: [], git: {},
      sprites: [{ id: 'sprites-1', ok: true }],
    })))
    mount()
    await screen.findByLabelText('Coder URL')
    // The Fly.io mark is decorative; the text label stays the choice's name.
    expect(screen.getByRole('radio', { name: 'Fly.io Sprites' }).closest('label')!.querySelector('span[aria-hidden="true"]')).not.toBeNull()
    fireEvent.click(screen.getByRole('radio', { name: 'Fly.io Sprites' }))
    fireEvent.click(screen.getAllByRole('button', { name: 'Add profile' }).find(b => !b.closest('[hidden]'))!)
    expect(screen.getByLabelText('API token secret name')).toHaveValue('SPRITES_TOKEN')
    // Until the configuration is saved, the profile says it is not in the composer yet.
    expect(screen.getByTestId('sprites-profile-unsaved')).toHaveTextContent('This profile is not saved yet')
    fireEvent.click(save())
    await waitFor(() => expect(writes).toHaveLength(1))
    await waitFor(() => expect(screen.queryByTestId('sprites-profile-unsaved')).toBeNull())
    expect((writes[0] as { value: { sprites_profiles: unknown[] } }).value.sprites_profiles).toEqual([
      { id: 'sprites-1', name: 'Fly.io Sprites', token_secret: 'SPRITES_TOKEN', workdir: '/home/sprite/workspace' },
    ])
    expect(JSON.stringify(writes[0])).not.toMatch(/"token"/)
    const check = await screen.findByRole('button', { name: 'Check configuration' })
    await waitFor(() => expect(check).toBeEnabled())
    fireEvent.click(check)
    const results = await screen.findByRole('list', { name: 'Check results' })
    expect(results).toHaveTextContent('Fly.io Sprites: Sprites API token accepted')
  })

  it('saves a Sprites token to the vault from a masked field and never shows it back', async () => {
    const good = { id: 'fly', name: 'Fly', token_secret: 'SPRITES_TOKEN', workdir: '/home/sprite/workspace' }
    stored = { session_environments: { coder_url: '', profiles: [], fargate_profiles: [], sprites_profiles: [good] } as never }
    const vault: { name: string; value: string }[] = []
    server.use(
      http.get('*/api/secrets', () => HttpResponse.json({ names: vault.map(v => v.name), managed: [] })),
      http.post('*/api/secrets', async ({ request }) => {
        vault.push(await request.json() as { name: string; value: string })
        return HttpResponse.json({ ok: true })
      }),
      http.delete('*/api/secrets/:name', ({ params }) => {
        vault.splice(vault.findIndex(v => v.name === params.name), 1)
        return HttpResponse.json({ ok: true })
      }),
    )
    mount()
    await screen.findByLabelText('Coder URL')
    fireEvent.click(screen.getByRole('radio', { name: 'Fly.io Sprites' }))
    expand('Fly')
    const field = screen.getByLabelText('API token')
    expect(field).toHaveAttribute('type', 'password')
    fireEvent.change(field, { target: { value: 'my-org/t1/secret-value' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save token' }))
    await waitFor(() => expect(vault).toEqual([{ name: 'SPRITES_TOKEN', value: 'my-org/t1/secret-value' }]))
    await waitFor(() => expect(field).toHaveValue(''))
    expect(await screen.findByText(/A token is saved in the gateway vault as SPRITES_TOKEN\./)).toBeVisible()
    // The token never rides on the profile draft or a config write.
    expect(writes).toEqual([])
    expect(document.body.innerHTML).not.toContain('secret-value')
    // Removing a profile leaves the vault alone; the token is removed explicitly.
    fireEvent.click(screen.getByRole('button', { name: 'Remove token' }))
    await waitFor(() => expect(vault).toEqual([]))
    expect(await screen.findByText(/No token is saved as SPRITES_TOKEN yet/)).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Remove token' })).toBeNull()
  })

  it("deletes a removed profile's token, but not one another profile still uses", async () => {
    const a = { id: 'fly-a', name: 'Fly A', token_secret: 'TOKEN_A', workdir: '/w' }
    const b = { id: 'fly-b', name: 'Fly B', token_secret: 'SHARED', workdir: '/w' }
    const c = { id: 'fly-c', name: 'Fly C', token_secret: 'SHARED', workdir: '/w' }
    stored = { session_environments: { coder_url: '', profiles: [], fargate_profiles: [], sprites_profiles: [a, b, c] } as never }
    const deleted: string[] = []
    server.use(
      http.get('*/api/secrets', () => HttpResponse.json({ names: ['TOKEN_A', 'SHARED', 'DRAFT_TOKEN'], managed: [] })),
      http.delete('*/api/secrets/:name', ({ params }) => { deleted.push(String(params.name)); return HttpResponse.json({ ok: true }) }),
    )
    mount()
    await screen.findByLabelText('Coder URL')
    fireEvent.click(screen.getByRole('radio', { name: 'Fly.io Sprites' }))
    const removeProfile = (title: string) => {
      expand(title)
      const header = screen.getByRole('button', { name: new RegExp(`^${title}`) })
      const panel = header.closest('div')!.parentElement!
      fireEvent.click(within(panel).getByRole('button', { name: 'Remove profile' }))
    }
    // A saved profile's token waits for the save; a shared name is never deleted.
    removeProfile('Fly A')
    removeProfile('Fly B')
    expect(deleted).toEqual([])
    fireEvent.click(save())
    await waitFor(() => expect(deleted).toEqual(['TOKEN_A']))
    // A never-saved profile has no save to wait for: its token goes on Remove.
    fireEvent.click(screen.getAllByRole('button', { name: 'Add profile' }).find(button => !button.closest('[hidden]'))!)
    fireEvent.change(screen.getAllByLabelText('API token secret name').at(-1)!, { target: { value: 'DRAFT_TOKEN' } })
    removeProfile('Fly.io Sprites')
    await waitFor(() => expect(deleted).toEqual(['TOKEN_A', 'DRAFT_TOKEN']))
  })

  it('picks a default environment across providers, and drops it with the profile', async () => {
    const fly = { id: 'fly', name: 'Fly', token_secret: 'SPRITES_TOKEN', workdir: '/w' }
    stored = { session_environments: { coder_url: 'https://coder.example.test', profiles: [{ ...profile }], fargate_profiles: [], sprites_profiles: [fly], default_profile: '' } as never }
    mount()
    await screen.findByLabelText('Coder URL')
    // Global, above the provider tabs: off by default, no picker until it is on.
    const defaults = screen.getByRole('region', { name: 'Global defaults' })
    const toggle = within(defaults).getByRole('switch', { name: 'Start new chats in a session environment' })
    expect(toggle).not.toBeChecked()
    expect(within(defaults).queryByRole('combobox', { name: 'Default for new chats' })).toBeNull()
    fireEvent.click(toggle)
    const select = within(defaults).getByRole('combobox', { name: 'Default for new chats' })
    expect(select).toHaveTextContent('Development')
    fireEvent.click(select)
    fireEvent.click(await screen.findByRole('option', { name: 'Fly' }))
    fireEvent.click(save())
    await waitFor(() => expect(writes).toHaveLength(1))
    expect((writes[0] as { value: { default_profile: string } }).value.default_profile).toBe('fly')
    // Removing the default profile falls back to this machine in the same draft.
    fireEvent.click(screen.getByRole('radio', { name: 'Fly.io Sprites' }))
    expand('Fly')
    fireEvent.click(screen.getAllByRole('button', { name: 'Remove profile' }).find(b => !b.closest('[hidden]'))!)
    fireEvent.click(save())
    await waitFor(() => expect(writes).toHaveLength(2))
    expect((writes[1] as { value: { default_profile: string; sprites_profiles: unknown[] } }).value).toMatchObject({ default_profile: '', sprites_profiles: [] })
  })

  it('adds a boxd profile, where a snapshot keeps its own size, and checks it', async () => {
    stored = { session_environments: { coder_url: '', profiles: [], fargate_profiles: [], sprites_profiles: [], boxd_profiles: [] } as never }
    server.use(http.post('*/api/session-environments/check', () => HttpResponse.json({
      coder: { ok: false }, profiles: [], git: {}, boxd: [{ id: 'boxd-1', ok: true }],
    })))
    mount()
    await screen.findByLabelText('Coder URL')
    const radio = screen.getByRole('radio', { name: 'boxd' })
    expect(radio.closest('label')!.querySelector('span[aria-hidden="true"]')).not.toBeNull()
    fireEvent.click(radio)
    fireEvent.click(screen.getAllByRole('button', { name: 'Add profile' }).find(b => !b.closest('[hidden]'))!)
    fireEvent.click(screen.getByRole('combobox', { name: 'Machine size' }))
    fireEvent.click(await screen.findByRole('option', { name: '2 vCPU, 8 GiB' }))
    // Naming a snapshot returns the size to the default, which boxd requires.
    fireEvent.change(screen.getByLabelText('Snapshot to start from (optional)'), { target: { value: 'my-workspace' } })
    expect(screen.getByRole('combobox', { name: 'Machine size' })).toBeDisabled()
    fireEvent.click(save())
    await waitFor(() => expect(writes).toHaveLength(1))
    expect((writes[0] as { value: { boxd_profiles: unknown[] } }).value.boxd_profiles).toEqual([
      { id: 'boxd-1', name: 'boxd', snapshot: 'my-workspace', vcpu: 0, workdir: '/home/boxd/workspace' },
    ])
    const check = await screen.findByRole('button', { name: 'Check configuration' })
    await waitFor(() => expect(check).toBeEnabled())
    fireEvent.click(check)
    const results = await screen.findByRole('list', { name: 'Check results' })
    expect(results).toHaveTextContent('boxd: signed in to boxd on the gateway')
  })

  it('saves a boxd profile added beside a pooled Coder profile and a Sprites one', async () => {
    // The dev gateway's own section, as GET returns it.
    stored = { session_environments: {
      coder_url: 'https://coder.example.test',
      profiles: [{ id: 'dev', name: 'Coder MicroVM (dev)', template: 'm', workdir: '/home/coder/crew-session', hot_pool: 2 }],
      fargate_profiles: [],
      sprites_profiles: [{ id: 'sprites-1', name: 'Fly.io Sprites', workdir: '/home/sprite/workspace', token_secret: 'SPRITES_TOKEN' }],
      boxd_profiles: [], kiro_cloud_profiles: [], archived_retention_days: 7, default_profile: '',
    } } as never
    server.use(http.get('*/api/session-environments/pool', () => HttpResponse.json({ profiles: {} })))
    mount()
    await screen.findByLabelText('Coder URL')
    fireEvent.click(screen.getByRole('radio', { name: 'boxd' }))
    fireEvent.click(screen.getAllByRole('button', { name: 'Add profile' }).find(b => !b.closest('[hidden]'))!)
    const saveButton = save()
    expect(saveButton).toBeEnabled()
    fireEvent.click(saveButton)
    await waitFor(() => expect(writes).toHaveLength(1))
    expect((writes[0] as { value: { boxd_profiles: unknown[] } }).value.boxd_profiles).toHaveLength(1)
  })

  it('lists only the selected provider\'s workspaces in Manage', async () => {
    stored = { session_environments: { coder_url: 'https://coder.example.test', profiles: [{ ...profile }], fargate_profiles: [], sprites_profiles: [] } as never }
    server.use(http.get('*/api/session-environments/workspaces', () => HttpResponse.json({
      workspaces: [
        { workspace: 'crewenv-devuser-aaaa', kind: 'session', slot: '', state: 'running', template: 't', created_at: '', deletion_due_at: '' },
        { workspace: 'kirocrew-0123456789abcdef', provider: 'sprites', kind: 'session', slot: '', state: '', template: 'fly', created_at: '', deletion_due_at: '' },
      ],
    })))
    mount()
    let list = await screen.findByRole('list', { name: 'Session environments' })
    expect(list).toHaveTextContent('crewenv-devuser-aaaa')
    expect(list).not.toHaveTextContent('kirocrew-0123456789abcdef')
    fireEvent.click(screen.getByRole('radio', { name: 'Fly.io Sprites' }))
    list = await screen.findByRole('list', { name: 'Session environments' })
    expect(list).toHaveTextContent('kirocrew-0123456789abcdef')
    expect(list).toHaveTextContent('Session sprite')
    expect(list).not.toHaveTextContent('crewenv-devuser-aaaa')
    expect(screen.queryByRole('link', { name: 'Open Coder' })).toBeNull()
    expect(screen.getByRole('region', { name: '3. Manage' })).toHaveTextContent(/Each session gets its own sprite/)
  })

  it('labels a Fargate session task in the workspace list', async () => {
    stored = { session_environments: { coder_url: '', profiles: [], fargate_profiles: [{ ...fargate }] } }
    server.use(http.get('*/api/session-environments/workspaces', () => HttpResponse.json({
      workspaces: [
        { workspace: '0123456789ab', provider: 'fargate', kind: 'session', slot: 'chat-1', state: 'running', template: 'aws-dev', created_at: '', deletion_due_at: '' },
      ],
    })))
    mount()
    await screen.findByLabelText('Coder URL')
    fireEvent.click(screen.getByRole('radio', { name: 'AWS Fargate' }))
    const list = await screen.findByRole('list', { name: 'Session environments' })
    expect(list).toHaveTextContent('0123456789ab')
    expect(list).toHaveTextContent('Fargate session task')
    expect(screen.queryByRole('link', { name: 'Open Coder' })).toBeNull()
  })
})
