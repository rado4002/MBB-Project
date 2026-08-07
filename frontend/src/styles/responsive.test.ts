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
      /@media \(min-width: 80rem\)[\s\S]*?\.workspace-columns \{ grid-template-columns: minmax\(0, 1fr\) minmax\(12rem, 15rem\)/,
    )
    expect(globalCss).toMatch(/\.context-trigger \{ display: none; \}/)
  })

  it('controls region scrolling and wraps long content without horizontal overflow', () => {
    expect(globalCss).toMatch(/\.queue-panel \{[^}]*overflow: hidden auto/)
    expect(globalCss).toMatch(/\.message-history \{[^}]*overflow-y: auto; overflow-x: hidden/)
    expect(globalCss).toMatch(/\.message-text, \.message-media \{[^}]*overflow-wrap: anywhere/)
    expect(globalCss).toMatch(/\.conversation-header__content h2 \{[^}]*overflow-wrap: anywhere/)
    expect(globalCss).toMatch(/\.context-details dd \{[^}]*overflow-wrap: anywhere/)
    expect(globalCss).toMatch(/body \{[^}]*min-inline-size: 0/)
    expect(globalCss).toMatch(/\.reply-composer \{[^}]*min-inline-size: 0;[^}]*max-inline-size: 100%/)
    expect(globalCss).toMatch(/\.composer-modes \{[^}]*min-inline-size: 0;[^}]*max-width: 100%/)
    expect(globalCss).toMatch(/\.composer-modes__options \{[^}]*display: inline-flex;[^}]*overflow: hidden/)
    expect(globalCss).not.toMatch(/\.reply-composer \{[^}]*calc\([^}]*-1\)/)
  })

  it('uses one compact header and supplementary desktop context', () => {
    expect(globalCss).toMatch(
      /\.workspace-header \{[^}]*grid-template-columns: auto minmax\(0, 1fr\) auto;[^}]*padding: var\(--space-2\) var\(--space-3\)/,
    )
    expect(globalCss).toMatch(
      /\.conversation-header__content--loaded \{[^}]*grid-template-columns: auto minmax\(0, 1fr\);[^}]*align-items: baseline/,
    )
    expect(globalCss).toMatch(/\.conversation-metadata \{[^}]*display: flex;[^}]*flex-wrap: wrap/)
    expect(globalCss).not.toContain('.workspace-summary')
    expect(globalCss).not.toContain('.workspace-detail-region')
  })

  it('uses a compact selected-conversation filter disclosure and dense readable messages', () => {
    expect(globalCss).toMatch(
      /\.inbox-page--selected \.inbox-header h1 \{[^}]*font-size: 1\.125rem/,
    )
    expect(globalCss).toMatch(
      /\.conversation-filters--compact \{[^}]*position: relative;[^}]*margin-bottom: var\(--space-1\)/,
    )
    expect(globalCss).toMatch(
      /\.conversation-filters__body \{[^}]*position: absolute;[^}]*width: min\(100%, 48rem\)/,
    )
    expect(globalCss).toMatch(/\.message-list \{[^}]*gap: var\(--space-2\)/)
    expect(globalCss).toMatch(/\.message article \{[^}]*padding: var\(--space-3\)/)
    expect(globalCss).toMatch(
      /\.message article header \{[^}]*align-items: baseline;[^}]*margin-bottom: var\(--space-1\)/,
    )
    expect(globalCss).toMatch(
      /@media \(max-width: 47\.99rem\) \{[\s\S]*?\.inbox-page--selected \.inbox-header,\s*\.inbox-page--selected \.queue-panel \{ display: none; \}/,
    )
    expect(globalCss).not.toMatch(
      /@media \(max-width: 47\.99rem\) \{[\s\S]*?\.inbox-page--selected \.conversation-filters[^\n]*display: none/,
    )
  })

  it('uses the remaining viewport for a flexible timeline and bounded composer', () => {
    expect(globalCss).toMatch(
      /\.app-frame \{[^}]*height: 100dvh;[^}]*grid-template-rows: auto minmax\(0, 1fr\);[^}]*overflow: hidden/,
    )
    expect(globalCss).toMatch(
      /\.inbox-page--selected \{[^}]*height: 100%;[^}]*grid-template-rows: auto auto minmax\(0, 1fr\);[^}]*overflow: hidden/,
    )
    expect(globalCss).toMatch(
      /\.workspace-columns \{[^}]*height: 100%;[^}]*min-height: 0;[^}]*overflow: hidden/,
    )
    expect(globalCss).toMatch(
      /\.timeline-panel \{[^}]*grid-template-rows: minmax\(0, 1fr\) auto;[^}]*padding: 0;[^}]*overflow: hidden/,
    )
    expect(globalCss).toMatch(
      /\.reply-composer \{[^}]*margin: 0;[^}]*border-top: 1px solid var\(--color-border\);[^}]*border-radius: 0/,
    )
    expect(globalCss).toMatch(
      /\.reply-composer \{[^}]*gap: var\(--space-1\);[^}]*padding: var\(--space-1\) var\(--space-3\)/,
    )
    expect(globalCss).toMatch(
      /\.reply-composer > textarea \{[^}]*field-sizing: content;[^}]*min-height: 3\.5rem;[^}]*max-height: 8\.5rem;[^}]*overflow-y: auto;[^}]*resize: vertical/,
    )
    expect(globalCss).toMatch(
      /\.reply-composer__meta \{[^}]*display: flex;[^}]*flex-wrap: wrap/,
    )
    expect(globalCss).toMatch(
      /@media \(min-width: 64rem\)[\s\S]*?\.composer-controls \{ grid-template-columns: auto minmax\(0, 1fr\); align-items: center/,
    )
    expect(globalCss).not.toContain('height: clamp(32rem, calc(100dvh - 20rem), 48rem)')
    expect(globalCss).not.toContain('min-height: calc(100dvh - 9rem)')
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
