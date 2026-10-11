/**
 * Screenshot harness for the top-bar Kiro sign-in pill.
 *
 * Runs the REAL built SPA behind `serveDist`, every /api/** call answered from
 * fixtures by `stubDashboardApi`. No gateway, no kiro-cli, no credential: the
 * `/api/kas-login` answers are the token-free status bodies the gateway serves.
 * The config names KAS as the agent backend, the one backend the pill reports
 * for.
 *
 * Each frame ASSERTS the pill's presence (and its label and cause) before it
 * writes the PNG, so a stale bundle fails loudly instead of producing a
 * plausible picture:
 *
 *   01 expired, dark   -> pill "Kiro sign-in expired", cause expired
 *   02 refused, light  -> the same pill, cause refresh_rejected
 *   03 healthy, dark   -> no pill
 *   04 click           -> lands on Settings > Agent Harness (KIRO_SIGN_IN_PATH)
 *   05/06 narrow       -> below 640px the label hides: icon-only, still named
 *
 * `--before` captures the expired state against a build WITHOUT the pill (pass
 * that build with `--dist`) and asserts the pill is absent there.
 * `--verify-only` runs the assertions and writes nothing.
 *
 * Usage: node scripts/capture-kiro-sign-in-pill.mjs [outDir] [--dist <dir>] [--before] [--verify-only]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist, DEFAULT_DIST } from './lib/serve-dist.mjs'
import { KIROCREW_CONFIG_FIXTURE, logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

// The node toolchain injects its own libstdc++ on LD_LIBRARY_PATH, which the
// bundled Chromium then loads in preference to the system one and fails on.
delete process.env.LD_LIBRARY_PATH

const args = process.argv.slice(2)
const VERIFY_ONLY = args.includes('--verify-only')
const BEFORE = args.includes('--before')
const distAt = args.indexOf('--dist')
const DIST = distAt >= 0 ? args[distAt + 1] : DEFAULT_DIST
const OUT = args.find((a, i) => !a.startsWith('--') && args[i - 1] !== '--dist') || '../temp-screenshots/kiro-sign-in-pill'
if (!VERIFY_ONLY) mkdirSync(OUT, { recursive: true })

const KAS_CONFIG = { ...KIROCREW_CONFIG_FIXTURE, agent: { ...KIROCREW_CONFIG_FIXTURE.agent, acp_backend: 'kas' } }
const HEALTHY = {
  authenticated: true, provider: 'Google', identity: 'social', transport: 'device',
  expires_at: '2099-01-01T00:00:00Z', expired: false, has_refresh_token: true,
  refresh_rejected: false, usable: true,
}
const EXPIRED = { ...HEALTHY, expires_at: '2000-01-01T00:00:00Z', expired: true, has_refresh_token: false, usable: false }
const REFUSED = { ...HEALTHY, refresh_rejected: true }
const NOW_ISO = new Date(Date.now() - 60_000).toISOString()
const SLOT = { key: 'chat-1', title: 'Release notes draft', messages: 4, running: false, agent: '', mode: '', tags: [], last_ts: NOW_ISO, last_turn_ts: NOW_ISO }
const KAS_ROW = {
  id: 'kas', policy_id: 'kas', selectable: true, independent_setup: false,
  installed: 'installed', missing_components: [], install_command: '', restart_required: false,
  capabilities: [{ id: 'crew_tools', available: true }],
  security_notes: [], operator_notes: [], tool_approval: 'agent_spec', offered_by_build: true,
  mcp: { per_tool_deny: 'settings-file', costs_whole_server: false, ineffective: [] },
}

async function main() {
  const { srv, base } = await serveDist(DIST)
  const browser = await chromium.launch()
  const results = []

  async function frame(name, { status, theme = 'dark', want, click = false, width = 1400, route = '/' }) {
    const context = await browser.newContext({ viewport: { width, height: 900 }, deviceScaleFactor: 2 })
    const page = await context.newPage()
    logPageProblems(page)
    const extra = async (path, route) => {
      if (path === '/api/config/kirocrew') { await json(route, KAS_CONFIG); return true }
      if (path === '/api/kas-login') { await json(route, status); return true }
      // The shell's agent pickers need a real roster to boot past the app-shell
      // boundary (the catch-all's `[]` guess is not one).
      if (path === '/api/agents' || path === '/api/chat/agents' || path === '/api/agents/catalog') {
        await json(route, { agents: [{ name: 'kirocrew', source: 'builtin' }], default_agent: 'kirocrew' })
        return true
      }
      if (path.startsWith('/api/chat/slots/')) {
        await json(route, { running: false, has_more: false, total: 1, queue: [], messages: [{ role: 'user', ts: Date.now() / 1000 - 300, content: 'hello' }] })
        return true
      }
      // Settings > Agent Harness, where the click lands: one KAS row, trimmed to
      // what the picker reads, so the ringed sign-in card renders.
      if (path === '/api/acp-backends') { await json(route, { backends: [KAS_ROW] }); return true }
      if (path === '/api/config/schema') {
        await json(route, { entries: [{ path: 'agent.acp_backend', type: 'enum', enumValues: ['', 'kas'] }] })
        return true
      }
      return false
    }
    await stubDashboardApi(page, { slots: [SLOT], theme, extra, localStorageEntries: { 'mc-active-slot': SLOT.key } })
    await page.goto(base + route, { waitUntil: 'domcontentloaded' })
    await page.locator('header').first().waitFor()

    const pill = page.getByTestId('kiro-sign-in-pill')
    let got = null
    if (want) {
      await pill.waitFor({ timeout: 15_000 }).catch(() => {})
    } else {
      // Long enough for the config and status reads to land and a pill to mount.
      await page.waitForTimeout(2500)
    }
    if (await pill.count()) {
      got = {
        visible: await pill.isVisible(),
        text: (await pill.innerText()).trim(),
        cause: await pill.getAttribute('data-cause'),
        title: await pill.getAttribute('title'),
        name: await pill.getAttribute('aria-label'),
      }
    }
    // The icon-only form (narrow bar) still has to carry the label in its
    // accessible name, since its visible text is gone.
    const ok = want
      ? !!got && got.visible && got.text === want.text && got.cause === want.cause
        && (got.name || '').startsWith('Kiro sign-in expired')
      : got === null
    const row = { name, ok, pill: got }

    if (!VERIFY_ONLY) {
      const header = await page.locator('header').first().boundingBox()
      const clipWidth = Math.min(760, header.width)
      await page.screenshot({
        path: `${OUT}/${name}.png`,
        clip: { x: Math.max(0, header.x + header.width - clipWidth), y: header.y, width: clipWidth, height: header.height + 12 },
      })
      console.log('wrote', `${OUT}/${name}.png`)
    }
    if (click && got) {
      await pill.click()
      await page.waitForURL(/\/settings\/agent/, { timeout: 10_000 }).catch(() => {})
      row.landedOn = new URL(page.url()).pathname + new URL(page.url()).search
      row.ok = row.ok && row.landedOn.startsWith('/settings/agent') && row.landedOn.includes('kiro-sign-in')
      if (!VERIFY_ONLY) {
        await page.waitForTimeout(1500)
        await page.screenshot({ path: `${OUT}/${name}-landed.png` })
        console.log('wrote', `${OUT}/${name}-landed.png`)
      }
    }
    results.push(row)
    await context.close()
  }

  if (BEFORE) {
    await frame('00-before-expired-dark', { status: EXPIRED, want: null })
  } else {
    await frame('01-expired-dark', { status: EXPIRED, want: { text: 'Kiro sign-in expired', cause: 'expired' } })
    await frame('02-refused-light', { status: REFUSED, theme: 'light', want: { text: 'Kiro sign-in expired', cause: 'refresh_rejected' } })
    await frame('03-healthy-dark', { status: HEALTHY, want: null })
    await frame('04-click-opens-sign-in', { status: EXPIRED, want: { text: 'Kiro sign-in expired', cause: 'expired' }, click: true })
    // Below 640px the label is hidden and the pill is icon-only. The phone
    // chat page's single bar holds no actions group (two controls by rule, the
    // same reason the update pill is absent there), so these use another page.
    await frame('05-narrow-expired-dark', { status: EXPIRED, width: 560, route: '/schedule', want: { text: '', cause: 'expired' } })
    await frame('06-narrow-refused-light', { status: REFUSED, theme: 'light', width: 560, route: '/schedule', want: { text: '', cause: 'refresh_rejected' } })
  }

  await browser.close()
  srv.close()
  console.log('--- assertions ---')
  for (const r of results) console.log(JSON.stringify(r))
  if (!results.every(r => r.ok)) {
    console.error('FAIL: the top bar did not show the expected Kiro sign-in pill state')
    process.exit(1)
  }
  console.log('OK')
}

main().catch(err => { console.error(err); process.exit(1) })
