import axe from 'axe-core'

export async function expectAccessible(container: HTMLElement) {
  const result = await axe.run(container, {
    rules: {
      // jsdom does not calculate layout or rendered color contrast.
      'color-contrast': { enabled: false },
    },
  })
  const summary = result.violations
    .map((violation) => `${violation.id}: ${violation.help}`)
    .join('\n')
  if (result.violations.length) throw new Error(summary)
}
