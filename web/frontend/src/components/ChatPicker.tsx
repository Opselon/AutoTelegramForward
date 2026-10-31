import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { AlertCircle, Check, Loader2, Pin, Search, X } from 'lucide-react'
import { api } from '../api'
import { pinnedFirstOrder } from '../chatOrder'
import type { ChatSummary } from '../types'
import { isRtlLang, makeT, type UiLang } from '../locales/engine'

/** One page of dialogs per request. The server clamps to 1..200. */
const PAGE_SIZE = 100

/** What a row shows as its title: name, else @username, else the id. */
function labelOf(chat: ChatSummary): string {
  const title = (chat.title ?? '').trim()
  if (title) return title
  const username = (chat.username ?? '').trim()
  if (username) return `@${username}`
  return chat.chat_id
}

/**
 * Name the failure the way the user can act on it.
 *
 * A session that exists but has no live client (503 `session_not_connected`) and
 * an older server without the route (404) are both **permanent for this panel**:
 * the honest answer is "browse is unavailable, type the id instead". Anything
 * else is worth a retry. The id field stays usable in every case — the picker is
 * a convenience, never a gate.
 */
function failureKey(err: unknown): string {
  const raw = err instanceof Error ? err.message : String(err)
  let code = ''
  try {
    const parsed = JSON.parse(raw)
    if (parsed && typeof parsed === 'object' && typeof parsed.code === 'string') {
      code = parsed.code
    }
  } catch {
    // Not JSON: a network error or a proxy page. Falls through to the generic key.
  }
  if (code === 'session_not_connected' || code === 'session_not_found' || code === 'not_found') {
    return 'ui_chat_picker_unavailable'
  }
  return 'ui_chat_picker_load_failed'
}

interface Props {
  /** The session whose dialogs are browsed — always the rule's own session. */
  sessionId: string
  lang: UiLang
  /** Ids already in the list being edited; shown as added and not pickable twice. */
  alreadyIn: string[]
  onPick: (picks: { id: string; title: string }[]) => void
  onClose: () => void
}

/**
 * The dashboard's chat browser: the dialog list the mobile pickers have had, so
 * a task's sources/targets can be chosen instead of typed.
 *
 * It is deliberately additive. Manual entry stays open behind it, a failed load
 * is a banner rather than an error screen, and picking returns ids — the same
 * strings a rule stores — so the existing save path keeps validating the route.
 */
export function ChatPicker({ sessionId, lang, alreadyIn, onPick, onClose }: Props) {
  const t = makeT(lang)
  const isRtl = isRtlLang(lang)
  const [query, setQuery] = useState('')
  const [chats, setChats] = useState<ChatSummary[]>([])
  const [total, setTotal] = useState(0)
  const [truncated, setTruncated] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [selected, setSelected] = useState<Record<string, string>>({})
  // One request at a time: a fast typist must not have an older, shorter answer
  // land after a newer one and blank the list.
  const requestSeq = useRef(0)

  const taken = useMemo(() => new Set(alreadyIn.map((id) => id.trim())), [alreadyIn])

  const load = useCallback(
    async (offset: number, append: boolean) => {
      const seq = ++requestSeq.current
      setLoading(true)
      setError(null)
      try {
        const page = await api.listChats(sessionId, {
          query: query.trim(),
          limit: PAGE_SIZE,
          offset,
        })
        if (seq !== requestSeq.current) return
        const next = Array.isArray(page?.chats) ? page.chats : []
        setChats((prev) => (append ? [...prev, ...next] : next))
        setTotal(Number(page?.total ?? next.length))
        setTruncated(Boolean(page?.truncated))
      } catch (err) {
        if (seq !== requestSeq.current) return
        if (!append) setChats([])
        setError(t(failureKey(err)))
      } finally {
        if (seq === requestSeq.current) setLoading(false)
      }
    },
    // `t` is re-created per render by makeT; the failure text only depends on
    // the language, so keying on `lang` keeps the callback stable.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [sessionId, query, lang],
  )

  // Server-side search, debounced: the query is what the *server* filters on, so
  // a match beyond the first page is still found (the same honesty rule the
  // mobile sheet documents).
  useEffect(() => {
    const timer = setTimeout(() => void load(0, false), 300)
    return () => clearTimeout(timer)
  }, [load])

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const ordered = useMemo(() => pinnedFirstOrder(chats), [chats])
  const canLoadMore = truncated && chats.length < total

  const toggle = (chat: ChatSummary) => {
    const key = chat.chat_id
    if (taken.has(key)) return
    setSelected((prev) => {
      if (key in prev) {
        const next = { ...prev }
        delete next[key]
        return next
      }
      return { ...prev, [key]: labelOf(chat) }
    })
  }

  const picks = Object.entries(selected).map(([id, title]) => ({ id, title }))

  const cardStyle: React.CSSProperties = {
    backgroundColor: 'var(--color-surface-inset)',
    border: '1px solid var(--color-border-strong)',
    borderRadius: '10px',
    padding: '8px 10px',
    marginBottom: '6px',
  }

  return (
    <div
      role="dialog"
      aria-modal="true"
      dir={isRtl ? 'rtl' : 'ltr'}
      style={{
        position: 'fixed',
        inset: 0,
        backgroundColor: 'var(--color-scrim)',
        backdropFilter: 'blur(6px)',
        zIndex: 10000,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: '16px',
      }}
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose()
      }}
    >
      <div
        style={{
          width: '100%',
          maxWidth: '560px',
          maxHeight: '88vh',
          backgroundColor: 'var(--color-surface)',
          color: 'var(--color-text)',
          borderRadius: '18px',
          border: '1px solid var(--color-border)',
          boxShadow: 'var(--shadow-3)',
          display: 'flex',
          flexDirection: 'column',
          overflow: 'hidden',
        }}
      >
        <div
          style={{
            padding: '14px 16px',
            borderBottom: '1px solid var(--color-border)',
            backgroundColor: 'var(--color-surface-2)',
            display: 'flex',
            alignItems: 'center',
            gap: '10px',
          }}
        >
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 'var(--type-base)', fontWeight: 700 }}>{t('ui_chat_picker_title')}</div>
            <div style={{ fontSize: '11px', color: 'var(--color-text-muted)', marginTop: '2px' }}>
              {t('ui_chat_picker_subtitle', { total: String(total) })}
            </div>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label={t('ui_close')}
            style={{ background: 'none', border: 'none', color: 'var(--color-text-muted)', cursor: 'pointer' }}
          >
            <X size={18} />
          </button>
        </div>

        <div style={{ padding: '12px 16px 0' }}>
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: '8px',
              backgroundColor: 'var(--color-surface-inset)',
              border: '1px solid var(--color-border-strong)',
              borderRadius: '10px',
              padding: '8px 10px',
            }}
          >
            <Search size={14} color="var(--color-text-muted)" />
            <input
              autoFocus
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder={t('ui_chat_picker_search_hint')}
              style={{
                flex: 1,
                background: 'transparent',
                border: 'none',
                outline: 'none',
                color: 'var(--color-text)',
                fontSize: 'var(--type-xs)',
              }}
            />
            {loading && <Loader2 size={14} className="spin" color="var(--color-info)" />}
          </div>
          {error && (
            <div
              style={{
                display: 'flex',
                gap: '6px',
                alignItems: 'center',
                color: 'var(--color-danger-text)',
                fontSize: '11px',
                marginTop: '8px',
              }}
            >
              <AlertCircle size={12} />
              <span>{error}</span>
            </div>
          )}
        </div>

        <div style={{ flex: 1, overflowY: 'auto', padding: '12px 16px' }}>
          {!loading && !error && ordered.length === 0 && (
            <div style={{ fontSize: 'var(--type-xs)', color: 'var(--color-text-muted)' }}>{t('ui_chat_picker_no_matches')}</div>
          )}
          {ordered.map((chat) => {
            const isTaken = taken.has(chat.chat_id)
            const isSelected = chat.chat_id in selected
            return (
              <button
                type="button"
                key={chat.chat_id}
                onClick={() => toggle(chat)}
                disabled={isTaken}
                style={{
                  ...cardStyle,
                  display: 'flex',
                  alignItems: 'center',
                  gap: '10px',
                  width: '100%',
                  textAlign: isRtl ? 'right' : 'left',
                  cursor: isTaken ? 'default' : 'pointer',
                  opacity: isTaken ? 0.55 : 1,
                  borderColor: isSelected ? 'var(--color-primary)' : 'var(--color-border-strong)',
                  backgroundColor: isSelected ? 'var(--color-primary-soft)' : 'var(--color-surface-inset)',
                }}
              >
                <span
                  style={{
                    width: '16px',
                    height: '16px',
                    borderRadius: '4px',
                    border: `1px solid ${isSelected ? 'var(--color-primary)' : 'var(--color-border-strong)'}`,
                    backgroundColor: isSelected ? 'var(--color-primary)' : 'transparent',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    flexShrink: 0,
                  }}
                >
                  {isSelected && <Check size={12} color="var(--color-on-accent)" />}
                </span>
                <span style={{ flex: 1, minWidth: 0 }}>
                  <span style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                    {chat.pinned && <Pin size={12} color="var(--color-warning)" />}
                    <span
                      style={{
                        fontSize: 'var(--type-xs)',
                        fontWeight: 600,
                        color: 'var(--color-text)',
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap',
                      }}
                    >
                      {labelOf(chat)}
                    </span>
                  </span>
                  <span style={{ fontSize: '10px', color: 'var(--color-text-muted)' }}>
                    {[
                      chat.chat_type || '',
                      chat.username ? `@${chat.username}` : '',
                      chat.chat_id,
                    ]
                      .filter(Boolean)
                      .join(' · ')}
                  </span>
                </span>
                {isTaken && (
                  <span style={{ fontSize: '10px', color: 'var(--color-success)' }}>
                    {t('ui_chat_picker_already_added')}
                  </span>
                )}
              </button>
            )
          })}
          {canLoadMore && (
            <button
              type="button"
              onClick={() => void load(chats.length, true)}
              style={{
                width: '100%',
                backgroundColor: 'var(--color-surface-2)',
                border: '1px solid var(--color-border-strong)',
                borderRadius: '10px',
                color: 'var(--color-text)',
                padding: '8px',
                fontSize: 'var(--type-xs)',
                cursor: 'pointer',
              }}
            >
              {t('ui_chat_picker_load_more', {
                shown: String(chats.length),
                total: String(total),
              })}
            </button>
          )}
        </div>

        <div
          style={{
            padding: '12px 16px',
            borderTop: '1px solid var(--color-border)',
            backgroundColor: 'var(--color-surface-2)',
            display: 'flex',
            alignItems: 'center',
            gap: '10px',
          }}
        >
          <span style={{ fontSize: '11px', color: 'var(--color-text-muted)', flex: 1 }}>
            {picks.length
              ? t('ui_chat_picker_selected_count', { n: String(picks.length) })
              : t('ui_chat_picker_none_selected')}
          </span>
          <button
            type="button"
            onClick={onClose}
            style={{
              background: 'none',
              border: '1px solid var(--color-border-strong)',
              borderRadius: '8px',
              color: 'var(--color-text)',
              padding: '8px 12px',
              fontSize: 'var(--type-xs)',
              cursor: 'pointer',
            }}
          >
            {t('ui_cancel')}
          </button>
          <button
            type="button"
            disabled={picks.length === 0}
            onClick={() => {
              onPick(picks)
              onClose()
            }}
            style={{
              backgroundColor: picks.length ? 'var(--color-primary)' : 'var(--color-surface-2)',
              color: picks.length ? 'var(--color-on-accent)' : 'var(--color-text-muted)',
              border: '1px solid var(--color-border-strong)',
              borderRadius: '8px',
              padding: '8px 12px',
              fontSize: 'var(--type-xs)',
              fontWeight: 600,
              cursor: picks.length ? 'pointer' : 'not-allowed',
              whiteSpace: 'nowrap',
            }}
          >
            {t('ui_chat_picker_add_selected', { n: String(picks.length || 1) })}
          </button>
        </div>
      </div>
    </div>
  )
}
