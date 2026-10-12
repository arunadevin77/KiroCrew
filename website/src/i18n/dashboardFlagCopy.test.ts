/**
 * The Dynamic Dashboard preview flag's description must not name the chat
 * composer — a drift guard for issue #19400.
 *
 * ## The defect this guard closes
 *
 * The Feature Previews card for `mc-preview-dashboard` described the preview as
 * "a status row above the chat composer, a Dashboard view in the side panel and
 * on a crewmate's page, and an All Dashboards page". The dock component that
 * drew that row was retired, and this copy was not updated with it, so a user
 * who turned the flag on went looking above the composer for a feature that is
 * not there.
 *
 * Catalog parity cannot catch this: the key exists in all thirteen catalogs with
 * a well-formed, correctly translated value. The VALUE describes a surface the
 * app no longer has.
 *
 * ## The guard
 *
 * English is checked against the word it used, and every shipped locale against
 * the app's OWN translated name for the composer
 * (`pages.settings.chatPanel.composer`, the Chat panel's label), resolved per
 * locale rather than hardcoded here — the same cross-key shape
 * `instancesDisconnectHint.test.ts` uses, and for the same reason: a literal
 * copy of foreign prose in a test rots independently of the catalog it
 * describes. A stale translation misleads exactly as much as stale English,
 * which is why this is not an English-only guard. Its limit, stated plainly: a
 * translation that names the composer with a different noun than the Chat
 * panel's label is not caught. All twelve were corrected in the commit that
 * added this file; six of them trip this assertion on the pre-fix value.
 *
 * Two liveness assertions keep the comparison targets alive, the shape the
 * precedent above also carries: the card must still render its description from
 * `DESC_KEY`, and the Chat panel must still label the composer from
 * `COMPOSER_LABEL_KEY`. Without them, re-pointing either at a different key
 * leaves every assertion here passing against a catalog entry nothing renders.
 *
 * The generated pseudolocale is excluded: its values are accent-mangled per
 * key, so a substring match does not survive the transform. It regenerates from
 * the English catalogs anyway, so it cannot drift on its own.
 */

import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { describe, it, expect } from 'vitest'

import { CATALOGS } from './catalogs'
import { SUPPORTED_LANGUAGES } from './languages'

const DESC_KEY = 'pages.developer.featurePreviewsTab.dashboard_desc'
const COMPOSER_LABEL_KEY = 'pages.settings.chatPanel.composer'

const GENERATED = new Set(SUPPORTED_LANGUAGES.filter((l) => l.devOnly).map((l) => l.code))

/** Resolve a dotted key against a nested catalog object. */
function resolve(catalog: Record<string, unknown>, dotted: string): string | undefined {
  let node: unknown = catalog
  for (const part of dotted.split('.')) {
    if (typeof node !== 'object' || node === null) return undefined
    node = (node as Record<string, unknown>)[part]
  }
  return typeof node === 'string' ? node : undefined
}

function catalogOf(code: string): Record<string, unknown> {
  return (CATALOGS[code] as { translation: Record<string, unknown> }).translation
}

describe('dashboard preview flag copy does not promise a composer row (#19400)', () => {
  const shipped = SUPPORTED_LANGUAGES.filter((l) => !GENERATED.has(l.code)).map((l) => l.code)

  it('the English description does not name the composer', () => {
    const desc = resolve(catalogOf('en'), DESC_KEY)
    expect(typeof desc, `en is missing ${DESC_KEY}`).toBe('string')
    expect(
      (desc as string).toLowerCase().includes('composer'),
      `the flag description must not name the chat composer; the dock that drew ` +
        `a row above it is retired, got: "${desc}"`,
    ).toBe(false)
  })

  it.each(shipped)('%s: the description does not name the composer', (code) => {
    const catalog = catalogOf(code)
    const desc = resolve(catalog, DESC_KEY)
    const label = resolve(catalog, COMPOSER_LABEL_KEY)
    expect(typeof desc, `${code} is missing ${DESC_KEY}`).toBe('string')
    expect(typeof label, `${code} is missing ${COMPOSER_LABEL_KEY}`).toBe('string')
    expect(
      (desc as string).toLowerCase().includes((label as string).toLowerCase()),
      `${code}: the flag description must not name the composer as the Chat ` +
        `panel labels it ("${label}"); the row above it is retired, got: "${desc}"`,
    ).toBe(false)
  })

  it('the Feature Previews card still describes the flag from DESC_KEY', () => {
    const source = readFileSync(
      join(__dirname, '..', 'pages', 'settings', 'FeaturePreviewsSection.tsx'),
      'utf-8',
    )
    expect(source).toContain(`'${DESC_KEY}'`)
  })

  it('the Chat panel still labels the composer from COMPOSER_LABEL_KEY', () => {
    const source = readFileSync(
      join(__dirname, '..', 'pages', 'settings', 'ChatPanel.tsx'),
      'utf-8',
    )
    expect(source).toContain(`'${COMPOSER_LABEL_KEY}'`)
  })
})
