import { useEffect, useRef } from 'react'
import { Link } from 'react-router-dom'

export function NotFoundPage() {
  const headingRef = useRef<HTMLHeadingElement>(null)
  useEffect(() => headingRef.current?.focus(), [])
  return (
    <main className="centered-page">
      <section className="auth-panel">
        <p className="eyebrow">MBB</p>
        <h1 tabIndex={-1} ref={headingRef}>Page not found</h1>
        <p>The requested page is not available.</p>
        <Link className="button button--primary" to="/">Return to MBB</Link>
      </section>
    </main>
  )
}
