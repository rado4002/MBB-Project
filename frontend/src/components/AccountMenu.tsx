import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import type { HumanSummary } from '../api/contracts/auth'

interface AccountMenuProps {
  human: HumanSummary
  onLogout: () => Promise<void>
}

function roleLabel(role: HumanSummary['role']) {
  return role.charAt(0).toUpperCase() + role.slice(1)
}

export function AccountMenu({ human, onLogout }: AccountMenuProps) {
  const [open, setOpen] = useState(false)
  const buttonRef = useRef<HTMLButtonElement>(null)
  const containerRef = useRef<HTMLDivElement>(null)

  const close = (returnFocus = false) => {
    setOpen(false)
    if (returnFocus) queueMicrotask(() => buttonRef.current?.focus())
  }

  useEffect(() => {
    if (!open) return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') close(true)
    }
    const onPointerDown = (event: MouseEvent) => {
      if (!containerRef.current?.contains(event.target as Node)) close()
    }
    document.addEventListener('keydown', onKeyDown)
    document.addEventListener('mousedown', onPointerDown)
    return () => {
      document.removeEventListener('keydown', onKeyDown)
      document.removeEventListener('mousedown', onPointerDown)
    }
  }, [open])

  return (
    <div className="account-menu" ref={containerRef}>
      <button
        ref={buttonRef}
        className="account-menu__trigger"
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
      >
        <span className="account-menu__name">{human.display_name}</span>
        <span className="account-menu__role">{roleLabel(human.role)}</span>
        <svg aria-hidden="true" viewBox="0 0 20 20" width="16" height="16">
          <path d="m5 7.5 5 5 5-5" fill="none" stroke="currentColor" strokeWidth="1.8" />
        </svg>
      </button>
      {open ? (
        <div className="account-menu__popover" role="menu">
          <div className="account-menu__identity">
            <strong>{human.display_name}</strong>
            <span>{roleLabel(human.role)}</span>
          </div>
          <Link role="menuitem" to="/account" onClick={() => close()}>
            My Account
          </Link>
          <Link role="menuitem" to="/session" onClick={() => close()}>
            Session
          </Link>
          <button
            role="menuitem"
            type="button"
            onClick={() => {
              close()
              void onLogout().catch(() => undefined)
            }}
          >
            Logout
          </button>
        </div>
      ) : null}
    </div>
  )
}
