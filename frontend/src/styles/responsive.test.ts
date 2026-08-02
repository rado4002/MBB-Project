import { describe, expect, it } from 'vitest'
import globalCss from './global.css?raw'
import tokensCss from './tokens.css?raw'

describe('responsive Inbox style contract', () => {
  it('defines mobile, tablet, and desktop layouts without three narrow tablet columns', () => {
    expect(globalCss).toContain('@media (max-width: 47.99rem)')
    expect(globalCss).toContain('@media (min-width: 48rem)')
    expect(globalCss).toContain('@media (min-width: 80rem)')
    expect(globalCss).toMatch(
      /@media \(min-width: 48rem\)[\s\S]*?\.inbox-layout \{ grid-template-columns: minmax\(18rem, 0\.9fr\) minmax\(0, 1\.5fr\)/,
    )
    expect(globalCss).toMatch(
      /@media \(min-width: 80rem\)[\s\S]*?\.workspace-columns \{ grid-template-columns: minmax\(0, 2fr\) minmax\(16rem, 0\.8fr\)/,
    )
    expect(globalCss).toMatch(/\.context-trigger \{ display: none; \}/)
  })

  it('controls region scrolling and wraps long content without horizontal overflow', () => {
    expect(globalCss).toMatch(/\.queue-panel \{[^}]*overflow: hidden auto/)
    expect(globalCss).toMatch(/\.message-history \{[^}]*overflow-y: auto; overflow-x: hidden/)
    expect(globalCss).toMatch(/\.message-text, \.message-media \{[^}]*overflow-wrap: anywhere/)
    expect(globalCss).toMatch(/\.workspace-summary h3 \{[^}]*overflow-wrap: anywhere/)
    expect(globalCss).toMatch(/\.context-details dd \{[^}]*overflow-wrap: anywhere/)
  })

  it('uses tokens and static skeletons with an explicit reduced-motion boundary', () => {
    expect(tokensCss).toContain('--color-selected-surface:')
    expect(tokensCss).toContain('--color-skeleton:')
    expect(tokensCss).toContain('--color-overlay:')
    expect(globalCss).toMatch(/\.skeleton-block, \.skeleton-row, \.skeleton-message \{[^}]*var\(--color-skeleton\)/)
    expect(globalCss).toMatch(
      /@media \(prefers-reduced-motion: reduce\)[\s\S]*?animation: none !important/,
    )
    expect(globalCss).not.toMatch(/@keyframes\s+(pulse|shimmer)/i)
  })
})
