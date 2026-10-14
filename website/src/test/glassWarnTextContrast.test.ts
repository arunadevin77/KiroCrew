/**
 * Warn-hued text on a warn-tinted fill clears the WCAG 1.4.3 text floor (4.5:1)
 * in every built-in LIGHT palette. Issue #19920: plain `--warn` on a
 * `glass-warn` pane read as gold on pale yellow (1.9:1 in rosepine-light, 4.0:1
 * in `light`). Issue #20071: the same hue on `bg-warn-subtle` / `bg-warn/N`
 * fills, 64 source lines outside the glass. One token now carries both:
 * `--warn-text` is `--warn` in dark palettes and, under `[data-mode="light"]`,
 * half the hue darkened toward black (still amber-brown, so it reads as a
 * warning); `text-warn-text` paints it on the fills and `.glass-warn-fg` on the
 * glass. This file reads that mix from
 * index.css and measures it against every tint the source actually pairs with
 * it, and refuses a source line that still pairs plain `text-warn` with a warn
 * tint or a `glass-warn` pane. A tint that only appears on `hover:` is paired
 * with `hover:text-warn-text`, so the control keeps its plain hue at rest.
 *
 * SURFACES: a tint is translucent, so the exact pixel under the text depends
 * on what is behind it. The test takes the fill over `--bg`, `--bg-elevated`
 * and `--card` (the surfaces the pairings sit on; the first two are also the
 * glass's reduce-transparency fallbacks) and requires the floor against each.
 *
 * SCOPE: light palettes only, which is what both issues ask; dark palettes keep
 * plain `--warn`, and a test pins that. Theme packs installed at runtime are
 * out of reach.
 */
import { describe, it, expect } from 'vitest'
import { readdirSync, readFileSync } from 'node:fs'
import { join, relative, resolve, sep } from 'node:path'
import { relativeLuminance, contrastRatio, parseCssColor } from '../lib/iconContrast'
import { CSS, THEMES, resolveVar } from './themePalette'

const MIN_TEXT = 4.5
/** `.glass-warn` mixes `--warn` at 12% into its tint (index.css). */
const GLASS_TINT = 0.12
const SURFACES = ['--bg', '--bg-elevated', '--card'] as const

interface Rgb { r: number; g: number; b: number }

const ACTIVE = CSS.replace(/\/\*[\s\S]*?\*\//g, '')
const LIGHT_THEMES = THEMES.filter((t) => t === 'light' || t.endsWith('-light'))
const DARK_THEMES = THEMES.filter((t) => !LIGHT_THEMES.includes(t))

function tokenRgb(theme: string, prop: string): Rgb {
  const raw = resolveVar(theme, prop)
  if (raw === undefined) throw new Error(`theme ${theme}: ${prop} resolves to nothing`)
  const c = parseCssColor(raw)
  if (!c) throw new Error(`theme ${theme}: ${prop}=${raw} is not a colour this test can measure`)
  return { r: c.r, g: c.g, b: c.b }
}

/** The alpha of a theme's `--warn-subtle` (`rgba(r,g,b,A)`): the share of
 *  `--warn` a `bg-warn-subtle` fill lays over its surface. */
function warnSubtleShare(theme: string): number {
  const raw = resolveVar(theme, '--warn-subtle')
  const c = raw === undefined ? null : parseCssColor(raw)
  if (!c) throw new Error(`theme ${theme}: --warn-subtle=${raw} is not a colour this test can measure`)
  return c.a
}

/** `color-mix(in srgb, a w, b)`: a straight per-channel mix in sRGB. */
const mix = (a: Rgb, b: Rgb, w: number): Rgb => ({
  r: a.r * w + b.r * (1 - w),
  g: a.g * w + b.g * (1 - w),
  b: a.b * w + b.b * (1 - w),
})
const lum = ({ r, g, b }: Rgb) => relativeLuminance(r, g, b)

const BLACK: Rgb = { r: 0, g: 0, b: 0 }

/** The light-mode token's warn share, read from the stylesheet so a change to
 *  the rule is measured rather than a copy of it. */
function lightWarnShare(): number {
  const m = /\[data-mode="light"\]\s*\{\s*--warn-text\s*:\s*color-mix\(in srgb,\s*var\(--warn\)\s*(\d+)%,\s*#000\)\s*;?\s*\}/.exec(ACTIVE)
  expect(m, 'index.css lost the [data-mode="light"] --warn-text mix toward black').not.toBeNull()
  return Number(m![1]) / 100
}

/** Light-mode `--warn-text` for a theme. */
const warnText = (theme: string, share: number): Rgb =>
  mix(tokenRgb(theme, '--warn'), BLACK, share)

// ── Source scan ──────────────────────────────────────────────────────────────
// A class token is a run of [\w-/] bounded by non-class characters; a variant
// prefix (`hover:`) is kept with it. `text-warn` must end the token exactly:
// `text-warn-text` and `text-warn-fg` are other utilities, `text-warn/80` an
// opacity modifier this test does not govern.
const TEXT_WARN = /(?<![\w-])(?:[\w-]+:)*text-warn(?![\w/-])/
const TEXT_WARN_TEXT = /(?<![\w-])(?:[\w-]+:)*text-warn-text(?![\w/-])/
const WARN_TINT = /(?<![\w-])(?:[\w-]+:)*bg-warn(?:-subtle|\/(\d+))(?![\w-])/g
const GLASS_WARN = /(?<![\w-])glass-warn(?![\w-])/
// The same three tokens with their variant prefix captured (`''` at rest,
// `hover:` and so on), for the check that pairs a tint with the text painted in
// the SAME state.
const TEXT_WARN_PFX = /(?<![\w-])((?:[\w-]+:)*)text-warn(?![\w/-])/g
const TEXT_WARN_TEXT_PFX = /(?<![\w-])((?:[\w-]+:)*)text-warn-text(?![\w/-])/g
const WARN_TINT_PFX = /(?<![\w-])((?:[\w-]+:)*)bg-warn(?:-subtle|\/\d+)(?![\w-])/g

/** True when some state of the line lays a warn tint under plain `text-warn`.
 *  A tint that only exists under `hover:` is covered by `hover:text-warn-text`
 *  (or by an at-rest `text-warn-text`), which keeps the control's plain hue at
 *  rest where no tint sits under it. */
function plainWarnOnTint(text: string): boolean {
  const prefixes = (re: RegExp) => new Set([...text.matchAll(re)].map((m) => m[1]))
  const plain = prefixes(TEXT_WARN_PFX)
  const safe = prefixes(TEXT_WARN_TEXT_PFX)
  for (const t of prefixes(WARN_TINT_PFX)) {
    const covered = safe.has(t) || (t !== '' && safe.has(''))
    if (!covered && (plain.has(t) || plain.has(''))) return true
  }
  return false
}

interface SourceLine { file: string; line: number; text: string }

function sourceLines(): SourceLine[] {
  const root = resolve(__dirname, '..')
  const out: SourceLine[] = []
  for (const entry of readdirSync(root, { recursive: true, withFileTypes: true })) {
    if (!entry.isFile() || !/\.tsx?$/.test(entry.name) || /\.test\.tsx?$/.test(entry.name)) continue
    const full = join(entry.parentPath, entry.name)
    if (full.split(sep).includes('test')) continue
    const file = relative(root, full)
    readFileSync(full, 'utf8').split('\n').forEach((text, i) => out.push({ file, line: i + 1, text }))
  }
  return out
}
const LINES = sourceLines()
const FILES = new Set(LINES.map((l) => l.file))

/** `{ subtle }` or `{ n }` for every warn tint paired with `text-warn-text`
 *  anywhere in the source: the set the token is measured against. */
function pairedTints(): { tints: Map<string, number | 'subtle'>; sites: string[] } {
  const tints = new Map<string, number | 'subtle'>()
  const sites: string[] = []
  for (const l of LINES) {
    if (!TEXT_WARN_TEXT.test(l.text)) continue
    const found = [...l.text.matchAll(WARN_TINT)]
    if (found.length === 0) continue
    sites.push(`${l.file}:${l.line}`)
    for (const m of found) tints.set(m[1] === undefined ? 'subtle' : `/${m[1]}`, m[1] === undefined ? 'subtle' : Number(m[1]) / 100)
  }
  return { tints, sites }
}

describe('warn text on warn tints in light palettes (#19920, #20071)', () => {
  it('anchors the premise: the rules exist and the light palettes are found', () => {
    expect(ACTIVE).toMatch(/:root\s*\{\s*--warn-text\s*:\s*var\(--warn\)\s*;?\s*\}/)
    expect(ACTIVE).toMatch(/\.glass-warn-fg\s*\{\s*color:\s*var\(--warn-text\)/)
    expect(readFileSync(resolve(__dirname, '../tailwind-theme.css'), 'utf8')).toMatch(/--color-warn-text:\s*var\(--warn-text\);/)
    expect(LIGHT_THEMES.length).toBeGreaterThanOrEqual(15)
    for (const t of ['light', 'rosepine-light', 'everforest-light']) expect(LIGHT_THEMES).toContain(t)
    expect(FILES.size, 'scanned no source files').toBeGreaterThan(100)
  })

  it('leaves dark palettes on plain --warn', () => {
    // The token is derived outside the [data-theme] blocks: `:root` sets it to
    // `--warn` and only `[data-mode="light"]` overrides it. No theme block and
    // no dark-mode rule may set it, or a dark palette's warn text would drift.
    expect(ACTIVE).not.toMatch(/\[data-mode="dark"\][^{}]*\{[^{}]*--warn-text\s*:/)
    for (const t of DARK_THEMES) expect(resolveVar(t, '--warn-text'), `${t} overrides --warn-text`).toBe('var(--warn)')
    expect(DARK_THEMES.length).toBeGreaterThanOrEqual(15)
  })

  it('plain --warn would fail somewhere (so the mix is doing real work)', () => {
    const failing = LIGHT_THEMES.filter((t) => {
      const warn = tokenRgb(t, '--warn')
      return contrastRatio(lum(warn), lum(mix(warn, tokenRgb(t, '--bg'), warnSubtleShare(t)))) < MIN_TEXT
    })
    expect(failing.length).toBeGreaterThan(0)
  })

  it('clears 4.5:1 on the glass-warn tint in every light palette', () => {
    const w = lightWarnShare()
    const failures: string[] = []
    for (const t of LIGHT_THEMES) {
      const warn = tokenRgb(t, '--warn')
      const fg = lum(warnText(t, w))
      for (const base of ['--bg-elevated', '--bg']) {
        const ratio = contrastRatio(fg, lum(mix(warn, tokenRgb(t, base), GLASS_TINT)))
        if (ratio < MIN_TEXT) failures.push(`${t}: warn ${w * 100}% vs warn/12 over ${base} is ${ratio.toFixed(2)}:1`)
      }
    }
    expect(failures, `glass-warn text below 4.5:1:\n${failures.join('\n')}`).toEqual([])
  })

  it('clears 4.5:1 on every warn tint the source pairs with text-warn-text, in every light palette', () => {
    const w = lightWarnShare()
    const { tints, sites } = pairedTints()
    // The premise: the pairings exist (the issue listed 64 lines) and the
    // strongest tint the token is tuned for is still among them, so a new
    // `bg-warn/40 text-warn-text` is measured here rather than assumed.
    expect(sites.length).toBeGreaterThanOrEqual(50)
    expect([...tints.keys()].sort()).toEqual(expect.arrayContaining(['subtle', '/10', '/15', '/30']))
    const failures: string[] = []
    for (const t of LIGHT_THEMES) {
      const warn = tokenRgb(t, '--warn')
      const fg = lum(warnText(t, w))
      for (const [name, share] of tints) {
        const n = share === 'subtle' ? warnSubtleShare(t) : share
        for (const base of SURFACES) {
          const ratio = contrastRatio(fg, lum(mix(warn, tokenRgb(t, base), n)))
          if (ratio < MIN_TEXT) failures.push(`${t}: warn ${w * 100}% vs bg-warn${name === 'subtle' ? '-subtle' : name} over ${base} is ${ratio.toFixed(2)}:1`)
        }
      }
    }
    expect(failures, `text-warn-text below 4.5:1:\n${failures.join('\n')}`).toEqual([])
  })

  it('tells a tint at rest from a tint that only appears on hover', () => {
    // The scan's own premise: the four controls that have no tint until hover
    // keep their plain hue at rest, and a real pairing is still refused.
    expect(plainWarnOnTint('bg-warn-subtle text-warn')).toBe(true)
    expect(plainWarnOnTint('text-warn bg-warn/10 border-warn')).toBe(true)
    expect(plainWarnOnTint('hover:text-warn hover:bg-warn-subtle')).toBe(true)
    expect(plainWarnOnTint('text-warn hover:bg-warn/10')).toBe(true)
    expect(plainWarnOnTint('text-warn hover:text-warn-text hover:bg-warn/10')).toBe(false)
    expect(plainWarnOnTint('text-muted hover:text-warn-text hover:bg-warn-subtle')).toBe(false)
    expect(plainWarnOnTint('bg-warn-subtle text-warn-text')).toBe(false)
    expect(plainWarnOnTint('bg-warn text-warn-fg hover:bg-warn/80')).toBe(false)
  })

  it('leaves no warn tint or glass-warn pane painting plain text-warn in the source', () => {
    // A line that lays a warn tint (`bg-warn-subtle`, `bg-warn/N`) or a
    // `glass-warn` pane under plain `text-warn` is the pairing these issues fix.
    // `text-warn-text` (fills) and `glass-warn-fg` (glass) are the sanctioned
    // replacements. Lines are the unit, as in the issue's own census: a ternary
    // that puts the tint and the text in two literals on one line is still one
    // element wearing both. A tint under a variant is matched with the text of
    // that variant (see plainWarnOnTint).
    const offenders: string[] = []
    for (const l of LINES) {
      if (!TEXT_WARN.test(l.text)) continue
      if (plainWarnOnTint(l.text) || GLASS_WARN.test(l.text)) offenders.push(`${l.file}:${l.line}`)
    }
    expect(offenders, `warn tints still painting plain text-warn:\n${offenders.join('\n')}`).toEqual([])
  })
})
