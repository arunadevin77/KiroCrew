/**
 * Screenshot harness for the Schedule-job secret-request warn note, captured
 * from the REAL running dashboard route (/schedule), both themes.
 *
 * What it proves: the actual "Agent requested secrets" note that
 * SchedulePage.tsx (JobSecretsPanel) renders is readable in dark mode and
 * unchanged in light mode after swapping `text-warn-fg` for `text-warn-text`
 * on its `bg-warn-subtle` container. The note is reached through the genuine
 * flow -- a real cron job carrying real `secret_env_pending` state, served by a
 * live gateway -- not a hand-built fixture of the markup.
 *
 * Prereqs (set up by the operator before running):
 *   - a gateway serving the built dist (dev-backend.sh, or a pod)
 *   - a script cron whose job carries secret_env_pending (seeded via the same
 *     CronService.update_job path cron_secret_request uses)
 *
 * Usage:
 *   node scripts/capture-schedule-secret-warn-realpane.mjs <base-with-token> [outDir]
 *   e.g. node scripts/capture-schedule-secret-warn-realpane.mjs \
 *          'http://localhost:6799?token=...' ../temp-screenshots/schedule-secret-warn
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2]
if (!BASE) throw new Error('pass the dashboard base URL (with ?token=...) as argv[1]')
const OUT = process.argv[3] || '../temp-screenshots/schedule-secret-warn'
mkdirSync(OUT, { recursive: true })

const origin = new URL(BASE).origin

const browser = await chromium.launch()
try {
  for (const theme of ['dark', 'light']) {
    const context = await browser.newContext({
      viewport: { width: 1440, height: 900 },
      deviceScaleFactor: 1,
      colorScheme: theme,
    })
    const page = await context.newPage()

    // Authenticate: loading the token URL seeds the dashboard session.
    await page.goto(BASE, { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(1200)
    // Pin the colour theme the real app reads from localStorage, then reload
    // so useTheme applies data-theme / data-mode to <html>.
    await page.evaluate((t) => localStorage.setItem('mc-color-theme', t), theme)
    await page.goto(`${origin}/schedule`, { waitUntil: 'domcontentloaded' })

    // Open the job whose pending secret request renders the note.
    await page.getByText('Dark warn note demo').first().click()
    const note = page.locator('[role="note"]').filter({ hasText: 'Agent requested secrets' })
    await note.waitFor({ state: 'visible', timeout: 15_000 })
    await note.scrollIntoViewIfNeeded()
    await page.waitForTimeout(400)

    // FULL application chrome: capture the whole viewport (nav rail, sidebar,
    // the Schedule page with the note in context), not just the note element.
    const file = `${OUT}/schedule-secret-warn-${theme}.png`
    await page.screenshot({ path: file })
    console.log(`${theme}: captured full app -> ${file}`)
    await context.close()
  }
} finally {
  await browser.close()
}
