/**
 * The one destructive-action confirm: a real `role="alertdialog"` with focus
 * management, replacing `window.confirm`.
 *
 * `window.confirm` blocks the main thread, cannot be styled or translated
 * reliably, and swallows the Escape/focus contract the rest of the dashboard
 * honors. The caller owns the copy: a `t()` title and body naming *what* is
 * about to be deleted, plus the confirm/cancel labels.
 */
import { ModalShell } from './ui/Modal'
import { Button } from './ui/Primitives'
import type { UiLang } from '../locales/engine'

export function ConfirmDialog({
  open,
  lang,
  title,
  body,
  confirmLabel,
  cancelLabel,
  busy = false,
  onConfirm,
  onCancel,
}: {
  open: boolean
  lang: UiLang
  title: string
  body?: string
  /** Already resolved through the catalog by the caller. */
  confirmLabel: string
  cancelLabel: string
  busy?: boolean
  onConfirm: () => void
  onCancel: () => void
}) {
  return (
    <ModalShell open={open} lang={lang} titleId="confirm-dialog-title" title={title} onClose={onCancel} width={440} variant="alert">
      {body ? <p className="confirm-dialog__body">{body}</p> : null}
      <div className="modal-sheet-actions confirm-dialog__actions">
        <Button variant="ghost" size="sm" onClick={onCancel} disabled={busy}>
          {cancelLabel}
        </Button>
        <Button variant="danger" size="sm" onClick={onConfirm} loading={busy}>
          {confirmLabel}
        </Button>
      </div>
    </ModalShell>
  )
}
