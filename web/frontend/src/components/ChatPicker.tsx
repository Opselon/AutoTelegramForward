import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { AlertCircle, Check, Loader2, Pin, Search } from 'lucide-react'
import { api } from '../api'
import { pinnedFirstOrder } from '../chatOrder'
import type { ChatSummary } from '../types'
import { makeT, type UiLang } from '../locales/engine'
import { ModalShell } from './ui/Modal'

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
 *
 * It rides the shared `ModalShell` (portal to document.body): overlay, Escape,
 * focus trap, scroll lock and the phone bottom-sheet chrome all come from the
 * shell — the picker stacks on top of RuleModal without stealing its Escape
 * (the overlay stack in `Modal.tsx` pops the topmost surface first) and its
 * action bar stays pinned above the safe area instead of scrolling away.
 */
export function ChatPicker({ sessionId, lang, alreadyIn, onPick, onClose }: Props) {
  const t = makeT(lang)
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
    [sessionId, query, lang, t],
  )

  // Server-side search, debounced: the query is what the *server* filters on, so
  // a match beyond the first page is still found (the same honesty rule the
  // mobile sheet documents).
  useEffect(() => {
    const timer = setTimeout(() => void load(0, false), 300)
    return () => clearTimeout(timer)
  }, [load])

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
    padding: '10px 12px',
    marginBottom: '6px',
  }

  return createPortal(
    <ModalShell
      open
      lang={lang}
      titleId="chat-picker-title"
      onClose={onClose}
      width={560}
      title={
        <span style={{ display: 'block' }}>
          {t('ui_chat_picker_title')}
          <span
            style={{
              display: 'block',
              fontSize: 'var(--type-xs)',
              fontWeight: 500,
              color: 'var(--color-text-muted)',
              marginTop: 2,
            }}
          >
            {t('ui_chat_picker_subtitle', { total: String(total) })}
          </span>
        </span>
      }
      footer={
        <>
          <span style={{ fontSize: 'var(--type-xs)', color: 'var(--color-text-muted)', flex: 1, minWidth: 0 }}>
            {picks.length
              ? t('ui_chat_picker_selected_count', { n: String(picks.length) })
              : t('ui_chat_picker_none_selected')}
          </span>
          <button type="button" onClick={onClose} className="btn btn--ghost btn--sm">
            {t('ui_cancel')}
          </button>
          <button
            type="button"
            disabled={picks.length === 0}
            onClick={() => {
              onPick(picks)
              onClose()
            }}
            className="btn btn--primary btn--sm"
          >
            {t('ui_chat_picker_add_selected', { n: String(picks.length || 1) })}
          </button>
        </>
      }
    >
      <div style={{ padding: '0' }}>
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: '8px',
            backgroundColor: 'var(--color-surface-inset)',
            border: '1px solid var(--color-border-strong)',
            borderRadius: '10px',
            padding: '10px 12px',
          }}
        >
          <Search size={16} color="var(--color-text-muted)" aria-hidden="true" />
          <input
            autoFocus
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder={t('ui_chat_picker_search_hint')}
            aria-label={t('ui_chat_picker_search_hint')}
            style={{
              flex: 1,
              minWidth: 0,
              background: 'transparent',
              border: 'none',
              outline: 'none',
              color: 'var(--color-text)',
              fontSize: 'var(--type-base)',
            }}
          />
          {loading && <Loader2 size={16} className="spin" color="var(--color-info)" aria-hidden="true" />}
        </div>
        {error && (
          <div
            style={{
              display: 'flex',
              gap: '6px',
              alignItems: 'center',
              color: 'var(--color-danger-text)',
              fontSize: 'var(--type-xs)',
              marginTop: '10px',
            }}
          >
            <AlertCircle size={14} aria-hidden="true" />
            <span>{error}</span>
          </div>
        )}
      </div>

      <div style={{ marginTop: '12px' }}>
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
                minHeight: 44,
                textAlign: 'start',
                color: 'inherit',
                font: 'inherit',
                cursor: isTaken ? 'default' : 'pointer',
                opacity: isTaken ? 0.55 : 1,
                borderColor: isSelected ? 'var(--color-primary)' : 'var(--color-border-strong)',
                backgroundColor: isSelected ? 'var(--color-primary-soft)' : 'var(--color-surface-inset)',
              }}
            >
              <span
                aria-hidden="true"
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
                  {chat.pinned && <Pin size={12} color="var(--color-warning)" aria-hidden="true" />}
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
                <span style={{ fontSize: 'var(--type-xs)', color: 'var(--color-text-muted)', overflowWrap: 'anywhere' }}>
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
                <span style={{ fontSize: 'var(--type-xs)', color: 'var(--color-success)', flexShrink: 0 }}>
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
              minHeight: 44,
              backgroundColor: 'var(--color-surface-2)',
              border: '1px solid var(--color-border-strong)',
              borderRadius: '10px',
              color: 'var(--color-text)',
              padding: '10px',
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
    </ModalShell>,
    document.body,
  )
}
