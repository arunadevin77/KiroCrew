// MCP Management: one press shares a server.
//
// Contract under test:
// - every row action is a real labelled <button> ("Share this server", "Stop
//   sharing", "Use new command"), never a bare switch
// - sharing is ONE request: no review dialog (the endpoint records the exact
//   launch itself)
// - a row shared while sharing is off turns sharing on as well
// - a server that can never be shared offers no share button, and one line why
// - a shared row whose command changed reads "Paused · command changed", shows
//   the new command in the row, and its confirm button pins that launch
// - the saving shown is the MEASURED number the gateway sent, and no line at
//   all when it sent none
// - a share runs outside the chat sandbox, so the launch it approves is shown
//   in the row before the press; Share all lists every launch and shares only
//   on a second press, pinned to what it listed
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, getDefaultNormalizer, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api/client'
import { McpManagement } from '../pages/settings/McpManagement'

function server(over: Record<string, unknown> = {}) {
  return {
    name: 'alpha-mcp',
    stub: false,
    can_stub: true,
    in_allowlist: false,
    entry_poolable: false,
    agents: ['kirocrew'],
    transport: 'stdio',
    denylisted: false,
    share_blocked: null,
    saves_bytes: null,
    ...over,
  }
}

function launch(over: Record<string, unknown> = {}) {
  return {
    name: 'alpha-mcp',
    commands: [['/opt/alpha', '--one']],
    envs: [[]],
    complete: true,
    expected_launch: 'alpha-identity',
    ...over,
  }
}

const status = (over: Record<string, unknown> = {}) => ({
  enabled: false,
  stub: [] as string[],
  stub_count: 0,
  running: false,
  ping_ok: false,
  supported: true,
  ...over,
})

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={qc}>
        <McpManagement />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.restoreAllMocks()
})
afterEach(cleanup)

describe('one-step sharing', () => {
  it('shares on one press, with no review dialog', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: true }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server()] } as never)
    vi.spyOn(api, 'mcpGatewayLaunchPreview').mockResolvedValue(launch() as never)
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub').mockResolvedValue({
      ok: true, name: 'alpha-mcp', stub: true, restart_required: true,
    } as never)

    mount()
    const share = await screen.findByRole('button', { name: 'Share alpha-mcp' })
    expect(share.tagName).toBe('BUTTON')
    expect(share.textContent).toContain('Share this server')
    expect(screen.queryByRole('switch', { name: 'Share alpha-mcp' })).toBeNull()
    // Chain: servers read -> the row's launch read enables the button.
    await waitFor(() => expect((share as HTMLButtonElement).disabled).toBe(false), { timeout: 5000 })
    // The launch it approves is on screen, in the row, before the press,
    // with no Details to open: it runs outside the chat sandbox.
    expect(screen.getByText('/opt/alpha --one')).toBeTruthy()
    // What sharing gains comes first; what the shared copy can reach follows.
    expect(screen.getByText('Each chat runs its own copy of this today. Sharing runs one copy for all your chats.')).toBeTruthy()
    expect(screen.getByText("That copy runs outside each chat's sandbox, with your access, like any program you start.")).toBeTruthy()
    // Where the command comes from, before the first press too.
    expect(screen.getByText('Comes from the setup of: kirocrew')).toBeTruthy()
    fireEvent.click(share)

    await waitFor(() => expect(setStub).toHaveBeenCalledTimes(1))
    // The launch approved is exactly the one read for this row.
    expect(setStub).toHaveBeenCalledWith('alpha-mcp', true, 'alpha-identity')
    // Nothing asks a second time.
    expect(screen.queryByRole('button', { name: /approve/i })).toBeNull()
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('turns sharing on too when it was off', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: false }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server()] } as never)
    vi.spyOn(api, 'mcpGatewaySetStub').mockResolvedValue({ ok: true, name: 'alpha-mcp', stub: true } as never)
    const enable = vi.spyOn(api, 'mcpGatewayEnable').mockResolvedValue({ ping_ok: true } as never)
    vi.spyOn(api, 'mcpGatewayLaunchPreview').mockResolvedValue(launch() as never)

    mount()
    const share = await screen.findByRole('button', { name: 'Share alpha-mcp' })
    // Chain: servers read -> the row's launch read enables the button.
    await waitFor(() => expect((share as HTMLButtonElement).disabled).toBe(false), { timeout: 5000 })
    fireEvent.click(share)

    await waitFor(() => expect(enable).toHaveBeenCalledWith(true))
  })

  it('stops sharing with its own button', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: true, stub: ['alpha-mcp'] }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [server({ stub: true, in_allowlist: true })],
    } as never)
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub').mockResolvedValue({ ok: true, name: 'alpha-mcp', stub: false } as never)

    mount()
    const stop = await screen.findByRole('button', { name: 'Stop sharing alpha-mcp' })
    expect(stop.textContent).toContain('Stop sharing')
    fireEvent.click(stop)

    await waitFor(() => expect(setStub).toHaveBeenCalledWith('alpha-mcp', false, undefined))
  })

  it.each([
    ['credential_vending', 'gives each chat its own credentials'],
    ['needs_tenant_nonce', 'each chat needs its own copy'],
    ['computer_use', 'it controls this computer'],
  ])('refuses %s with no share button and one line why', async (code, reason) => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: true }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [server({ name: 'creds-agent', share_blocked: code })],
    } as never)
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub')

    mount()
    expect(await screen.findByText(new RegExp(`Can't share: ${reason}`))).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Share creds-agent' })).toBeNull()
    expect(setStub).not.toHaveBeenCalled()
  })

  it('shows a changed command and confirms exactly that launch', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(
      status({
        enabled: true,
        stub: ['alpha-mcp'],
        launch_refused: {
          'alpha-mcp': {
            reason: 'changed_needs_reapproval',
            approved_commands: [['/opt/alpha', '--one']],
            approved_envs: [['MODE=old']],
          },
        },
      }) as never,
    )
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [server({ stub: true, in_allowlist: true })],
    } as never)
    vi.spyOn(api, 'mcpGatewayLaunchPreview').mockResolvedValue({
      name: 'alpha-mcp',
      commands: [['/opt/alpha', '--two']],
      envs: [[]],
      complete: true,
      expected_launch: 'pinned-identity',
    } as never)
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub').mockResolvedValue({ ok: true, name: 'alpha-mcp', stub: true } as never)

    mount()
    const confirm = await screen.findByRole('button', { name: 'Use the new command for alpha-mcp' })
    expect(confirm.textContent).toContain('Use new command')
    expect(screen.getAllByText('Paused · command changed').length).toBeGreaterThan(0)
    // The new command is on screen before anything can be confirmed, with the
    // one approved before it, so the two can be compared.
    // Chain: status + servers reads -> the row's launch read renders the command.
    expect(await screen.findByText('/opt/alpha --two', undefined, { timeout: 5000 })).toBeTruthy()
    expect(screen.getByText('Its command changed. Compare it with the command you approved earlier.')).toBeTruthy()
    // The earlier approval names who gave it.
    expect(screen.getByText('You approved this command earlier.')).toBeTruthy()
    expect(screen.getByText('/opt/alpha --one')).toBeTruthy()
    expect(screen.getByText('MODE=old')).toBeTruthy()
    // Where the command is set, so the person can tell who changed it.
    expect(screen.getByText('Comes from the setup of: kirocrew')).toBeTruthy()
    // Chain: servers read -> the row's launch read enables the button.
    await waitFor(() => expect((confirm as HTMLButtonElement).disabled).toBe(false), { timeout: 5000 })
    fireEvent.click(confirm)

    await waitFor(() => expect(setStub).toHaveBeenCalledWith('alpha-mcp', true, 'pinned-identity'))
    expect(setStub).toHaveBeenCalledTimes(1)
  })

  it('quotes each argument, so where one ends is never hidden', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: true }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server()] } as never)
    vi.spyOn(api, 'mcpGatewayLaunchPreview').mockResolvedValue(
      launch({ commands: [['sh', '-c', 'echo ok', "it's"]] }) as never,
    )

    mount()
    expect(await screen.findByText(`sh -c 'echo ok' 'it'"'"'s'`, undefined, { timeout: 5000 })).toBeTruthy()
  })

  it('keeps runs of spaces in a command and its env, so it shows as it runs', async () => {
    // Collapsed spaces would make `[ "  " = " " ]` look true and hide the branch that runs.
    const script = 'if [ "  " = " " ]; then :; else rm -f ~/Documents/report.txt; fi'
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: true }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server()] } as never)
    vi.spyOn(api, 'mcpGatewayLaunchPreview').mockResolvedValue(
      launch({ commands: [['sh', '-c', script]], envs: [['MODE=a  b']] }) as never,
    )

    mount()
    const exact = { normalizer: getDefaultNormalizer({ collapseWhitespace: false, trim: false }) }
    const cmd = await screen.findByText(`sh -c '${script}'`, exact, { timeout: 5000 })
    expect(cmd.className).toContain('whitespace-pre-wrap')
    expect(screen.getByText('MODE=a  b', exact).className).toContain('whitespace-pre-wrap')
  })

  it('counts every row it lists in the header, a blocked one too', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: true, stub: ['alpha-mcp'] }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [
        server({ stub: true, in_allowlist: true }),
        server({ name: 'beta-mcp' }),
        server({ name: 'gamma-mcp', share_blocked: 'own_copy' }),
      ],
    } as never)
    vi.spyOn(api, 'mcpGatewayLaunchPreview').mockResolvedValue(launch() as never)

    mount()
    expect(await screen.findByText('1 of 3 shared')).toBeTruthy()
  })

  it('reads a marked row as paused, and does not count it, while sharing is off', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: false, stub: ['alpha-mcp'] }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [server({ stub: true, in_allowlist: true }), server({ name: 'beta-mcp' })],
    } as never)
    vi.spyOn(api, 'mcpGatewayLaunchPreview').mockResolvedValue(launch() as never)

    mount()
    expect(await screen.findByText('0 of 2 shared')).toBeTruthy()
    expect(screen.getAllByText('Shared · paused (Sharing is off)').length).toBeGreaterThan(0)
  })

  it('never shows a launch read before the command changed as the new one', async () => {
    const statusSpy = vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: true }) as never)
    const serversSpy = vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server()] } as never)
    vi.spyOn(api, 'mcpGatewayLaunchPreview')
      .mockResolvedValueOnce(launch() as never)
      .mockResolvedValue(launch({ commands: [['/opt/alpha', '--two']], expected_launch: 'two' }) as never)
    vi.spyOn(api, 'mcpGatewaySetStub').mockResolvedValue({ ok: true, name: 'alpha-mcp', stub: true } as never)

    mount()
    const share = await screen.findByRole('button', { name: 'Share alpha-mcp' })
    await waitFor(() => expect((share as HTMLButtonElement).disabled).toBe(false), { timeout: 5000 })
    expect(screen.getByText('/opt/alpha --one')).toBeTruthy()
    // Shared, and later its command changed: the page refetches status and servers.
    statusSpy.mockResolvedValue(
      status({
        enabled: true,
        stub: ['alpha-mcp'],
        launch_refused: {
          'alpha-mcp': { reason: 'changed_needs_reapproval', approved_commands: [['/opt/alpha', '--one']] },
        },
      }) as never,
    )
    serversSpy.mockResolvedValue({ servers: [server({ stub: true, in_allowlist: true })] } as never)
    fireEvent.click(share)
    // Chain: share POST -> status/servers refetch -> the waiting row reads the launch afresh.
    expect(await screen.findByText('/opt/alpha --two', undefined, { timeout: 5000 })).toBeTruthy()
  })

  it('cannot confirm a changed command it could not show', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(
      status({
        enabled: true,
        stub: ['alpha-mcp'],
        launch_refused: { 'alpha-mcp': { reason: 'changed_needs_reapproval' } },
      }) as never,
    )
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [server({ stub: true, in_allowlist: true })],
    } as never)
    vi.spyOn(api, 'mcpGatewayLaunchPreview').mockResolvedValue({
      name: 'alpha-mcp', commands: [], envs: [], complete: false,
    } as never)
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub')

    mount()
    const confirm = await screen.findByRole('button', { name: 'Use the new command for alpha-mcp' })
    await waitFor(() => expect(api.mcpGatewayLaunchPreview).toHaveBeenCalled())
    expect((confirm as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(confirm)
    expect(setStub).not.toHaveBeenCalled()
    // It can still be turned off.
    const stop = screen.getByRole('button', { name: 'Stop sharing alpha-mcp' })
    expect((stop as HTMLButtonElement).disabled).toBe(false)
  })

  it('says so when the changed command could not be read', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(
      status({
        enabled: true,
        stub: ['alpha-mcp'],
        launch_refused: { 'alpha-mcp': { reason: 'changed_needs_reapproval' } },
      }) as never,
    )
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [server({ stub: true, in_allowlist: true })],
    } as never)
    vi.spyOn(api, 'mcpGatewayLaunchPreview').mockRejectedValue(new Error('503'))

    mount()
    await screen.findByRole('button', { name: 'Use the new command for alpha-mcp' })
    // Chain: status + servers reads -> the launch read fails -> the error shows.
    expect(await screen.findByText("Can't share: its command can't be shown.", undefined, { timeout: 5000 })).toBeTruthy()
  })
})

describe('the page-wide switch', () => {
  it('names marked servers the check argues against while sharing is off', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: false, stub: ['beta-mcp'] }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [
        server({ name: 'beta-mcp', stub: true, in_allowlist: true, recommendation: { strength: 'refuted', recommendStub: false, recommendShare: false, reasons: [] } }),
      ],
    } as never)

    mount()
    expect(await screen.findByText(/the check advises against: beta-mcp$/)).toBeTruthy()
  })
})

describe('the saving shown', () => {
  it('is the measured number the gateway sent', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status() as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [server({ saves_bytes: 240_000_000 })],
    } as never)

    mount()
    const hint = await screen.findByText(/^Saves \d/)
    // The number is the gateway's measurement, formatted; not a figure of ours.
    expect(hint.textContent).toMatch(/^Saves 240\s?MB$/)
  })

  it('says nothing when there is no measurement', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status() as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [server({ saves_bytes: null })],
    } as never)

    mount()
    await screen.findByRole('button', { name: 'Share alpha-mcp' })
    expect(screen.queryByText(/^Saves/)).toBeNull()
  })

  it('is not shown for a server already shared', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: true, stub: ['alpha-mcp'] }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [server({ stub: true, in_allowlist: true })],
    } as never)

    mount()
    await screen.findByRole('button', { name: 'Stop sharing alpha-mcp' })
    expect(screen.queryByText(/^Saves/)).toBeNull()
  })
})

describe('the launch a share approves', () => {
  it('offers no share button for a launch it could not read whole, only the reason', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: true }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server()] } as never)
    vi.spyOn(api, 'mcpGatewayLaunchPreview').mockResolvedValue(launch({ complete: false, expected_launch: undefined }) as never)
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub')

    mount()
    // Chain: servers read -> the row's launch read; bounded so a hang fails here.
    expect(await screen.findByText("Can't share: its command can't be shown.", undefined, { timeout: 5000 })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Share alpha-mcp' })).toBeNull()
    expect(setStub).not.toHaveBeenCalled()
  })

  it('reads the launch again after a refused share, so the next press pins the current one', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: true }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server()] } as never)
    const preview = vi
      .spyOn(api, 'mcpGatewayLaunchPreview')
      .mockResolvedValueOnce(launch() as never)
      .mockResolvedValue(launch({ commands: [['/opt/alpha', '--moved']], expected_launch: 'moved-identity' }) as never)
    const setStub = vi
      .spyOn(api, 'mcpGatewaySetStub')
      .mockRejectedValueOnce(new Error('launch_changed_since_display'))
      .mockResolvedValue({ ok: true, name: 'alpha-mcp', stub: true } as never)

    mount()
    const share = await screen.findByRole('button', { name: 'Share alpha-mcp' })
    // Chain: servers read -> launch read enables the button.
    await waitFor(() => expect((share as HTMLButtonElement).disabled).toBe(false), { timeout: 5000 })
    fireEvent.click(share)
    // Chain: refused POST -> the launch read runs again and shows the moved launch.
    expect(await screen.findByText('/opt/alpha --moved', undefined, { timeout: 5000 })).toBeTruthy()
    // Read again after the refusal (the page also drops every cached launch on a write).
    expect(preview.mock.calls.length).toBeGreaterThanOrEqual(2)
    fireEvent.click(screen.getByRole('button', { name: 'Share alpha-mcp' }))
    await waitFor(() => expect(setStub).toHaveBeenLastCalledWith('alpha-mcp', true, 'moved-identity'), { timeout: 5000 })
  })

  it('names the marked servers that start sharing too when sharing is off', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: false, stub: ['beta-mcp'] }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [server(), server({ name: 'beta-mcp', stub: true, in_allowlist: true })],
    } as never)
    vi.spyOn(api, 'mcpGatewayLaunchPreview').mockResolvedValue(launch() as never)

    mount()
    // Chain: servers read -> the row's launch read renders the launch box.
    expect(
      await screen.findByText('Sharing turns on, so these start sharing too: beta-mcp', undefined, { timeout: 5000 }),
    ).toBeTruthy()
  })
})

describe('details', () => {
  it('shows a shared server\'s launch only when opened, env names before values', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled: true, stub: ['alpha-mcp'] }) as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({
      servers: [server({ stub: true, in_allowlist: true })],
    } as never)
    const preview = vi.spyOn(api, 'mcpGatewayLaunchPreview').mockResolvedValue({
      name: 'alpha-mcp',
      commands: [['/opt/alpha', '--one'], ['/opt/alpha', '--one']],
      envs: [['PATH=/usr/bin', 'TOKEN=${TOKEN}'], ['PATH=/usr/bin:/bin']],
      complete: true,
    } as never)

    const { container } = mount()
    await screen.findByRole('button', { name: 'Stop sharing alpha-mcp' })
    expect(screen.queryByText('/opt/alpha --one')).toBeNull()

    const details = container.querySelector('details') as HTMLDetailsElement
    details.open = true
    fireEvent(details, new Event('toggle'))

    // Chain: the toggle -> the launch read -> the rendered launches.
    await waitFor(() => expect(preview).toHaveBeenCalledWith('alpha-mcp'), { timeout: 5000 })
    expect(await screen.findByText('Variants: 2', undefined, { timeout: 5000 })).toBeTruthy()
    expect(screen.getAllByText('/opt/alpha --one')).toHaveLength(2)
    expect(screen.getByText('TOKEN=${TOKEN}')).toBeTruthy()
    expect(screen.getByText('PATH=/usr/bin:/bin')).toBeTruthy()
  })
})

describe('share all', () => {
  function mountTwo(enabled = true) {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(status({ enabled }) as never)
    const declared = {
      strength: 'declared',
      recommendStub: true,
      recommendShare: true,
      reasons: [{ code: 'preflight_passed', detail: '' }],
    }
    const servers = {
      servers: [
        { ...server(), recommendation: declared },
        { ...server({ name: 'beta-mcp' }), recommendation: declared },
      ],
    }
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue(servers as never)
    vi.spyOn(api, 'mcpGatewayLaunchPreview').mockImplementation(
      async (name: string) =>
        launch({
          name,
          commands: [[`/opt/${name}`, '--serve']],
          expected_launch: `${name}-identity`,
        }) as never,
    )
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub').mockImplementation(
      async (name: string) => ({ ok: true, name, stub: true, stubbed: [name] }) as never,
    )
    mount()
    return setStub
  }

  it('lists every command first and shares nothing on the first press', async () => {
    const setStub = mountTwo()
    const all = await screen.findByRole('button', { name: 'Share all that can be shared' })
    await waitFor(() => expect((all as HTMLButtonElement).disabled).toBe(false), { timeout: 5000 })
    fireEvent.click(all)

    // Chain: servers re-read -> one launch read per candidate -> the list renders.
    const list = await screen.findByRole('region', { name: 'Servers to share' }, { timeout: 5000 })
    expect(list.textContent).toContain('/opt/alpha-mcp --serve')
    expect(list.textContent).toContain('/opt/beta-mcp --serve')
    expect(setStub).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('region', { name: 'Servers to share' })).toBeNull(), { timeout: 5000 })
    expect(setStub).not.toHaveBeenCalled()
  })

  it('reports a failed launch read as an error, not as nothing to share', async () => {
    const setStub = mountTwo()
    vi.mocked(api.mcpGatewayLaunchPreview).mockImplementation(async (name: string) => {
      if (name === 'beta-mcp') throw new Error('read failed')
      return launch({ name, commands: [[`/opt/${name}`, '--serve']], expected_launch: `${name}-identity` }) as never
    })
    const all = await screen.findByRole('button', { name: 'Share all that can be shared' })
    await waitFor(() => expect((all as HTMLButtonElement).disabled).toBe(false), { timeout: 5000 })
    fireEvent.click(all)

    // Chain: servers re-read -> one launch read per candidate -> error + list.
    expect(await screen.findByText(/Failed: beta-mcp\./, undefined, { timeout: 5000 })).toBeTruthy()
    const list = await screen.findByRole('region', { name: 'Servers to share' }, { timeout: 5000 })
    expect(list.textContent).not.toContain('/opt/beta-mcp')
    expect(setStub).not.toHaveBeenCalled()
  })

  it('shares exactly the launches it listed', async () => {
    const setStub = mountTwo()
    const all = await screen.findByRole('button', { name: 'Share all that can be shared' })
    await waitFor(() => expect((all as HTMLButtonElement).disabled).toBe(false), { timeout: 5000 })
    fireEvent.click(all)

    // Chain: servers re-read -> one launch read per candidate -> the confirm button.
    const confirm = await screen.findByRole('button', { name: /^Share the 2 listed$/ }, { timeout: 5000 })
    expect(confirm.tagName).toBe('BUTTON')
    fireEvent.click(confirm)

    // Chain: one stub POST per listed server, sent in turn.
    await waitFor(() => expect(setStub).toHaveBeenCalledTimes(2), { timeout: 5000 })
    expect(setStub).toHaveBeenCalledWith('alpha-mcp', true, 'alpha-mcp-identity', true)
    expect(setStub).toHaveBeenCalledWith('beta-mcp', true, 'beta-mcp-identity', true)
  })

  it('turns sharing on before the shares, so each is checked as pooled', async () => {
    const setStub = mountTwo(false)
    const order: string[] = []
    const enable = vi.spyOn(api, 'mcpGatewayEnable').mockImplementation(async () => {
      order.push('enable')
      return { ok: true, enabled: true, ping_ok: true } as never
    })
    setStub.mockImplementation(async (name: string) => {
      order.push(name)
      return { ok: true, name, stub: true, stubbed: [name] } as never
    })
    const all = await screen.findByRole('button', { name: 'Share all that can be shared' })
    await waitFor(() => expect((all as HTMLButtonElement).disabled).toBe(false), { timeout: 5000 })
    fireEvent.click(all)
    fireEvent.click(await screen.findByRole('button', { name: /^Share the 2 listed$/ }, { timeout: 5000 }))

    // Chain: enable POST -> one stub POST per listed server, in turn.
    await waitFor(() => expect(setStub).toHaveBeenCalledTimes(2), { timeout: 5000 })
    expect(order).toEqual(['enable', 'alpha-mcp', 'beta-mcp'])
    expect(enable).toHaveBeenCalledTimes(1)
  })
})
