/** Real-browser evidence for the two fork entries in an assistant message's More menu.
 *
 * Drives website/capture/fork-menu.html: the real AssistantMessage over the literal ChatPage row
 * chain, with the props ChatPage passes for a reply whose fork point resolves on the server
 * (`forkIndex` + `forkMessageId`). `onFork` records its calls. Asserted, then shot:
 *
 *   setting-on       Tail-only Fork on: "Fork without earlier messages" (the fork icon), then
 *                    "Fork with earlier messages" (the history icon)
 *   setting-off      Tail-only Fork off: the menu is unchanged, "Fork conversation from here" only
 *   fork-in-flight   after choosing Fork with earlier messages: that entry spins and is dimmed, the
 *                    plain entry is dimmed with no spinner, and clicking either entry starts no second fork
 * Also asserted with hit-tested clicks (elementFromPoint at the item centre, then a real mouse click):
 *   plain entry -> onFork(1, 'm2')   history entry -> onFork(1, 'm2', 'head')
 *
 * Usage (website/ is the vite root):
 *   npx vite --host 127.0.0.1 --port 6874 --strictPort
 *   node scripts/capture-18611-fork-menu.mjs http://127.0.0.1:6874 ../temp-screenshots/fork-menu
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'

const BASE = process.argv[2] || 'http://127.0.0.1:6874'
const OUT = resolve(process.argv[3] || '../temp-screenshots/fork-menu')
mkdirSync(OUT, { recursive: true })
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0
const check = (l, ok) => { console.log(`${l} => ${ok ? 'OK' : 'FAIL'}`); if (!ok) failures++ }

async function open(query) {
  const ctx = await browser.newContext({ viewport: { width: 760, height: 600 }, deviceScaleFactor: 2 })
  ctx.setDefaultTimeout(120000)
  const page = await ctx.newPage()
  const errors = []; page.on('pageerror', e => errors.push(String(e)))
  await page.goto(`${BASE}/capture/fork-menu.html?${query}`, { waitUntil: 'networkidle', timeout: 180000 })
  await page.waitForSelector('[data-role="assistant"]'); await page.waitForTimeout(400)
  return { ctx, page, errors }
}
const forkIds = page => page.getByRole('menuitem').evaluateAll(els => els.map(e => e.getAttribute('data-testid')).filter(id => id && id.startsWith('fork-')))
const text = (page, id) => page.getByTestId(id).innerText().then(t => t.trim())
const hasIcon = (page, id, name) => page.getByTestId(id).evaluate((el, n) => !!el.querySelector(`svg.lucide-${n}`), name)
async function openMore(page) {
  const reply = page.locator('[data-role="assistant"]').first()
  await reply.hover({ position: { x: 120, y: 30 } }); await page.waitForTimeout(800)
  await reply.getByTestId('assistant-more-actions').click(); await page.waitForTimeout(700)
  await page.waitForSelector('[role="menu"]')
}
/** The item must be what a real pointer lands on at its centre. */
async function hitTested(page, testId) {
  const box = await page.getByTestId(testId).boundingBox()
  const x = box.x + box.width / 2, y = box.y + box.height / 2
  return page.evaluate(([px, py, id]) => !!document.elementFromPoint(px, py)?.closest(`[data-testid="${id}"]`), [x, y, testId])
}
const calls = page => page.evaluate(() => window.__forkCalls)

// 1 setting on
{
  const { ctx, page, errors } = await open('theme=dark&choice=on')
  await openMore(page)
  check('[on] fork entries = plain, then Fork with earlier messages', JSON.stringify(await forkIds(page)) === JSON.stringify(['fork-from-here', 'fork-with-history']))
  check('[on] the plain entry says it leaves earlier messages out', (await text(page, 'fork-from-here')) === 'Fork without earlier messages')
  check('[on] the other entry says it keeps them', (await text(page, 'fork-with-history')) === 'Fork with earlier messages')
  check('[on] the plain entry keeps the fork icon', await hasIcon(page, 'fork-from-here', 'git-fork') && !(await hasIcon(page, 'fork-from-here', 'history')))
  check('[on] the other entry has its own icon', await hasIcon(page, 'fork-with-history', 'history') && !(await hasIcon(page, 'fork-with-history', 'git-fork')))
  check('[on] no entry is held', (await page.getByTestId('fork-with-history').getAttribute('aria-disabled')) === null && (await page.getByTestId('fork-from-here').getAttribute('aria-disabled')) === null)
  await page.screenshot({ path: resolve(OUT, 'fork-menu-setting-on.png') })
  check('[on] no page errors', errors.length === 0); await ctx.close()
}
// 2 setting off
{
  const { ctx, page, errors } = await open('theme=dark&choice=off')
  await openMore(page)
  check('[off] fork entries = the plain entry only', JSON.stringify(await forkIds(page)) === JSON.stringify(['fork-from-here']))
  check('[off] the plain entry keeps its label', (await text(page, 'fork-from-here')) === 'Fork conversation from here')
  check('[off] the plain entry keeps the fork icon', await hasIcon(page, 'fork-from-here', 'git-fork'))
  await page.screenshot({ path: resolve(OUT, 'fork-menu-setting-off.png') })
  check('[off] no page errors', errors.length === 0); await ctx.close()
}
// 3 fork in flight
{
  const { ctx, page, errors } = await open('theme=dark&choice=on&inflight=1')
  await openMore(page)
  check('[inflight] Fork with earlier messages is hit-tested', await hitTested(page, 'fork-with-history'))
  await page.getByTestId('fork-with-history').click(); await page.waitForTimeout(400)
  check('[inflight] the chosen entry calls onFork(1, m2, head) once', JSON.stringify(await calls(page)) === JSON.stringify([[1, 'm2', 'head']]))
  await openMore(page)
  check('[inflight] Fork with earlier messages is held', (await page.getByTestId('fork-with-history').getAttribute('aria-disabled')) === 'true')
  check('[inflight] the plain entry is held too', (await page.getByTestId('fork-from-here').getAttribute('aria-disabled')) === 'true')
  check('[inflight] the chosen entry shows the spinner', await hasIcon(page, 'fork-with-history', 'loader-circle') && !(await hasIcon(page, 'fork-with-history', 'history')))
  check('[inflight] the plain entry shows no spinner', !(await hasIcon(page, 'fork-from-here', 'loader-circle')) && await hasIcon(page, 'fork-from-here', 'git-fork'))
  await page.getByTestId('fork-from-here').click({ force: true }); await page.waitForTimeout(250)
  await page.getByTestId('fork-with-history').click({ force: true }); await page.waitForTimeout(250)
  check('[inflight] clicking either held entry starts no second fork', (await calls(page)).length === 1)
  // Park the pointer outside the menu so no entry carries a hover highlight in the still.
  await page.mouse.move(730, 30); await page.waitForTimeout(500)
  const held = await page.getByTestId('fork-with-history').evaluate(el => ({ highlighted: el.hasAttribute('data-highlighted'), dimmed: getComputedStyle(el.firstElementChild).opacity }))
  check('[inflight] the held entry is dimmed', held.dimmed === '0.5')
  check('[inflight] the plain entry is dimmed too', (await page.getByTestId('fork-from-here').evaluate(el => getComputedStyle(el.firstElementChild).opacity)) === '0.5')
  check('[inflight] no entry is highlighted in the still', !held.highlighted)
  await page.screenshot({ path: resolve(OUT, 'fork-menu-fork-in-flight.png') })
  check('[inflight] no page errors', errors.length === 0); await ctx.close()
}
// 4 each entry keeps its call shape (not shot)
{
  const { ctx, page, errors } = await open('theme=dark&choice=on')
  await openMore(page)
  check('[plain] the plain entry is hit-tested', await hitTested(page, 'fork-from-here'))
  await page.getByTestId('fork-from-here').click(); await page.waitForTimeout(300)
  check('[plain] calls onFork(1, m2) with no direction', JSON.stringify(await calls(page)) === JSON.stringify([[1, 'm2']]))
  check('[plain] no page errors', errors.length === 0); await ctx.close()
}
await browser.close()
console.log(failures ? `${failures} check(s) FAILED` : 'all checks OK')
process.exit(failures ? 1 : 0)
