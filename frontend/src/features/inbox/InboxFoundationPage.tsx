import { useEffect, useRef } from 'react'

export function InboxFoundationPage() {
  const headingRef = useRef<HTMLHeadingElement>(null)
  useEffect(() => headingRef.current?.focus(), [])

  return (
    <>
      <header className="page-header">
        <h1 tabIndex={-1} ref={headingRef}>Inbox</h1>
      </header>
      <section className="foundation-region" aria-label="Inbox foundation">
        <p>The Inbox foundation is ready.</p>
      </section>
    </>
  )
}
