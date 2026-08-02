export function PageLoadingState() {
  return (
    <main className="centered-page" aria-busy="true" aria-live="polite">
      <div className="auth-panel">
        <p className="eyebrow">MBB</p>
        <h1>Checking your session</h1>
        <p className="muted">Please wait.</p>
      </div>
    </main>
  )
}
