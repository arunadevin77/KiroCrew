/**
 * Warn-hued text on a `glass-warn` pane clears the WCAG 1.4.3 text floor
 * (4.5:1) in every built-in LIGHT palette. Issue #19920: plain `--warn` on the
 * pane's own `glass-warn` tint read as gold on pale yellow (1.9:1 in
 * rosepine-light, 4.0:1 in `light`), so `.glass-warn-fg` mixes the hue toward
 * `--text-strong` under `[data-mode="light"]`. This file reads that mix from
 * index.css and measures it, and refuses a `glass-warn` pane in the source that
 * still paints plain `text-warn` (the queued-message card and the memory mode
 * chip both carried that pairing).
 *
 * SURFACES: the glass is translucent, so the exact pixel under the text depends
 * on what scrolled behind it. The test takes the opaque fallback the stylesheet
 * itself paints when the glass cannot (`--warn` at 12% over `--bg-elevated`,
 * the reduce-transparency fill) and the same tint over `--bg`, and requires the
 * floor against both.
 *
 * SCOPE: light palettes only, which is what the issue asks; dark palettes keep
 * plain `--warn`. Theme packs installed at runtime are out of reach.
 */
import { describe, it, expect } from 'vitest'
import { readdirSync, readFileSync } from 'node:fs'
import { join, relative, resolve, sep } from 'node:path'
import { relativeLuminance, contrastRatio, parseCssColor } from '../lib/iconContrast'
import { CSS, THEMES, resolveVar } from './themePalette'

const MIN_TEXT = 4.5
const WARN_TINT = 0.12

interface Rgb { r: number; g: number; b: number }

const ACTIVE = CSS.replace(/\/\*[\s\S]*?\*\//g, '')
const LIGHT_THEMES = THEMES.filter((t) => t === 'light' || t.endsWith('-light'))

function tokenRgb(theme: string, prop: string): Rgb {
  const raw = resolveVar(theme, prop)
  if (raw === undefined) throw new Error(`theme ${theme}: ${prop} resolves to nothing`)
  const c = parseCssColor(raw)
  if (!c) throw new Error(`theme ${theme}: ${prop}=${raw} is not a colour this test can measure`)
  return { r: c.r, g: c.g, b: c.b }
}

/** `color-mix(in srgb, a w, b)`: a straight per-channel mix in sRGB. */
const mix = (a: Rgb, b: Rgb, w: number): Rgb => ({
  r: a.r * w + b.r * (1 - w),
  g: a.g * w + b.g * (1 - w),
  b: a.b * w + b.b * (1 - w),
})
const lum = ({ r, g, b }: Rgb) => relativeLuminance(r, g, b)

/** The light-mode rule's warn share, read from the stylesheet so a change to
 *  the rule is measured rather than a copy of it. */
function lightWarnShare(): number {
  const m = /\[data-mode="light"\]\s*\.glass-warn-fg\s*\{\s*color:\s*color-mix\(in srgb,\s*var\(--warn\)\s*(\d+)%,\s*var\(--text-strong\)\)/.exec(ACTIVE)
  expect(m, 'index.css lost the [data-mode="light"] .glass-warn-fg mix toward --text-strong').not.toBeNull()
  return Number(m![1]) / 100
}

describe('warn text on a glass-warn pane in light palettes (#19920)', () => {
  it('anchors the premise: the rules exist and the light palettes are found', () => {
    expect(ACTIVE).toMatch(/\.glass-warn-fg\s*\{\s*color:\s*var\(--warn\)/)
    expect(LIGHT_THEMES.length).toBeGreaterThanOrEqual(15)
    for (const t of ['light', 'rosepine-light', 'everforest-light']) expect(LIGHT_THEMES).toContain(t)
  })

  it('plain --warn would fail somewhere (so the mix is doing real work)', () => {
    const failing = LIGHT_THEMES.filter((t) => {
      const warn = tokenRgb(t, '--warn')
      return contrastRatio(lum(warn), lum(mix(warn, tokenRgb(t, '--bg-elevated'), WARN_TINT))) < MIN_TEXT
    })
    expect(failing.length).toBeGreaterThan(0)
  })

  it('clears 4.5:1 on the warn-tinted fill in every light palette', () => {
    const w = lightWarnShare()
    const failures: string[] = []
    for (const t of LIGHT_THEMES) {
      const warn = tokenRgb(t, '--warn')
      const fg = lum(mix(warn, tokenRgb(t, '--text-strong'), w))
      for (const base of ['--bg-elevated', '--bg']) {
        const ratio = contrastRatio(fg, lum(mix(warn, tokenRgb(t, base), WARN_TINT)))
        if (ratio < MIN_TEXT) failures.push(`${t}: warn ${w * 100}% vs warn/12 over ${base} is ${ratio.toFixed(2)}:1`)
      }
    }
    expect(failures, `glass-warn text below 4.5:1:\n${failures.join('\n')}`).toEqual([])
  })

  it('leaves no glass-warn pane painting plain text-warn in the source', () => {
    // A class string that names both `glass-warn` and `text-warn` is the
    // pairing this issue fixes. `glass-warn-fg` is the sanctioned replacement.
    const root = resolve(__dirname, '..')
    const PAIR = /(['"`])[^'"`\n]*\bglass-warn\b(?!-)[^'"`\n]*\btext-warn\b[^'"`\n]*\1|(['"`])[^'"`\n]*\btext-warn\b[^'"`\n]*\bglass-warn\b(?!-)[^'"`\n]*\2/
    const offenders: string[] = []
    let scanned = 0
    for (const entry of readdirSync(root, { recursive: true, withFileTypes: true })) {
      if (!entry.isFile() || !/\.tsx?$/.test(entry.name) || /\.test\.tsx?$/.test(entry.name)) continue
      const full = join(entry.parentPath, entry.name)
      if (full.split(sep).includes('test')) continue
      scanned++
      const src = readFileSync(full, 'utf8')
      src.split('\n').forEach((line, i) => {
        if (PAIR.test(line)) offenders.push(`${relative(root, full)}:${i + 1}`)
      })
    }
    expect(scanned, 'scanned no source files').toBeGreaterThan(100)
    expect(offenders, `glass-warn panes still painting plain text-warn:\n${offenders.join('\n')}`).toEqual([])
  })
})
