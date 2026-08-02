import { useState, type InputHTMLAttributes } from 'react'

interface PasswordFieldProps extends Omit<InputHTMLAttributes<HTMLInputElement>, 'type'> {
  label: string
}

export function PasswordField({ label, id, ...inputProps }: PasswordFieldProps) {
  const [visible, setVisible] = useState(false)
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      <div className="password-input">
        <input id={id} type={visible ? 'text' : 'password'} {...inputProps} />
        <button
          type="button"
          className="password-toggle"
          aria-label={`${visible ? 'Hide' : 'Show'} ${label.toLowerCase()}`}
          aria-pressed={visible}
          onClick={() => setVisible((value) => !value)}
        >
          {visible ? 'Hide' : 'Show'}
        </button>
      </div>
    </div>
  )
}
