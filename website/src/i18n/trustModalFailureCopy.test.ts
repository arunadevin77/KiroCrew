/**
 * The trust dialog's failure headlines -- a drift guard on a copy family.
 *
 * The dialog has three paths (enable, first install, update) and two failure
 * states on each: the grant stood (`failed`, `install_failed`, `update_failed`)
 * or nothing was saved (`failed_generic`, `install_failed_generic`,
 * `update_failed_generic`). Each state's advice is one clause shared by its
 * three paths, so a reword on one path only would make the paths read
 * differently. The invariants are asserted as STRUCTURE, in every authored
 * catalog, rather than as literals:
 *
 * 1. The three headlines of a state end on one shared clause, and that clause
 *    names the retry button by its own label.
 * 2. The not-saved state's next step is a separate sentence with two forms.
 *    Only the connected form names the agent hand-off by its label: the
 *    hand-off button hides while the gateway is unreachable, so the
 *    disconnected form and the headlines themselves never point at it.
 *
 * English only: the granted clause does not describe the retry as trusting a
 * second time, and the not-saved clause does not send the reader to logs.
 */

import { describe, it, expect } from 'vitest'

import { CATALOGS } from './catalogs'

const GRANTED = ['failed', 'install_failed', 'update_failed'] as const
const GENERIC = ['failed_generic', 'install_failed_generic', 'update_failed_generic'] as const

type Catalog = Record<string, unknown>

function at(catalog: Catalog, path: string): string {
  let node: unknown = catalog
  for (const part of path.split('.')) node = (node as Catalog | undefined)?.[part]
  if (typeof node !== 'string') throw new Error(`missing ${path}`)
  return node
}

function modal(catalog: Catalog, key: string): string {
  return at(catalog, `components.appstore.trustAppModal.${key}`)
}

/** The longest suffix every string shares. */
function sharedSuffix(values: readonly string[]): string {
  const [first, ...rest] = values
  let n = 0
  while (n < first.length && rest.every(v => v.length > n && v[v.length - 1 - n] === first[first.length - 1 - n])) n += 1
  return first.slice(first.length - n)
}

/**
 * A button label as a sentence can carry it. The Bengali hand-off label ends on
 * the আপনি-form imperative (করুন), which the Bengali register (style/bn.md §5)
 * keeps out of running copy, so a sentence names the control by the words
 * before that ending and closes on the তুমি form. Every other label is used
 * whole.
 */
function labelStem(label: string): string {
  return label.replace(/\s*করুন$/, '')
}

// The generated pseudolocale rewrites every string on its own, so a label it
// embeds inside a sentence is not the label it renders on the button.
const LOCALES = Object.entries(CATALOGS)
  .filter(([code]) => code !== 'en-XA')
  .map(([code, { translation }]) => [code, translation as Catalog] as const)

describe('trust dialog failure headlines', () => {
  it.each(LOCALES)('%s: the granted headlines share one clause naming the retry button', (_code, catalog) => {
    const clause = sharedSuffix(GRANTED.map(k => modal(catalog, k)))
    expect(clause).toContain(modal(catalog, 'confirm_after_failure'))
  })

  it.each(LOCALES)('%s: the not-saved headlines share one clause naming the retry button', (_code, catalog) => {
    const clause = sharedSuffix(GENERIC.map(k => modal(catalog, k)))
    expect(clause).toContain(modal(catalog, 'confirm_after_failure'))
  })

  it.each(LOCALES)('%s: only the connected next step names the agent hand-off', (_code, catalog) => {
    const ask = labelStem(at(catalog, 'components.askAgent.ask_the_agent'))
    expect(modal(catalog, 'failed_generic_next_ask')).toContain(ask)
    expect(modal(catalog, 'failed_generic_next_offline')).not.toContain(ask)
    for (const key of GENERIC) expect(modal(catalog, key)).not.toContain(ask)
  })

  it('en: the retry is not described as trusting again, and nothing points at logs', () => {
    const en = CATALOGS.en.translation as Catalog
    for (const key of GRANTED) expect(modal(en, key)).not.toMatch(/re-confirm/i)
    for (const key of [...GENERIC, 'failed_generic_next_ask', 'failed_generic_next_offline']) {
      expect(modal(en, key)).not.toMatch(/\blogs?\b/i)
    }
  })
})
