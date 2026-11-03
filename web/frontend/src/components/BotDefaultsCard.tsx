/**
 * Bot-defaults viewer: what the task's attached bot supplies to every task
 * that does not pin its own value (`GET /bot-tokens/{id}/defaults`).
 *
 * The settings rows already show provenance per key (task / bot / default);
 * this card answers the complementary question — "what does the bot actually
 * say?" — in one place, so an operator comparing a task against its bot can
 * see both sides without a second tool. Read-only by design: a careless write
 * here would silently re-baseline every task that inherits, so editing stays
 * out of the UI until it gets the same care as the task editor.
 *
 * A failed read is a status, not a silent skip: the card says so and offers a
 * retry, but never blocks the settings editor around it.
 */
import { useCallback, useEffect, useState } from 'react'
import { RefreshCw } from 'lucide-react'
import { api } from '../api'
import { makeT, type UiLang } from '../locales/engine'

export function BotDefaultsCard({ botId, lang }: { botId: string | undefined; lang: UiLang }) {
  const t = makeT(lang)
  const [defaults, setDefaults] = useState<Record<string, unknown> | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  const load = useCallback(async () => {
    if (!botId) return
    setLoading(true)
    setError(null)
    try {
      const res = await api.getBotTokenDefaults(botId)
      if (res?.success === false) {
        setError(res.message || res.error_code || t('ui_status_unknown'))
        setDefaults(null)
      } else {
        setDefaults(res?.defaults ?? {})
      }
    } catch (err) {
      setDefaults(null)
      setError(err instanceof Error ? err.message : t('ui_status_unknown'))
    } finally {
      setLoading(false)
    }
  }, [botId, t])

  useEffect(() => {
    setDefaults(null)
    setError(null)
    void load()
  }, [load])

  if (!botId) return null

  const entries = defaults ? Object.entries(defaults).sort(([a], [b]) => a.localeCompare(b)) : []

  return (
    <div
      style={{
        marginTop: '14px',
        padding: 'var(--space-3)',
        background: 'var(--color-surface-inset)',
        border: '1px solid var(--color-border)',
        borderRadius: '10px',
        minInlineSize: 0,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 'var(--space-2)' }}>
        <div style={{ minWidth: 0 }}>
          <div style={{ fontSize: 'var(--type-sm)', fontWeight: 700, color: 'var(--color-text)' }}>
            {t('ui_bot_defaults')}
          </div>
          <div style={{ fontSize: 'var(--type-xs)', color: 'var(--color-text-muted)', marginTop: 2 }}>
            {t('ui_bot_defaults_hint')}
          </div>
        </div>
        <button
          type="button"
          onClick={() => void load()}
          className="btn btn--ghost btn--icon btn--sm"
          aria-label={t('ui_refresh')}
          title={t('ui_refresh')}
          disabled={loading}
        >
          <RefreshCw size={13} aria-hidden="true" className={loading ? 'spin' : undefined} />
        </button>
      </div>

      {error ? (
        <div
          role="alert"
          style={{
            marginTop: 'var(--space-2)',
            fontSize: 'var(--type-xs)',
            color: 'var(--color-danger-text)',
            overflowWrap: 'anywhere',
          }}
        >
          {error}
        </div>
      ) : null}

      {!error && loading && defaults === null ? (
        <div style={{ fontSize: 'var(--type-xs)', color: 'var(--color-text-muted)', marginTop: 'var(--space-2)' }}>
          {t('ui_loading_the_settings_schema')}
        </div>
      ) : null}

      {!error && defaults !== null && entries.length === 0 ? (
        <div style={{ fontSize: 'var(--type-xs)', color: 'var(--color-text-muted)', marginTop: 'var(--space-2)' }}>
          {t('ui_status_unknown')}
        </div>
      ) : null}

      {!error && entries.length > 0 ? (
        <dl
          style={{
            margin: 'var(--space-2) 0 0',
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fill, minmax(240px, 1fr))',
            gap: 'var(--space-2)',
          }}
        >
          {entries.map(([key, value]) => (
            <div key={key} style={{ minWidth: 0 }}>
              <dt
                dir="ltr"
                style={{
                  fontSize: 'var(--type-xs)',
                  fontWeight: 600,
                  color: 'var(--color-text-muted)',
                  textAlign: 'start',
                  overflowWrap: 'anywhere',
                }}
              >
                {key}
              </dt>
              <dd
                dir="ltr"
                style={{
                  margin: 0,
                  fontSize: 'var(--type-xs)',
                  color: 'var(--color-text)',
                  fontFamily: "'JetBrains Mono', Consolas, monospace",
                  textAlign: 'start',
                  overflowWrap: 'anywhere',
                }}
              >
                {typeof value === 'object' && value !== null ? JSON.stringify(value) : String(value)}
              </dd>
            </div>
          ))}
        </dl>
      ) : null}
    </div>
  )
}
