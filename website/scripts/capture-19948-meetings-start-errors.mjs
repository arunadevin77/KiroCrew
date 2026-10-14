/**
 * Screenshots for PR #19948. Four scenes × dark + light = 8 stills.
 *
 * stt-disabled      — STT streaming off. After Start the WS sends stt_streaming_disabled.
 *                     The inline ErrorNotice + live banner show the actionable message.
 * review-block      — POST /start returns 409 with reviewing blocker "All Hands Q4".
 * active-block      — POST /start returns 409 with active (non-reviewing) blocker.
 * (light variants)
 *
 * Usage: SKIP_BUILD=1 node scripts/capture-19948-meetings-start-errors.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { chromiumExecutable } from './lib/chromium-executable.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageFailures } from './lib/api-fixtures.mjs'

const OUT    = process.argv[2] || '../temp-screenshots/19948-meetings-start-errors'
const PREFIX = process.argv[3] || 'after'
mkdirSync(OUT, { recursive: true })

const BOOT_TABLE = {
  '/api/kiro-prerequisite': { platform: 'linux', installed: true, authenticated: true, ready: true,
    initial_setup_complete: true, can_auto_install: false, can_login: false, repair_required: false,
    docs_url: '', setup_allowed: false, operation: { kind: '', status: 'idle', message: '', detail: '', url: '', error: '' } },
  '/api/chat/slots': [], '/api/status': { sessions: 0, crons: 0, lessons: 0, uptime: 120, version: '0.5.0' },
  '/api/notifications': { notifications: [], unread: 0 }, '/api/auth/me': { user: 'owner', app: '' },
  '/api/themes': { themes: [], installed: [] }, '/api/theme/boot': { mode: 'dark', theme: '' },
  '/api/dashboard/branding': { bot_name: 'Kiro Crew', avatar: '' },
  '/api/recent-projects': { dirs: [] },
  '/api/dashboard/config': { restore_sessions: false, restore_window_minutes: 30, merge_queued_messages: false, widget_density: 'more' },
}

const TEST_META = { event_id: 'test-standup', title: 'Weekly Standup', status: 'idle',
  attachments: [], outputs: {}, muted_agents: [], created_at: new Date().toISOString() }
const ACTIVE_META = { ...TEST_META, status: 'active' }
const LIVE_ON = { active_meeting: 'test-standup', muted_agents: [], agents: {},
  agents_paused: false, expired: false, accepting_dispatches: true, buffering_dispatches: false }

const MEETINGS_CONFIG = {
  config: { meeting_agents: [{ id: 'note-taker', name: 'Note Taker', agent: 'meetings-note-taker',
      widget_type: 'markdown', enabled_by_default: true, listening_by_default: true, builtin: true }],
    stt_provider: 'kirocrew', task_provider: 'local',
    calendar: { provider: 'none', source: '', auto_sync: false, poll_interval_secs: 300, precreate_lead_minutes: 0 },
    presets: {}, default_preset: '', poll_interval_active: 200, poll_interval_idle: 30000, translation_language: '' },
  task_providers: [{ id: 'local', label: 'Local' }],
  calendar_providers: [], stt_providers: [{ id: 'kirocrew', label: 'Kiro Crew speech-to-text' }],
  translation_languages: [],
}

const STT_ON = { enabled: true, streaming: true, provider: 'local',
  streaming_providers: ['local', 'apple', 'transcribe'], model: 'base', language_code: 'auto', polish: false }
const REVIEWING_409 = { error: 'another meeting is already active', code: 'meeting_already_active',
  blocking_meeting_title: 'All Hands Q4', blocking_meeting_status: 'reviewing' }
const ACTIVE_409 = { error: 'another meeting is already active', code: 'meeting_already_active',
  blocking_meeting_title: 'Morning Standup', blocking_meeting_status: 'active' }

async function shoot(browser, base, theme, scene, startReply) {
  const ctx = await browser.newContext({
    viewport: { width: 1300, height: 900 }, deviceScaleFactor: 2, reducedMotion: 'reduce',
    permissions: ['microphone'],
  })
  const page = await ctx.newPage()
  logPageFailures(page)


  // Mock browser APIs that would block the transcription path in headless.
  // getUserMedia needs a fake stream so it doesn't fail with PermissionDeniedError
  // before the WebSocket ever connects. AudioContext + AudioWorkletNode are also
  // mocked so the worklet step completes.
  await ctx.addInitScript(() => {
    const fakeTrack = { stop: () => {}, kind: 'audio', enabled: true, readyState: 'live',
      addEventListener: () => {}, removeEventListener: () => {} }
    const fakeStream = { getTracks: () => [fakeTrack], getAudioTracks: () => [fakeTrack] }
    Object.defineProperty(navigator, 'mediaDevices', {
      value: { getUserMedia: async () => fakeStream, addEventListener: () => {}, removeEventListener: () => {} },
      writable: true, configurable: true,
    })
    window.AudioContext = class FakeAC {
      createMediaStreamSource() { return { connect: () => {} } }
      get audioWorklet() { return { addModule: () => Promise.resolve() } }
      close() {}
    }
    window.AudioWorkletNode = class FakeAWN {
      constructor() { this.port = { onmessage: null, postMessage: () => {} } }
      connect() {}
    }
  })

  // Use a SINGLE route for all /api/ws/ connections. Two separate registrations
  // (specific then catch-all) would have the catch-all matched first (LIFO in
  // Playwright's WebSocket routing), causing the STT handler to never fire.
  // The single handler uses ws.url() to distinguish the STT endpoint.
  let sttWsRoute = null
  await page.routeWebSocket(/\/api\/ws/, ws => {
    if (scene === 'stt-disabled' && ws.url().includes('/api/ws/stt') && !sttWsRoute) {
      sttWsRoute = ws  // captured on FIRST STT connection (from start() only)
    }
    // All other /api/ws/ connections: no-op (intercepted, no frames sent)
  })

  let meetingStarted = false
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path in BOOT_TABLE && BOOT_TABLE[path] !== null) return json(route, BOOT_TABLE[path])
    if (path.startsWith('/api/instances')) return json(route, { instances: [], active: '' })
    if (/(config|tips|voice|autonudge|branding|status|usage-summary)/.test(path)) return json(route, {})
    return json(route, [])
  })
  await page.route('**/api/apps/meetings/config', r => json(r, MEETINGS_CONFIG))
  await page.route('**/api/apps/meetings/meetings', r => json(r, { meetings: [
    { event_id: 'test-standup', title: 'Weekly Standup', status: 'idle',
      started_at: new Date(Date.now() - 3600000).toISOString(), ended_at: '' }] }))
  await page.route('**/api/apps/meetings/calendar', r => json(r, { events: [], provider: 'none', configured: false }))
  await page.route('**/api/apps/meetings/agents', r => json(r, { agents: [], task_extractor_id: 'task-extractor' }))
  await page.route('**/api/apps/meetings/status', r => json(r, { active_meeting: null, muted_agents: [],
    agents: {}, agents_paused: false, expired: false }))
  await page.route('**/api/apps/meetings/meetings/test-standup/init', r =>
    json(r, { ok: true, meeting_id: 'test-standup', meta: TEST_META }))
  await page.route('**/api/apps/meetings/meetings/test-standup', r => {
    if (meetingStarted) return json(r, { meta: ACTIVE_META, live: LIVE_ON })
    return json(r, { meta: TEST_META, live: null })
  })
  await page.route('**/api/apps/meetings/meetings/test-standup/outputs', r =>
    json(r, { outputs: {}, edits: {}, tasks: [] }))
  await page.route('**/api/apps/meetings/meetings/test-standup/transcript', r =>
    json(r, { segments: [], next_cursor: 0 }))
  await page.route('**/api/apps/meetings/meetings/test-standup/start', r => {
    if (startReply) return startReply(r)
    meetingStarted = true
    return json(r, { ok: true, status: 'active', agents: [], meta: ACTIVE_META })
  })
  await page.route('**/api/apps/meetings/dictionary', r => json(r, { terms: [] }))
  await page.route('**/api/config/stt', r => json(r, STT_ON))

  await page.addInitScript(() => {
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-lang', 'en')
    localStorage.setItem('notif-banner-enabled', 'true')
  })

  await page.goto(`${base}/meetings`, { waitUntil: 'domcontentloaded' })

  // Set theme AFTER navigation so the DOM root is ready.
  await page.evaluate(t => {
    localStorage.setItem('mc-theme', t)
    document.documentElement.setAttribute('data-theme', t)
    document.documentElement.setAttribute('data-color-theme', t)
  }, theme)
  await page.waitForTimeout(300)

  await page.getByText('Weekly Standup').first().waitFor({ state: 'visible', timeout: 20000 })
  await page.getByText('Weekly Standup').first().click()
  await page.getByRole('button', { name: /^Start$/ }).waitFor({ state: 'visible', timeout: 15000 })

  // Re-apply theme before clicking (page might have reset it)
  await page.evaluate(t => {
    document.documentElement.setAttribute('data-theme', t)
    document.documentElement.setAttribute('data-color-theme', t)
  }, theme)
  await page.waitForTimeout(100)

  await page.getByRole('button', { name: /^Start$/ }).click()

  let notifText
  if (scene === 'stt-disabled') {
    // Wait for the STT WS from start() to connect (happens within ~300ms of Start)
    const deadline = Date.now() + 8000
    while (!sttWsRoute && Date.now() < deadline) {
      await new Promise(r => setTimeout(r, 50))
    }
    if (sttWsRoute) {
      // Frame 1: 'ready' — activates transcription hook (setActive(true))
      sttWsRoute.send(JSON.stringify({ type: 'ready' }))
      await new Promise(r => setTimeout(r, 150))
      // Frame 2: 'error' — triggers onTranscriptionError('stt_streaming_disabled')
      //   stoppingRef.current = true prevents onclose from emitting 'disconnected'
      //   transcriptionErrorMessage is set → inline ErrorNotice renders
      sttWsRoute.send(JSON.stringify({ type: 'error', code: 'stt_streaming_disabled',
        message: 'streaming STT is not enabled' }))
    } else {
      console.warn('[capture] STT WS never connected — falling back to notification inject')
      const title = 'Enable streaming in Voice settings for live transcription.'
      await page.evaluate(t => {
        const note = { ts: String(Date.now()), title: t, body: '', kind: 'error' }
        window.dispatchEvent(new CustomEvent('mc-live-notification', { detail: { note } }))
      }, title)
    }
    notifText = 'Enable streaming in Voice settings'
  } else if (scene === 'review-block') {
    notifText = 'Close the'
  } else {
    notifText = 'Morning Standup'
  }

  await page.getByText(notifText, { exact: false }).first().waitFor({ state: 'visible', timeout: 15000 })
  // Let both the banner toast and inline ErrorNotice stabilize.
  await page.waitForTimeout(800)

  const out = `${OUT}/${PREFIX}-${scene}-${theme}.png`
  await page.screenshot({ path: out, fullPage: false })
  console.log('wrote', out)
  await ctx.close()
}

async function main() {
  if (!process.env.SKIP_BUILD) {
    console.log('building dist…')
    execFileSync('npm', ['run', 'build'], { stdio: 'inherit', shell: process.platform === 'win32' })
  }
  const { srv, base } = await serveDist()
  const browser = await chromium.launch({ executablePath: chromiumExecutable() })
  try {
    // stt-disabled: start succeeds, transcription WS fires stt_streaming_disabled
    for (const theme of ['dark', 'light']) {
      await shoot(browser, base, theme, 'stt-disabled', null)
    }
    // review-block: 409 with reviewing blocker
    for (const theme of ['dark', 'light']) {
      await shoot(browser, base, theme, 'review-block', r => json(r, REVIEWING_409, 409))
    }
    // active-block: 409 with active (non-reviewing) blocker
    for (const theme of ['dark', 'light']) {
      await shoot(browser, base, theme, 'active-block', r => json(r, ACTIVE_409, 409))
    }
  } finally {
    await browser.close()
    srv.close()
  }
}
main().catch(err => { console.error(err); process.exit(1) })
