import { forwardRef, type ReactNode } from 'react'

interface InlineAlertProps {
  children: ReactNode
  tone?: 'error' | 'warning'
  requestId?: string
  id?: string
}

export const InlineAlert = forwardRef<HTMLDivElement, InlineAlertProps>(
  function InlineAlert({ children, tone = 'error', requestId, id }, ref) {
    return (
      <div
        className={`alert alert--${tone}`}
        role="alert"
        id={id}
        tabIndex={-1}
        ref={ref}
      >
        <div>{children}</div>
        {requestId ? (
          <div className="alert__reference">
            Request reference: <code>{requestId}</code>
          </div>
        ) : null}
      </div>
    )
  },
)
