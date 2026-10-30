/**
 * Shared modal shell: overlay, labelled dialog, Escape, focus in on open and
 * restored on close, Tab trapped inside, body scroll locked while open.
 *
 * RuleModal, FilterModal and ConfirmDialog were three copies of the same
 * accessibility choreography; Navigation's drawer already proved the pattern,
 * and this is the same one lifted into a reusable surface. Copy never lives
 * here — a title, a body and every button label arrive through `t()` from the
 * caller, like every other primitive.
 */
import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { X } from 'lucide-react'
import { makeT, isRtlLang, type UiLang } from '../../locales/engine'

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'

/** How long the exit transition runs before the shell actually unmounts.
    Kept in one place with the matching CSS durations (see index.css, motion
    round section): enter 200ms, exit subtler and shorter. */
const EXIT_MS = 170

export function ModalShell({
  open,
  lang,
  titleId,
  title,
  onClose,
  children,
  width,
  variant = 'dialog',
}: {
  open: boolean
  lang: UiLang
  /** id of the element labelling the dialog — referenced by aria-labelledby. */
  titleId: string
  title: ReactNode
  onClose: () => void
  children: ReactNode
  /** Dialog max width; the shell stays fluid below it. */
  width?: number
  /** `dialog` renders a labelled banner row with a close button; `alert` is the
      smaller confirm surface (role="alertdialog") without its own close X. */
  variant?: 'dialog' | 'alert'
}) {
  const t = makeT(lang)
  const isRtl = isRtlLang(lang)
  const sheetRef = useRef<HTMLDivElement | null>(null)
  const restoreRef = useRef<HTMLElement | null>(null)
  // Exit animation state: the shell keeps rendering for one short beat with
  // the closing classes applied, THEN calls onClose so the parent unmounts
  // after the transition instead of snapping the overlay out of existence.
  // Enter needs no JS — @starting-style drives it in CSS.
  const [closing, setClosing] = useState(false)
  const closeTimer = useRef<number | null>(null)

  const requestClose = useCallback(() => {
    if (closeTimer.current !== null) return // already closing — ignore repeats
    const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
    if (reduce || EXIT_MS <= 0) {
      onClose()
      return
    }
    setClosing(true)
    closeTimer.current = window.setTimeout(() => {
      closeTimer.current = null
      onClose()
    }, EXIT_MS)
  }, [onClose])

  // Reopening clears the closing beat (and any stray timer) so a fast
  // close-then-open on the same mounted shell never renders half-faded.
  useEffect(() => {
    if (!open) return
    setClosing(false)
    if (closeTimer.current !== null) {
      window.clearTimeout(closeTimer.current)
      closeTimer.current = null
    }
  }, [open])

  useEffect(
    () => () => {
      if (closeTimer.current !== null) window.clearTimeout(closeTimer.current)
    },
    [],
  )

  // Focus moves into the dialog on open and returns to whoever opened it on
  // close — the same contract the navigation drawer implements.
  useEffect(() => {
    if (!open) {
      restoreRef.current?.focus?.()
      restoreRef.current = null
      return
    }
    restoreRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const el = sheetRef.current
    if (!el) return
    const handle = window.requestAnimationFrame(() => {
      const target = el.querySelector<HTMLElement>(FOCUSABLE)
      target?.focus()
    })
    return () => window.cancelAnimationFrame(handle)
  }, [open])

  useEffect(() => {
    if (!open) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.stopPropagation()
        requestClose()
        return
      }
      if (event.key !== 'Tab') return
      const focusables = sheetRef.current?.querySelectorAll<HTMLElement>(FOCUSABLE)
      if (!focusables || focusables.length === 0) return
      const first = focusables[0]
      const last = focusables[focusables.length - 1]
      const current = document.activeElement
      if (event.shiftKey && (current === first || !sheetRef.current?.contains(current))) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && current === last) {
        event.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', onKey, true)
    return () => document.removeEventListener('keydown', onKey, true)
  }, [open, requestClose])

  // The page behind a modal must not scroll under a touch drag.
  useEffect(() => {
    if (!open) return
    const previous = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.body.style.overflow = previous
    }
  }, [open])

  if (!open) return null

  return (
    <div
      className={`modal-overlay${closing ? ' modal-overlay--closing' : ''}`}
      dir={isRtl ? 'rtl' : 'ltr'}
      onClick={(event) => {
        if (event.target === event.currentTarget) requestClose()
      }}
    >
      <div
        ref={sheetRef}
        className={`glass-panel modal-sheet${closing ? ' modal-sheet--closing' : ''}`}
        role={variant === 'alert' ? 'alertdialog' : 'dialog'}
        aria-modal="true"
        aria-labelledby={titleId}
        style={{ width: '100%', maxWidth: width ?? 680 }}
      >
        <header className="modal-head">
          <h3 id={titleId} className="modal-title">
            {title}
          </h3>
          {variant === 'dialog' && (
            <button
              type="button"
              onClick={requestClose}
              className="btn btn--ghost btn--icon"
              aria-label={t('ui_close')}
            >
              <X size={16} aria-hidden="true" />
            </button>
          )}
        </header>
        <div className="modal-sheet-body">{children}</div>
      </div>
    </div>
  )
}
