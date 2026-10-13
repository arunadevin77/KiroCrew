/**
 * Find the files an assistant reply offers to open, so the transcript can show
 * each one as an "Open file" chip under the reply.
 *
 * The signal is a hidden HTML comment the agent writes after a file the user
 * should see: `<!-- kirocrew:open-file path="/abs/path/notes.md" -->`.
 * rehypeRaw drops HTML comments from the rendered message, so the marker itself
 * is invisible, like the `kirocrew:preview` marker it sits beside (see
 * detectPreviewUrl).
 *
 * The marker only decides that a chip is SHOWN. Nothing opens until the user
 * clicks it, and the click goes through the same handler a click on a path chip
 * uses, so whether the file may be read stays the read endpoint's decision.
 * Even so, a chip is offered only for a path a reader can trust at a glance:
 *  - a marker inside a fenced code block or an inline code span is text the
 *    user can SEE (quoted output, documentation), not a hidden directive, so it
 *    gets no chip;
 *  - the path must be absolute. A relative path would be resolved against the
 *    gateway's current project directory and `~` against the gateway's home, so
 *    neither gets a chip. Neither do shapes that can reach another host (a UNC
 *    or device root in any separator mix, the `/net` and `/afs` automount
 *    roots), `.` or `..` segments, control characters and over-long values.
 */

// `<!-- kirocrew:open-file path="..." -->`, tolerant of surrounding whitespace.
const MARKER_RE = /<!--\s*kirocrew:open-file\s+path="([^"]*)"\s*-->/gi

/** Longest path a marker may name; anything longer gets no chip, rather than a truncated one. */
export const OPEN_FILE_MARKER_MAX_PATH = 4096

/** Most chips one reply shows; markers past this are ignored. */
export const OPEN_FILE_MARKER_MAX_CHIPS = 8

/** A POSIX absolute path (`/x`), or a Windows drive-qualified one (`C:\x`, `C:/x`). */
const ABSOLUTE_RE = /^(?:\/|[A-Za-z]:[\\/])/
/** Two separators in a row at the root, in any mix: a UNC share or device path on Windows. */
const NETWORK_ROOT_RE = /^(?:[A-Za-z]:)?[\\/][\\/]/
const CONTROL_RE = /[\u0000-\u001f\u007f]/
/** Wildcards and the `?` of a Windows device or NT-namespace prefix (`\\?\`, `/??/`). */
const DEVICE_CHAR_RE = /[?*]/
/** Automount roots that reach an arbitrary host by name (`/net/<host>/…`, `/afs/<cell>/…`). */
const AUTOMOUNT_ROOTS = new Set(['net', 'afs'])

/**
 * Is `raw` a path an open-file marker may name? Absolute and in normal form
 * only: relative paths, `~` paths, a root of two separators in any mix
 * (`//host`, `/\host`, `\\host`, `C://x`), `?` and `*`, a `:` anywhere but a
 * drive letter's, any empty, `.` or `..` segment, the `/net` and `/afs`
 * automount roots, control characters, empty and over-long values are all
 * refused. Every refused shape either resolves against a directory the agent
 * did not name or can reach another host.
 */
export function isOpenableMarkerPath(raw: string): boolean {
  if (!raw || raw.length > OPEN_FILE_MARKER_MAX_PATH) return false
  if (raw !== raw.trim()) return false
  if (CONTROL_RE.test(raw) || DEVICE_CHAR_RE.test(raw)) return false
  if (!ABSOLUTE_RE.test(raw) || NETWORK_ROOT_RE.test(raw)) return false
  const drive = /^[A-Za-z]:/.test(raw)
  if (raw.indexOf(':', drive ? 2 : 0) !== -1) return false
  const segments = raw.slice(drive ? 3 : 1).split(/[\\/]/)
  // Normal form only: no empty, `.` or `..` segment, so the first segment IS the
  // directory under the root and the automount check below cannot be stepped
  // around (`/./net/…`, `/a/../net/…`, `//net/…`).
  if (segments.some(seg => seg === '' || seg === '.' || seg === '..')) return false
  if (!drive && AUTOMOUNT_ROOTS.has(segments[0].toLowerCase())) return false
  return true
}

/** `text` with fenced code blocks and inline code spans removed. */
function stripCode(text: string): string {
  const kept: string[] = []
  let fence: { ch: string; len: number } | null = null
  for (const line of text.split('\n')) {
    const m = /^ {0,3}(`{3,}|~{3,})/.exec(line)
    if (fence) {
      if (m && m[1][0] === fence.ch && m[1].length >= fence.len && line.trim() === m[1]) fence = null
      continue
    }
    if (m) { fence = { ch: m[1][0], len: m[1].length }; continue }
    kept.push(line)
  }
  return kept.join('\n').replace(/(`+)[\s\S]*?\1/g, '')
}

/**
 * The paths one assistant reply's open-file markers name, in the order they
 * appear, each once. A marker shown in code, or naming a path
 * `isOpenableMarkerPath` refuses, contributes nothing; the others still count.
 * At most `OPEN_FILE_MARKER_MAX_CHIPS` paths are returned.
 */
export function openFileMarkerPaths(content: string): string[] {
  if (!content || !content.includes('kirocrew:open-file')) return []
  const text = stripCode(content)
  const paths: string[] = []
  MARKER_RE.lastIndex = 0
  let m: RegExpExecArray | null
  while ((m = MARKER_RE.exec(text)) !== null) {
    const path = m[1]
    if (!isOpenableMarkerPath(path) || paths.includes(path)) continue
    paths.push(path)
    if (paths.length >= OPEN_FILE_MARKER_MAX_CHIPS) break
  }
  return paths
}
