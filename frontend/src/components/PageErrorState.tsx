import { useEffect, useRef } from 'react'

interface PageErrorStateProps {
  onRetry: () => void
}

export function PageErrorState({ onRetry }: PageErrorStateProps) {
  const headingRef = useRef<HTMLHeadingElement>(null)
  useEffect(() => headingRef.current?.focus(), [])

  return (
    <main className="centered-page">
      <div className="auth-panel">
        <p className="eyebrow">MBB</p>
        <h1 tabIndex={-1} ref={headingRef}>
          Authentication unavailable
        </h1>
        <p>We could not confirm your session. Protected information is hidden.</p>
        <button className="button button--primary" type="button" onClick={onRetry}>
          Try again
        </button>
      </div>
    </main>
  )
}
