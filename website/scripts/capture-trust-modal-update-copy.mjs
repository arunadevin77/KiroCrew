/**
 * Screenshot harness for the trust dialog's UPDATE path.
 *
 * An Update of an already-installed app re-runs the registry install, and when
 * the execution policy refuses it the same consent modal opens. That path
 * carries its own outcome line, confirm label, Cancel line and failure
 * headlines: the enable path's say trusting "is what turns it on" and that the
 * app "stays in place, switched off" (the app being updated may be on), and
 * the install path's say nothing is installed (it is). The failure headlines
 * name the retry without calling it trusting again, and the not-saved one
 * points at the agent hand-off rather than at logs it does not locate.
 *
 *   04 working    → the grant is in flight: the confirm shows the update's
 *                   pending label
 *   05 offline    → the grant failed while the gateway is unreachable: the
 *                   next step says to retry once it reconnects, and no Ask
 *                   the agent button shows (AFTER only)
 *
 * Runs the REAL built SPA behind the shared static server and answers every
 * /api/** call from fixtures, the SSE install stream included, so no gateway
 * and no kiro-cli is needed. Three states, one flow each, from a fresh page:
 *
 *   01 consent    → the modal as opened from Update, before any attempt
 *   02 not-saved  → the grant itself failed: nothing was saved (`update_failed_generic`)
 *   03 trusted    → the grant landed, the update failed, the app is still there
 *                   so the grant stands (`update_failed`)
 *
 * BEFORE (`before` prefix, a dist built without the update outcome line)
 * asserts the consent state reads like a first install's: no outcome line and
 * the install confirm label, with the failure headlines' re-confirm and logs
 * clauses. AFTER asserts the update copy and the absence of both other paths'
 * copy.
 * Each assertion runs before its frame is taken, so a screenshot can never show
 * a state the run did not verify.
 *
 * Usage: node scripts/capture-trust-modal-update-copy.mjs [outDir] [prefix] [dist]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist, DEFAULT_DIST } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'
import { chromiumExecutable } from './lib/chromium-executable.mjs'

const OUT = process.argv[2] || '../temp-screenshots/trust-modal-update-copy'
const PREFIX = process.argv[3] || 'after'
const DIST = process.argv[4] || DEFAULT_DIST

mkdirSync(OUT, { recursive: true })

const APP = 'agent-dashboard'
const DISPLAY = 'Agent Dashboard'
const REPO = 'https://git.example.test/apps/agent-dashboard.git'
const UPDATE_REASON = 'build failed (exit 1): npm install: ETIMEDOUT registry.npmjs.org'
const TRUST_REASON = 'trust store unavailable: read-only file system'

/** The enable path's copy, which the Update path must NOT show (and did, BEFORE). */
const ENABLE_ON_CANCEL = 'stays in place, switched off'
const ENABLE_ON_CANCEL_GRANTED = `Cancel keeps \u201c${DISPLAY}\u201d trusted and switched off`
const ONE_SWITCH = `Trusting \u201c${DISPLAY}\u201d is what turns it on.`
/** The install path's copy, which the Update path must not show either. */
const INSTALL_ON_CANCEL = 'nothing is installed'
const INSTALL_OUTCOME = 'it installs switched off'
const INSTALL_FAILED = 'could not be installed'
/** The Update path's own copy (AFTER). */
const UPDATE_ON_CANCEL = `Cancel changes nothing: \u201c${DISPLAY}\u201d stays as it is, on the version you have, and is not trusted.`
const UPDATE_ON_CANCEL_GRANTED = `Cancel keeps \u201c${DISPLAY}\u201d trusted, and the update does not run again`
const UPDATE_GENERIC = `\u201c${DISPLAY}\u201d could not be updated, and trust was not saved.`
const UPDATE_GRANTED = `\u201c${DISPLAY}\u201d is now trusted, but it could not be updated.`
const RETRY_BUTTON = 'Trust and try again'
const UPDATE_OUTCOME = `Trusting \u201c${DISPLAY}\u201d updates it: the new version replaces the one you have.`
const CONFIRM_UPDATE = 'Trust this app and update'
const CONFIRM_INSTALL = 'Trust this app'
const RECONFIRM = 're-confirms the trust you already gave'
const RETRY_CLAUSE = 'Your trust is already saved, so Trust and try again asks for no new trust and just tries again'
const OFFLINE_NEXT = 'Kiro Crew is not connected right now, so try again once it reconnects.'
const LOGS_CLAUSE = "'s logs"
const ASK_CLAUSE = 'If it fails again, choose Ask the agent to find out why.'
const WORKING_UPDATE = 'Trusting and updating...'
const WORKING_INSTALL = 'Trusting...'

const INSTALLED_APP = {
  name: APP, displayName: DISPLAY, version: '3.2.6', enabled: true,
  origin: 'registry', resources: 'gateway', lifecycle: 'gateway', source: `registry:${APP}`,
  trustRepository: REPO, installedAt: '2026-09-01T10:00:00Z',
  manifest: {
    name: APP, version: '3.2.6', displayName: DISPLAY,
    description: 'A dashboard for your agents, with its own FastAPI backend.',
    backend: { entryPoint: 'server.py', type: 'asgi' },
  },
}
const REGISTRY_ROW = {
  name: APP, displayName: DISPLAY, version: '3.3.0',
  description: INSTALLED_APP.manifest.description,
  author: 'example-apps', repo: REPO, gitUrl: REPO, trustRepository: REPO,
  tags: ['dashboard'], installed: true, enabled: true, origin: 'registry',
  updateAvailable: true,
  manifest: { name: APP, version: '3.3.0', displayName: DISPLAY, backend: { entryPoint: 'server.py', type: 'asgi' } },
}

const sse = (payload) => `event: done\ndata: ${JSON.stringify(payload)}\n\n`
const DENIED = {
  ok: false, name: APP, code: 'app_execution_denied',
  error: `blocked by execution policy: App ${APP} is not trusted to run its own code.`,
  log: '',
}
const FAILED_UPDATE = { ok: false, name: APP, error: UPDATE_REASON }

/**
 * Per scenario: what the install stream answers per attempt, and whether the
 * trust grant itself is refused. The app is installed and enabled throughout,
 * so the rollback probe always finds it and a landed grant stands.
 */
const SCENARIOS = {
  consent: { attempts: [DENIED] },
  notSaved: { attempts: [DENIED], trustFails: true },
  trusted: { attempts: [DENIED, FAILED_UPDATE] },
  working: { attempts: [DENIED], trustHangs: true },
  offline: { attempts: [DENIED], trustFails: true, offline: true },
}

async function openUpdateModal(context, base, scenario) {
  const page = await context.newPage()
  logPageProblems(page)
  let installs = 0
  let untrusts = 0
  page.rollbacks = () => untrusts
  await stubDashboardApi(page, {
    extra: async (path, route) => {
      if (path === '/api/apps') { await route.fulfill({ json: [INSTALLED_APP] }); return true }
      if (path === `/api/apps/${APP}`) { await route.fulfill({ json: INSTALLED_APP }); return true }
      if (path === '/api/apps/registry') {
        await route.fulfill({ json: { apps: [REGISTRY_ROW], serverPlatform: { os: 'darwin', arch: 'arm64' } } })
        return true
      }
      if (path === '/api/apps/registries') { await route.fulfill({ json: { registries: [] } }); return true }
      // The grant and its revocation share one route (POST / DELETE
      // /api/security/trusted-apps/<name>); the method tells them apart.
      if (path === `/api/security/trusted-apps/${APP}`) {
        if (route.request().method() === 'DELETE') {
          untrusts += 1
          await route.fulfill({ json: { apps: [], allowAll: false, ineffective: [], disabled: false } })
          return true
        }
        if (SCENARIOS[scenario].trustHangs) {
          await new Promise((resolve) => setTimeout(resolve, 20000))
          await route.fulfill({ status: 500, json: { error: TRUST_REASON } }).catch(() => {})
          return true
        }
        if (SCENARIOS[scenario].trustFails) {
          await route.fulfill({ status: 500, json: { error: TRUST_REASON } })
          return true
        }
        await route.fulfill({ json: { apps: [APP], allowAll: false, ineffective: [] } })
        return true
      }
      if (path === '/api/apps/registry/install-stream') {
        const { attempts } = SCENARIOS[scenario]
        const payload = attempts[Math.min(installs, attempts.length - 1)]
        installs += 1
        await route.fulfill({ status: 200, contentType: 'text/event-stream', body: sse(payload) })
        return true
      }
      return false
    },
  })
  // Registered after the stub's own handler, so it wins: every socket closes
  // at once and the dashboard sits in its disconnected state.
  if (SCENARIOS[scenario].offline) await page.routeWebSocket(/\/api\/ws/, (ws) => ws.close())
  await page.goto(`${base}/apps/detail/${APP}`, { waitUntil: 'domcontentloaded' })
  await page.getByText(DISPLAY).first().waitFor({ timeout: 15000 })
  await page.getByRole('button', { name: 'Update', exact: true }).first().click()
  const modal = page.locator('[role="dialog"]').filter({ hasText: /to run its own code\?/ })
  await modal.waitFor({ timeout: 15000 })
  await page.waitForTimeout(400)
  return { page, modal }
}

async function failThroughTheModal(page, modal) {
  const confirm = PREFIX === 'before' ? CONFIRM_INSTALL : CONFIRM_UPDATE
  await modal.getByRole('button', { name: confirm, exact: true }).click()
  await modal.getByRole('alert').waitFor({ timeout: 15000 })
  await modal.getByRole('button', { name: RETRY_BUTTON, exact: true }).waitFor({ timeout: 15000 })
  await page.waitForTimeout(600)
}

const fail = (msg, text) => { throw new Error(`${PREFIX}: ${msg}: ${JSON.stringify(text)}`) }

/** Shared: the Update path never carries the install path's sentences, in either build. */
function assertNoInstallCopy(body) {
  if (body.includes(INSTALL_ON_CANCEL)) fail('the update path must not say nothing is installed', body)
  if (body.includes(INSTALL_OUTCOME)) fail('the update path must not say the app installs switched off', body)
  if (body.includes(INSTALL_FAILED)) fail('the update path must not say the app could not be installed', body)
}

async function captureConsent(context, base) {
  const { page, modal } = await openUpdateModal(context, base, 'consent')
  const body = await modal.innerText()
  assertNoInstallCopy(body)
  if (PREFIX === 'before') {
    if (body.includes(UPDATE_OUTCOME)) fail('BEFORE: the consent state has no update outcome line', body)
    if (!await modal.getByRole('button', { name: CONFIRM_INSTALL, exact: true }).count()) fail('BEFORE: the confirm reads like a first install', body)
  } else {
    if (!body.includes(UPDATE_ON_CANCEL)) fail('the update path has its own Cancel line', body)
    if (body.includes(ENABLE_ON_CANCEL)) fail('the update path must not say the app is switched off', body)
    if (!body.includes(UPDATE_OUTCOME)) fail('the consent state says this is an update', body)
    if (!await modal.getByRole('button', { name: CONFIRM_UPDATE, exact: true }).count()) fail('the confirm names the update', body)
  }
  await modal.screenshot({ path: `${OUT}/${PREFIX}-01-update-consent.png` })
  await page.close()
}

async function captureNotSaved(context, base) {
  const { page, modal } = await openUpdateModal(context, base, 'notSaved')
  await failThroughTheModal(page, modal)
  const text = await modal.getByRole('alert').innerText()
  const body = await modal.innerText()
  assertNoInstallCopy(body)
  if (!text.includes(TRUST_REASON)) fail("the modal must render the server's reason beneath", text)
  if (PREFIX !== 'before' && !body.includes(UPDATE_OUTCOME)) fail('the outcome line stays after a failure: it says the retry still updates', body)
  if (PREFIX === 'before') {
    if (!text.includes(LOGS_CLAUSE)) fail('BEFORE: the not-saved headline points at logs', text)
  } else {
    if (text.includes(LOGS_CLAUSE)) fail('the not-saved headline must not point at logs', text)
    if (!text.includes(ASK_CLAUSE)) fail('the not-saved headline points at the agent hand-off', text)
    if (!text.includes(UPDATE_GENERIC)) fail('the not-saved headline says the app could not be updated', text)
    if (text.includes(ONE_SWITCH)) fail('the update path must not say trusting is what turns the app on', text)
    if (!body.includes(UPDATE_ON_CANCEL)) fail('the not-saved state keeps the update Cancel line', body)
    if (body.includes(ENABLE_ON_CANCEL)) fail('the update path must not say the app is switched off', body)
  }
  await modal.screenshot({ path: `${OUT}/${PREFIX}-02-update-not-saved-failure.png` })
  await page.close()
}

async function captureTrusted(context, base) {
  const { page, modal } = await openUpdateModal(context, base, 'trusted')
  await failThroughTheModal(page, modal)
  const text = await modal.getByRole('alert').innerText()
  const body = await modal.innerText()
  assertNoInstallCopy(body)
  if (!text.includes(UPDATE_REASON)) fail("the modal must render the server's reason beneath", text)
  if (page.rollbacks() !== 0) fail('the grant must stand: the app is installed, so nothing is rolled back', text)
  if (PREFIX !== 'before' && !body.includes(UPDATE_OUTCOME)) fail('the outcome line stays after a failure: it says the retry still updates', body)
  if (PREFIX === 'before') {
    if (!text.includes(RECONFIRM)) fail('BEFORE: the trusted headline says the retry re-confirms the trust', text)
  } else {
    if (text.includes(RECONFIRM)) fail('the trusted headline must not call the retry trusting again', text)
    if (!text.includes(RETRY_CLAUSE)) fail('the trusted headline says the retry keeps the trust already given', text)
    if (!text.includes(UPDATE_GRANTED)) fail('the trusted headline says the app is trusted but could not be updated', text)
    if (text.includes(ONE_SWITCH)) fail('the update path must not say trusting is what turns the app on', text)
    if (!body.includes(UPDATE_ON_CANCEL_GRANTED)) fail('the trusted state names what Cancel leaves: trusted, update not re-run', body)
    if (body.includes(ENABLE_ON_CANCEL_GRANTED)) fail('the update path must not say the app is switched off', body)
  }
  await modal.screenshot({ path: `${OUT}/${PREFIX}-03-update-trust-saved-failure.png` })
  await page.close()
}

async function captureWorking(context, base) {
  const { page, modal } = await openUpdateModal(context, base, 'working')
  const confirm = PREFIX === 'before' ? CONFIRM_INSTALL : CONFIRM_UPDATE
  await modal.getByRole('button', { name: confirm, exact: true }).click()
  const working = PREFIX === 'before' ? WORKING_INSTALL : WORKING_UPDATE
  await modal.getByRole('button', { name: working, exact: true }).waitFor({ timeout: 15000 })
  await page.waitForTimeout(400)
  await modal.screenshot({ path: `${OUT}/${PREFIX}-04-update-working.png` })
  await page.close()
}

async function captureOffline(context, base) {
  const { page, modal } = await openUpdateModal(context, base, 'offline')
  await failThroughTheModal(page, modal)
  const text = await modal.getByRole('alert').innerText()
  if (!text.includes(UPDATE_GENERIC)) fail('the not-saved headline says the app could not be updated', text)
  if (!text.includes(OFFLINE_NEXT)) fail('offline, the next step says to retry once it reconnects', text)
  if (text.includes('Ask the agent')) fail('offline, nothing names or shows the hidden hand-off', text)
  await modal.screenshot({ path: `${OUT}/${PREFIX}-05-update-not-saved-offline.png` })
  await page.close()
}

async function main() {
  const { srv, base } = await serveDist(DIST)
  const browser = await chromium.launch({ executablePath: chromiumExecutable() })
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1100 }, deviceScaleFactor: 1, serviceWorkers: 'block',
  })
  await captureConsent(context, base)
  await captureNotSaved(context, base)
  await captureTrusted(context, base)
  await captureWorking(context, base)
  if (PREFIX !== 'before') await captureOffline(context, base)
  await browser.close()
  srv.close()
  console.log(`Wrote ${OUT}/${PREFIX}-01-update-consent.png, ${PREFIX}-02-update-not-saved-failure.png, ${PREFIX}-03-update-trust-saved-failure.png, ${PREFIX}-04-update-working.png, ${PREFIX}-05-update-not-saved-offline.png`)
}

main().catch((err) => {
  console.error(err)
  process.exit(1)
})
