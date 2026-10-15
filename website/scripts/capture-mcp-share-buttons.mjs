/**
 * Capture harness for the MCP Management share buttons (PR #19661).
 *
 * Shoots a RUNNING pod. The page, its components and its styles are the pod's
 * build; the three MCP reads (`status`, `servers`, `servers/launch`) are
 * answered by route stubs with a fixed set of servers so every state the
 * reviewer asked for is on screen at once and no host path leaks into a frame.
 * Nothing is written: the stub and enable POSTs are never sent.
 *
 * Usage:
 *   POD_URL=http://127.0.0.1:7845 POD_TOKEN=<token> \
 *     node scripts/capture-mcp-share-buttons.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const OUT = process.argv[2] || '../temp-screenshots/mcp-share-buttons'
const BASE = process.env.POD_URL
const TOKEN = process.env.POD_TOKEN
if (!BASE || !TOKEN) {
  console.error('POD_URL and POD_TOKEN are required')
  process.exit(2)
}
mkdirSync(OUT, { recursive: true })

const declared = {
  strength: 'declared',
  recommendStub: true,
  recommendShare: true,
  reasons: [{ code: 'preflight_passed', detail: '' }],
}
const row = over => ({
  stub: false,
  can_stub: true,
  in_allowlist: false,
  entry_poolable: false,
  agents: ['kirocrew'],
  transport: 'stdio',
  denylisted: false,
  share_blocked: null,
  saves_bytes: null,
  recommendation: declared,
  ...over,
})
const SERVERS = [
  row({ name: 'chorus-mcp', saves_bytes: 240_000_000 }),
  row({ name: 'slack-mcp', stub: true, in_allowlist: true }),
  row({ name: 'grasp-mcp', stub: true, in_allowlist: true }),
  row({ name: 'creds-agent', can_stub: true, share_blocked: 'credential_vending' }),
  row({ name: 'taskei-mcp' }),
  // No LAUNCH entry: its read answers an incomplete display.
  row({ name: 'notion-mcp' }),
]
const LAUNCH = {
  'chorus-mcp': { commands: [['/opt/mcp/chorus/bin/chorus-mcp', '--stdio']], envs: [['LOG_LEVEL=warn']] },
  'slack-mcp': { commands: [['/opt/mcp/slack/bin/slack-mcp']], envs: [['SLACK_WORKSPACE=example']] },
  'grasp-mcp': {
    commands: [['/opt/mcp/grasp/bin/grasp-mcp', '--profile', 'work', '--stdio']],
    envs: [['GRASP_REGION=us-west-2']],
  },
  'taskei-mcp': { commands: [['/opt/mcp/taskei/bin/taskei-mcp']], envs: [[]] },
}

const pending = { delay: false }

async function stub(page) {
  await page.route('**/api/mcp-gateway/status', r =>
    r.fulfill({
      json: {
        enabled: true,
        stub: ['slack-mcp', 'grasp-mcp'],
        stub_count: 2,
        running: true,
        ping_ok: true,
        supported: true,
        launch_refused: {
          'grasp-mcp': {
            reason: 'changed_needs_reapproval',
            approved_commands: [['/opt/mcp/grasp/bin/grasp-mcp', '--stdio']],
            approved_envs: [['GRASP_REGION=us-east-1']],
          },
        },
      },
    }),
  )
  await page.route('**/api/mcp-gateway/servers', r => r.fulfill({ json: { servers: SERVERS } }))
  await page.route('**/api/mcp-gateway/servers/launch?**', async r => {
    const name = new URL(r.request().url()).searchParams.get('name')
    if (pending.delay && name === 'taskei-mcp') await new Promise(res => setTimeout(res, 60_000))
    const l = LAUNCH[name]
    if (!l) return r.fulfill({ json: { name, commands: [], envs: [], complete: false } })
    return r.fulfill({ json: { name, ...l, complete: true, expected_launch: `${name}-identity` } })
  })
  await page.route('**/api/mcp/measure', r => r.fulfill({ json: { running: false, done: 0, total: 0, error: '' } }))
  // Never write anything: these are the only mutating calls the page makes.
  await page.route('**/api/mcp-gateway/servers/stub', r => r.abort())
  await page.route('**/api/mcp-gateway/enable', r => r.abort())
}

async function open(page) {
  await page.evaluate(() => {
    window.history.pushState({}, '', '/developer?tab=mcp-pool')
    window.dispatchEvent(new PopStateEvent('popstate'))
  })
  await page.getByRole('button', { name: 'Share chorus-mcp' }).waitFor({ timeout: 30_000 })
  await page.waitForTimeout(1200)
}

const section = page => page.locator('section').filter({ has: page.getByRole('button', { name: 'Share all' }) })

const browser = await chromium.launch()
try {
  const port = new URL(BASE).port
  for (const [label, viewport] of [['desktop', { width: 1280, height: 2200 }], ['narrow', { width: 360, height: 1600 }]]) {
    const context = await browser.newContext({ viewport, deviceScaleFactor: 2 })
    await context.addCookies(
      [BASE, `http://localhost:${port}`].map(u => ({ name: `mc_token_${port}`, value: TOKEN, url: u })),
    )
    const page = await context.newPage()
    await stub(page)
    await page.addInitScript(() => {
      localStorage.setItem('mc-onboarded', '1')
      localStorage.setItem('mc-crewmates-onboarded', '1')
      localStorage.setItem('mc-theme', 'light')
    })
    await page.goto(`${BASE}/`, { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(1500)
    await open(page)

    if (label === 'narrow') {
      await section(page).screenshot({ path: `${OUT}/06-narrow-360.png` })
      await context.close()
      continue
    }
    await section(page).screenshot({ path: `${OUT}/01-default.png` })
    // The whole tab: the page-wide Sharing switch and Pre-install sections below the rows.
    await page.locator('[role="tabpanel"][data-state="active"]').screenshot({ path: `${OUT}/08-whole-tab.png` })

    const share = page.getByRole('button', { name: 'Share chorus-mcp' })
    await share.hover()
    await page.waitForTimeout(300)
    await page.locator('li').filter({ has: share }).screenshot({ path: `${OUT}/02-hover.png` })
    await page.mouse.move(0, 0)
    await share.focus()
    await page.keyboard.press('Shift+Tab')
    await page.keyboard.press('Tab')
    await page.waitForTimeout(300)
    await page.locator('li').filter({ has: share }).screenshot({ path: `${OUT}/03-focus.png` })

    const confirm = page.getByRole('button', { name: 'Use the new command for grasp-mcp' })
    await page.locator('li').filter({ has: confirm }).screenshot({ path: `${OUT}/04-command-changed.png` })

    await page.getByRole('button', { name: 'Share all' }).click()
    await page.getByRole('region', { name: 'Servers to share' }).waitFor({ timeout: 15_000 })
    await page.waitForTimeout(500)
    await page.getByRole('region', { name: 'Servers to share' }).screenshot({ path: `${OUT}/05-share-all-list.png` })
    await page.getByRole('button', { name: 'Cancel' }).click()

    // The checking state: one launch read held open.
    pending.delay = true
    await page.goto(`${BASE}/`, { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(1500)
    await page.evaluate(() => {
      window.history.pushState({}, '', '/developer?tab=mcp-pool')
      window.dispatchEvent(new PopStateEvent('popstate'))
    })
    const taskei = page.getByRole('button', { name: 'Share taskei-mcp' })
    await taskei.waitFor({ timeout: 30_000 })
    await page.waitForTimeout(800)
    await page.locator('li').filter({ has: taskei }).screenshot({ path: `${OUT}/07-checking.png` })
    pending.delay = false
    await context.close()
  }
  console.log(`wrote ${OUT}`)
} finally {
  await browser.close()
}
