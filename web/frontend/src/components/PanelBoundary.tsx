/**
 * The boundary every lazy panel renders through.
 *
 * Two failures have to be survivable on a dashboard that is *operated*, not
 * browsed: a chunk that fails to arrive (offline, a stale deploy, a blocked
 * CDN) and a panel that throws while rendering. Both used to take the whole
 * screen down — the only boundary was at the root, so one bad panel blanked the
 * dashboard and lost the operator's place. Here the shell stays up, the failure
 * is explained in the reader's language, and Retry re-mounts *that* panel.
 *
 * `Suspense` sits inside the same boundary on purpose: the fallback is the
 * skeleton primitive (three rows minimum), never a spinner on a blank page.
 */
import { Component, Fragment, Suspense, type ErrorInfo, type ReactNode } from 'react'
import { SkeletonRows } from './ui/Primitives'
import { makeT, type UiLang } from '../locales/engine'

interface RetryableProps {
  children: ReactNode
  lang?: UiLang
}

interface RetryableState {
  error: string | null
  attempt: number
}

class Retryable extends Component<RetryableProps, RetryableState> {
  state: RetryableState = { error: null, attempt: 0 }

  static getDerivedStateFromError(error: unknown): Partial<RetryableState> {
    // The message, not `error.toString()`: `Error: Error: …` used to reach
    // operators verbatim.
    const reason = error instanceof Error ? error.message : String(error)
    return { error: reason || 'unknown error' }
  }

  componentDidCatch(error: unknown, info: ErrorInfo): void {
    console.error('Panel failed:', error, info.componentStack)
  }

  private retry = (): void => {
    // Bumping the key remounts the lazy subtree, which re-runs the dynamic
    // import as well as the render — a thrown render alone would not retry it.
    this.setState((state) => ({ error: null, attempt: state.attempt + 1 }))
  }

  render(): ReactNode {
    const { error, attempt } = this.state
    const t = makeT(this.props.lang ?? 'en')
    if (error) {
      return (
        <div className="panel-fallback" role="alert">
          <p className="panel-fallback__title">{t('ui_panel_error_title')}</p>
          <p className="panel-fallback__body">{t('ui_panel_error_body')}</p>
          <p className="panel-fallback__reason">
            <span className="panel-fallback__label">{t('ui_error_reason')}: </span>
            <code>{error}</code>
          </p>
          <button type="button" className="btn btn--ghost btn--sm" onClick={this.retry}>
            {t('ui_try_again')}
          </button>
        </div>
      )
    }
    return (
      <Suspense fallback={<SkeletonRows rows={5} lang={this.props.lang} />}>
        <Fragment key={attempt}>{this.props.children}</Fragment>
      </Suspense>
    )
  }
}

export function PanelBoundary({ children, lang }: RetryableProps) {
  return (
    <Retryable lang={lang}>
      <div className="panel-host">{children}</div>
    </Retryable>
  )
}
