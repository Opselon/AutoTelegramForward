/**
 * The dashboard's six shared primitives, per `docs/ui-ux-system.md` §3.
 *
 * Every panel is built from these instead of bespoke markup, so a spacing fix or
 * a focus-ring fix lands once. Three rules hold across all of them:
 *
 * - **No copy lives here.** A label, an empty sentence or an error reason is a
 *   `t()` value passed in by the caller, which keeps the ten-locale contract in
 *   one place (`locales/`). The only strings this file resolves itself are the
 *   four generic ones a *fallback* needs (`ui_try_again`, `ui_panel_loading`,
 *   `ui_empty_*`), and they come from the catalog like everything else.
 * - **No hex, no ad-hoc spacing.** Colours are role tokens (`--color-*`),
 *   spacing is the `--space-1..6` scale, radii are `--radius-*`. A component
 *   that needs a new shade gets a new token, not an inline `#rrggbb`.
 * - **Reachable by keyboard.** Every control renders a real `<button>` /
 *   `<label>`; an icon-only button is *typed* so it cannot exist without an
 *   accessible name.
 */
import type { CSSProperties, MouseEvent, ReactNode } from 'react'
import { AlertTriangle, Inbox, Loader2 } from 'lucide-react'
import { makeT, type UiLang } from '../../locales/engine'

/* ----------------------------------------------------------------- Card --- */

export function Card({
  title,
  action,
  children,
  className = '',
  id,
}: {
  title?: ReactNode
  action?: ReactNode
  children: ReactNode
  className?: string
  id?: string
}) {
  return (
    <section className={`card ${className}`.trim()} id={id}>
      {(title || action) && (
        <header className="card__head">
          {title ? <h3 className="card__title">{title}</h3> : <span />}
          {action ? <div className="card__action">{action}</div> : null}
        </header>
      )}
      <div className="card__body">{children}</div>
    </section>
  )
}

/* --------------------------------------------------------------- Button --- */

type ButtonVariant = 'primary' | 'ghost' | 'danger' | 'link'
type ButtonSize = 'sm' | 'md'

type ButtonBase = {
  variant?: ButtonVariant
  size?: ButtonSize
  loading?: boolean
  disabled?: boolean
  children?: ReactNode
  onClick?: (event: MouseEvent<HTMLButtonElement>) => void
  type?: 'button' | 'submit' | 'reset'
  className?: string
  title?: string
  id?: string
}

/**
 * `iconOnly` demands a `label`: an icon button without a name is invisible to a
 * screen reader, so the type makes the mistake unrepresentable.
 */
export type ButtonProps = ButtonBase &
  ({ iconOnly: true; label: string } | { iconOnly?: false; label?: string })

export function Button({
  variant = 'ghost',
  size = 'md',
  loading = false,
  disabled = false,
  children,
  onClick,
  type = 'button',
  className = '',
  title,
  id,
  ...rest
}: ButtonProps) {
  const iconOnly = 'iconOnly' in rest && rest.iconOnly === true
  const label = 'label' in rest ? rest.label : undefined
  return (
    <button
      type={type}
      id={id}
      className={`btn btn--${variant} btn--${size}${iconOnly ? ' btn--icon' : ''} ${className}`.trim()}
      onClick={onClick}
      disabled={disabled || loading}
      // `aria-busy` tells a screen reader the control is working, and the
      // spinner is decorative: the width stays locked so nothing jumps.
      aria-busy={loading || undefined}
      aria-label={iconOnly ? label : undefined}
      title={title ?? (iconOnly ? label : undefined)}
    >
      {loading ? <Loader2 size={16} className="spin" aria-hidden="true" /> : null}
      {iconOnly ? null : children}
    </button>
  )
}

/* ---------------------------------------------------------------- Field --- */

export function Field({
  label,
  hint,
  error,
  htmlFor,
  children,
}: {
  label: ReactNode
  hint?: ReactNode
  error?: ReactNode
  htmlFor?: string
  children: ReactNode
}) {
  return (
    <div className={`field${error ? ' field--invalid' : ''}`}>
      <label className="field__label" htmlFor={htmlFor}>
        {label}
      </label>
      {children}
      {error ? (
        <p className="field__error" role="alert">
          {error}
        </p>
      ) : hint ? (
        <p className="field__hint">{hint}</p>
      ) : null}
    </div>
  )
}

/* ---------------------------------------------------------------- Badge --- */

export type BadgeTone = 'success' | 'warning' | 'danger' | 'info' | 'neutral'

export function Badge({
  tone = 'neutral',
  children,
  title,
}: {
  tone?: BadgeTone
  children: ReactNode
  title?: string
}) {
  return (
    <span className={`badge badge--${tone}`} title={title}>
      {children}
    </span>
  )
}

/* ------------------------------------------------------------- Skeleton --- */

export function Skeleton({
  height = 14,
  width = '100%',
  className = '',
}: {
  height?: number | string
  width?: number | string
  className?: string
}) {
  const style: CSSProperties = { height, width }
  return <span className={`skeleton ${className}`.trim()} style={style} aria-hidden="true" />
}

/**
 * Three or more rows, because a one-row placeholder reads as content that is
 * already there (spec §3: "skeleton while loading (3 rows minimum)").
 */
export function SkeletonRows({ rows = 3, lang }: { rows?: number; lang?: UiLang }) {
  const t = makeT(lang ?? 'en')
  return (
    <div className="skeleton-rows" role="status" aria-live="polite" aria-label={t('ui_panel_loading')}>
      {Array.from({ length: Math.max(3, rows) }, (_, index) => (
        <Skeleton key={index} height={index === 0 ? 18 : 12} width={index === 0 ? '45%' : '100%'} />
      ))}
    </div>
  )
}

/* ----------------------------------------------------------- Empty state --- */

export function EmptyState({
  icon,
  title,
  description,
  action,
}: {
  icon?: ReactNode
  title: ReactNode
  description?: ReactNode
  action?: ReactNode
}) {
  return (
    <div className="empty-state">
      <span className="empty-state__icon" aria-hidden="true">
        {icon ?? <Inbox size={22} />}
      </span>
      <p className="empty-state__title">{title}</p>
      {description ? <p className="empty-state__body">{description}</p> : null}
      {action ? <div className="empty-state__action">{action}</div> : null}
    </div>
  )
}

/* ----------------------------------------------------------- Error state --- */

export function ErrorState({
  title,
  reason,
  onRetry,
  retryLabel,
  lang,
}: {
  title: ReactNode
  reason?: ReactNode
  onRetry?: () => void
  retryLabel?: string
  lang?: UiLang
}) {
  const t = makeT(lang ?? 'en')
  return (
    <div className="error-state" role="alert">
      <span className="error-state__icon" aria-hidden="true">
        <AlertTriangle size={22} />
      </span>
      <p className="error-state__title">{title}</p>
      {reason ? (
        <p className="error-state__reason">
          <span className="error-state__label">{t('ui_error_reason')}: </span>
          {reason}
        </p>
      ) : null}
      {onRetry ? (
        <div className="error-state__action">
          <Button variant="ghost" size="sm" onClick={onRetry}>
            {retryLabel ?? t('ui_try_again')}
          </Button>
        </div>
      ) : null}
    </div>
  )
}

/* ---------------------------------------------------------------- Toast --- */

export type ToastTone = 'success' | 'danger'

/**
 * Transient feedback. The region is `aria-live="polite"` and always mounted by
 * the shell, so a message that appears later is still announced; the toast
 * itself is a child of that region rather than a second live region.
 */
export function Toast({
  message,
  tone = 'success',
  lang,
}: {
  message: string
  tone?: ToastTone
  lang?: UiLang
}) {
  const t = makeT(lang ?? 'en')
  return (
    <div className={`toast toast--${tone}`}>
      <span className="toast__dot" aria-hidden="true" />
      <span className="toast__text">{message}</span>
      <span className="visually-hidden">{t('ui_status_messages')}</span>
    </div>
  )
}
