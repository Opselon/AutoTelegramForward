/**
 * Haptic feedback, wherever the running surface provides it.
 *
 * Inside Telegram Mini App the platform offers `HapticFeedback`; everywhere
 * else this module is a silent no-op, so call sites never branch on the
 * host. Every call is guarded: haptics must never be the reason a tap
 * breaks. Used sparingly by contract — navigation taps, confirmations and
 * destructive gates — not on every hover or keystroke.
 */

type Impact = 'light' | 'medium' | 'soft' | 'rigid'
type Notify = 'error' | 'success' | 'warning'
export type HapticKind = Impact | Notify | 'selection'

export function haptic(kind: HapticKind = 'light'): void {
  try {
    const hf = (window as any)?.Telegram?.WebApp?.HapticFeedback
    if (!hf || typeof hf !== 'object') return
    if (kind === 'selection') {
      hf.selectionChanged?.()
    } else if (kind === 'error' || kind === 'success' || kind === 'warning') {
      hf.notificationOccurred?.(kind)
    } else {
      hf.impactOccurred?.(kind)
    }
  } catch {
    // No host, no permission, no problem.
  }
}
