/**
 * Screenshot harness for the prompt-cache hit rate in the context-usage popover.
 *
 * The popover gains a group under the window-token rows: the share of the
 * session's input tokens served from the prompt cache, with its basis spelled
 * out so the percentage is not read as a share of the context window. Three
 * states carry the whole design:
 *
 *  - `with-row`: the backend reports cache counts, so the group renders. Shot in
 *    dark and light, and once in Spanish, one of the longest labels of the set.
 *  - `without-row`: the backend reports input tokens but no cache counts (the
 *    default backend), so the popover renders exactly its other rows. Neither a
 *    0% row nor a notice appears.
 *  - `read-failed`: the crew-log read fails, so the popover says so in an inline
 *    error notice, with no hand-off because the composer draft is unsaved.
 *
 * Runs the REAL built SPA (website/dist) with every /api/** call answered from
 * fixtures, gateway-free. Labels are read from the CATALOGS, so a key rename
 * breaks the capture loudly instead of silently screenshotting the wrong element.
 * Every frame asserts what it claims before it is shot.
 *
 * Usage: node scripts/capture-cache-hit-rate.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/17985-cache-hits'
mkdirSync(OUT, { recursive: true })

const LOCALES = fileURLToPath(new URL('../src/i18n/locales/', import.meta.url))
const chatInput = lang => {
  const ci = JSON.parse(readFileSync(`${LOCALES}${lang}.json`, 'utf-8')).components.chatInput
  const words = {
    label: ci.cache_hit_rate,
    basis: ci.cache_hit_rate_basis,
    failed: ci.cache_hit_rate_load_failed,
    window: ci.context_window,
    contextUsage: ci.context_usage,
  }
  if (Object.values(words).some(w => !w)) throw new Error(`components.chatInput cache_hit_rate keys missing in ${lang} -- renamed?`)
  return words
}

const SLOT = 'cache-hit-demo'
const now = Math.floor(Date.now() / 1000)

const slots = [{
  key: SLOT,
  title: 'Long investigation session',
  running: false,
  last_message: 'Summarised the findings so far.',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  modified: now,
  source_links: [],
  source_links_total: 0,
}]

const detail = {
  running: false,
  has_more: false,
  total: 2,
  queue: [],
  messages: [
    { role: 'user', ts: now - 600, content: 'Why does the second turn feel faster?' },
    { role: 'assistant', ts: now - 30, content: 'Most of the prompt is read back from the cache.' },
  ],
  // Seeds the context gauge so the chip renders gateway-free: 42% of 200K.
  context_pct: 42,
  context_used_tokens: 84_000,
  context_window_tokens: 200_000,
}

/** The five-fold read of a crew log, with only the `usage` fold filled. */
const projections = tokens => ({
  projections: {
    usage: { name: 'usage', seq: 1, revision: 1, value: tokens ? { turns: { tokens_reported: 3 }, tokens } : {} },
  },
  unit: 'unit-1',
  resolved: true,
  writes_drained: true,
  recording: true,
})

// 750 cache reads of 1,000 input tokens in all: a 75% hit rate.
const REPORTING = { input: 100, output: 50, cache_read: 750, cache_write: 150 }
// Input tokens but no cache counts: a backend that does not report cache.
const NO_CACHE_COUNTS = { input: 400, output: 90, cache_read: 0, cache_write: 0 }

const until = async (cond, what, ms = 20000) => {
  const t0 = Date.now()
  while (!cond()) {
    if (Date.now() - t0 > ms) throw new Error(`timed out waiting for ${what}`)
    await new Promise(r => setTimeout(r, 50))
  }
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()

  async function shoot({ name, mode, theme = 'dark', lang = 'en' }) {
    const words = chatInput(lang)
    const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 2 })
    const page = await context.newPage()
    logPageProblems(page)
    let reads = 0
    await stubDashboardApi(page, {
      slots,
      theme,
      localStorageEntries: lang === 'en' ? null : { 'mc-lang': lang },
      extra: async (path, route) => {
        if (path === `/api/sessions/${SLOT}/crew-log/projections`) {
          reads += 1
          if (mode === 'read-failed') await json(route, { error: 'crew log unavailable' }, 500)
          else await json(route, projections(mode === 'with-row' ? REPORTING : NO_CACHE_COUNTS))
          return true
        }
        if (path === `/api/chat/slots/${SLOT}/autocompact`) {
          await json(route, { pct: null, global_pct: 70, min: 5, max: 90 })
          return true
        }
        if (path.startsWith('/api/chat/slots/')) { await json(route, detail); return true }
        return false
      },
    })

    await page.goto(`${base}/chat?sid=${encodeURIComponent(SLOT)}`, { waitUntil: 'domcontentloaded' })
    const ctxButton = page.getByLabel(words.contextUsage, { exact: true })
    await ctxButton.waitFor({ timeout: 20000 })
    await ctxButton.click()
    const popover = page.getByText(words.window, { exact: true }).locator('xpath=ancestor::div[contains(@class,"w-52")][1]')
    await popover.waitFor({ timeout: 10000 })
    const group = page.getByTestId('context-cache-hits')
    const notice = page.getByTestId('cache-hit-rate-load-error')

    if (mode === 'with-row') {
      await group.waitFor({ timeout: 20000 })
      // The percentage is formatted by the page's locale (Spanish writes `75 %`).
      const norm = s => s.replace(/\s+/g, ' ')
      const percent = new Intl.NumberFormat(lang, { style: 'percent' }).format(0.75)
      const text = norm(await group.innerText())
      for (const want of [words.label, percent, words.basis]) {
        if (!text.includes(norm(want))) throw new Error(`${name}: group reads ${JSON.stringify(text)}, expected ${JSON.stringify(want)}`)
      }
      if (await notice.count()) throw new Error(`${name}: an error notice rendered beside the row`)
    } else if (mode === 'without-row') {
      // The popover asked for the read, and it answered with no cache counts.
      await until(() => reads >= 1, 'the projections read')
      await page.waitForTimeout(500)
      if (await group.count()) throw new Error(`${name}: a cache row rendered for a backend with no cache counts`)
      if (await notice.count()) throw new Error(`${name}: an error notice rendered for a successful read`)
    } else {
      // The app retries a failed read once after a second before it reports it.
      await notice.waitFor({ timeout: 20000 })
      const text = await notice.innerText()
      if (!text.includes(words.failed)) throw new Error(`${name}: notice reads ${JSON.stringify(text)}`)
      if (await notice.getByRole('button').count()) throw new Error(`${name}: the notice offers a hand-off over an unsaved draft`)
      if (await group.count()) throw new Error(`${name}: a cache row rendered beside the failure`)
    }

    await page.waitForTimeout(400) // popover slide-up settles
    // Frame the popover together with the chip that opened it.
    const [p, c] = [await popover.boundingBox(), await ctxButton.boundingBox()]
    const pad = 16
    const x = Math.max(0, Math.min(p.x, c.x) - pad)
    const y = Math.max(0, Math.min(p.y, c.y) - pad)
    const right = Math.max(p.x + p.width, c.x + c.width) + pad
    const bottom = Math.max(p.y + p.height, c.y + c.height) + pad
    await page.screenshot({ path: `${OUT}/${name}.png`, clip: { x, y, width: right - x, height: bottom - y } })
    console.log(`captured ${OUT}/${name}.png`)
    await context.close()
  }

  await shoot({ name: 'with-row-dark', mode: 'with-row', theme: 'dark' })
  await shoot({ name: 'with-row-light', mode: 'with-row', theme: 'light' })
  await shoot({ name: 'with-row-es', mode: 'with-row', theme: 'dark', lang: 'es' })
  await shoot({ name: 'without-row', mode: 'without-row', theme: 'dark' })
  await shoot({ name: 'read-failed', mode: 'read-failed', theme: 'dark' })

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
