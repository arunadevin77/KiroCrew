import { describe, it, expect } from 'vitest'

import {
  isOpenableMarkerPath, openFileMarkerPaths, OPEN_FILE_MARKER_MAX_CHIPS, OPEN_FILE_MARKER_MAX_PATH,
} from '../utils/detectOpenFileMarker'

const marker = (path: string) => `<!-- kirocrew:open-file path="${path}" -->`

describe('openFileMarkerPaths', () => {
  it('returns the absolute path a marker in the reply names', () => {
    expect(openFileMarkerPaths(`Wrote the note.\n${marker('/home/u/notes/today.md')}\n`)).toEqual(['/home/u/notes/today.md'])
  })

  it('accepts Windows drive-qualified paths', () => {
    expect(openFileMarkerPaths(marker('C:\\Users\\u\\notes.md'))).toEqual(['C:\\Users\\u\\notes.md'])
    expect(openFileMarkerPaths(marker('D:/work/notes.md'))).toEqual(['D:/work/notes.md'])
  })

  it('tolerates whitespace inside the comment', () => {
    expect(openFileMarkerPaths('<!--   kirocrew:open-file   path="/a/b.md"   -->')).toEqual(['/a/b.md'])
  })

  it('returns nothing without a marker, and ignores the preview marker', () => {
    expect(openFileMarkerPaths('see /home/u/notes.md')).toEqual([])
    expect(openFileMarkerPaths('<!-- kirocrew:preview url="http://127.0.0.1:8080" -->')).toEqual([])
    expect(openFileMarkerPaths('')).toEqual([])
  })

  it('ignores a marker the reply shows in code', () => {
    expect(openFileMarkerPaths(`Emit this:\n\`\`\`\n${marker('/home/u/a.md')}\n\`\`\`\n`)).toEqual([])
    expect(openFileMarkerPaths(`Emit this:\n~~~html\n${marker('/home/u/a.md')}\n~~~`)).toEqual([])
    expect(openFileMarkerPaths(`Use \`${marker('/home/u/a.md')}\` to offer a file.`)).toEqual([])
    // A real marker after the fenced example still counts.
    expect(openFileMarkerPaths(`\`\`\`\n${marker('/x.md')}\n\`\`\`\n${marker('/home/u/real.md')}`)).toEqual(['/home/u/real.md'])
  })

  it('returns every marked file once, in reply order', () => {
    expect(openFileMarkerPaths(`${marker('/a.md')} then ${marker('/b.md')} and ${marker('/a.md')}`)).toEqual(['/a.md', '/b.md'])
  })

  it('skips a marker naming a refused path and keeps the others', () => {
    expect(openFileMarkerPaths(`${marker('/good.md')} ${marker('relative.md')} ${marker('/also-good.md')}`))
      .toEqual(['/good.md', '/also-good.md'])
  })

  it('caps the chips one reply offers', () => {
    const many = Array.from({ length: OPEN_FILE_MARKER_MAX_CHIPS + 3 }, (_, i) => marker(`/n/${i}.md`)).join('\n')
    expect(openFileMarkerPaths(many)).toHaveLength(OPEN_FILE_MARKER_MAX_CHIPS)
  })

  it('refuses relative, ~, empty and network-share paths', () => {
    for (const bad of ['notes.md', './notes.md', '../etc/passwd', 'sub/dir/notes.md', '~', '~/notes.md', '~root/x', '',
      '//host/share/x', '\\\\host\\share\\x', 'C:notes.md', 'C:\\\\host\\x']) {
      expect(openFileMarkerPaths(marker(bad)), bad).toEqual([])
    }
  })
})

describe('isOpenableMarkerPath', () => {
  it('refuses every root that can reach another host, in any separator mix', () => {
    for (const bad of ['/\\attacker.example\\share\\note.md', '\\/attacker.example/share/note.md',
      '\\\\attacker.example\\share\\note.md', '//attacker.example/share/note.md', 'C:\\/host/share/x.md',
      'C:/\\host\\share\\x.md', 'C://host/share/x.md', '\\\\?\\C:\\notes.md', '\\\\.\\pipe\\x', '/??/UNC/host/share/x.md',
      '/net/attacker.example/export/note.md', '/NET/host/x.md', '/afs/cell.example/x.md', '/home/u/*.md']) {
      expect(isOpenableMarkerPath(bad), bad).toBe(false)
      expect(openFileMarkerPaths(marker(bad)), bad).toEqual([])
    }
  })

  it('refuses a path that is not in normal form, so no segment trick reaches an automount root', () => {
    for (const bad of ['/./net/attacker.example/export/note.md', '/srv/../net/host/x.md', '/srv/./notes.md',
      '/srv//notes.md', '/srv/notes/', 'C:\\Users\\.\\u\\notes.md', 'C:\\Users\\\\u\\notes.md', '/.\\net\\host\\x.md']) {
      expect(isOpenableMarkerPath(bad), bad).toBe(false)
    }
  })

  it('refuses a colon anywhere but a drive letter', () => {
    for (const bad of ['C:\\notes.md:hidden', '/home/u/notes.md:stream', 'C:\\a\\D:\\b.md']) {
      expect(isOpenableMarkerPath(bad), bad).toBe(false)
    }
  })

  it('keeps ordinary absolute paths that only look like the refused shapes', () => {
    for (const good of ['/home/u/notes.md', '/network/notes.md', '/srv/net/notes.md', '/srv/afs-notes/a.md',
      'C:\\Users\\u\\net\\notes.md', 'D:/work/notes.md']) {
      expect(isOpenableMarkerPath(good), good).toBe(true)
    }
  })

  it('refuses dot-dot segments, control characters, padding and over-long paths', () => {
    for (const bad of ['/home/u/../../etc/shadow', 'C:\\a\\..\\b', '/a\u0000b', '/a\nb', ' /a.md', '/a.md ',
      '/' + 'a'.repeat(OPEN_FILE_MARKER_MAX_PATH)]) {
      expect(isOpenableMarkerPath(bad), JSON.stringify(bad.slice(0, 40))).toBe(false)
    }
    expect(isOpenableMarkerPath('/home/u/..notes/a..md')).toBe(true)
  })
})
