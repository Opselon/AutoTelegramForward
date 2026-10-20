import { useState } from 'react'
import { AlertCircle, Check, KeyRound, Loader2, Trash2 } from 'lucide-react'
import { api } from '../api'
import type { BotTokenInfo } from '../types'
import { makeT, type UiLang } from '../locales/engine'

interface Props {
  /** The panel's language; the field owns no copy of its own otherwise. */
  lang?: UiLang
  /** The owner's attached bots: the only ids the server will accept here. */
  bots: BotTokenInfo[]
  /** The selected bot id (the setting's current value). */
  value: string
  inputStyle: React.CSSProperties
  hintStyle: React.CSSProperties
  /** Writes the picked bot id into the panel's pending edits. */
  onChange: (value: any) => void
  /** Re-reads the bot list after an attach or a removal. */
  reloadBots: () => Promise<void> | void
  /** True while `delivery_transport` is BOT: the server refuses to clear the token then. */
  botRoute: boolean
}

/**
 * The credential half of the source → BOT → target route.
 *
 * The token is write-only by construction: it is POSTed once, and everything
 * that comes back (including this component's own list) carries an id and a
 * resolved @username. That is why the "attach" form cannot prefill, and why
 * removing a bot is a DELETE rather than clearing a field.
 *
 * Two refusals come from the server and are surfaced verbatim rather than
 * guessed at here: a token Telegram rejects, and a bot a task still routes
 * through (409). The panel does not pre-empt either one — the server owns both
 * rules, and a second copy of them here would drift.
 */
export function BotRouteField({
  lang = 'en',
  bots,
  value,
  inputStyle,
  hintStyle,
  onChange,
  reloadBots,
  botRoute,
}: Props) {
  const t = makeT(lang)
  const [open, setOpen] = useState(false)
  const [label, setLabel] = useState('')
  const [token, setToken] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const selected = bots.find((b) => b.id === value)

  const attach = async () => {
    const secret = token.trim()
    if (!secret) return
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      const info = await api.createBotToken(secret, label.trim())
      // Never keep the secret in component state longer than the request.
      setToken('')
      setLabel('')
      setOpen(false)
      await reloadBots()
      if (info?.id) onChange(info.id)
      setNotice(t('ui_bot_attached_telegram_accepted', { name: info?.label || t('ui_the_bot') }))
    } catch (err) {
      setError(err instanceof Error ? err.message : t('ui_telegram_refused_this_token'))
    } finally {
      setBusy(false)
    }
  }

  const remove = async (id: string) => {
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      await api.deleteBotToken(id)
      await reloadBots()
      if (value === id) onChange('')
      setNotice(t('ui_bot_removed_its_token_is_gone_from_this_account'))
    } catch (err) {
      // "still used by N task(s)" arrives here: the server refuses to pull a
      // credential out from under a running task.
      setError(err instanceof Error ? err.message : t('ui_could_not_remove_this_bot'))
    } finally {
      setBusy(false)
    }
  }

  const rowStyle: React.CSSProperties = {
    display: 'flex',
    gap: '6px',
    alignItems: 'center',
    marginTop: '8px',
  }
  const smallButton: React.CSSProperties = {
    display: 'flex',
    alignItems: 'center',
    gap: '4px',
    backgroundColor: '#0f172a',
    color: '#e2e8f0',
    border: '1px solid #334155',
    borderRadius: '8px',
    padding: '7px 10px',
    fontSize: '11px',
    fontWeight: 600,
    cursor: busy ? 'wait' : 'pointer',
    whiteSpace: 'nowrap',
  }

  return (
    <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column' }}>
      <div style={{ display: 'flex', gap: '6px', alignItems: 'center' }}>
        <select
          style={{ ...inputStyle, flex: 1 }}
          value={value ?? ''}
          onChange={(e) => onChange(e.target.value)}
          disabled={!bots.length}
        >
          <option value="">
            {bots.length ? t('ui_none') : t('ui_no_bot_attached_yet')}
          </option>
          {bots.map((b) => (
            <option key={b.id} value={b.id}>
              {b.label}
            </option>
          ))}
        </select>
        {selected && (
          <button
            type="button"
            title={t('ui_remove_this_bot_from_the_account')}
            onClick={() => void remove(selected.id)}
            disabled={busy}
            style={{ ...smallButton, color: '#fca5a5' }}
          >
            <Trash2 size={12} />
          </button>
        )}
        <button type="button" onClick={() => setOpen((v) => !v)} style={smallButton}>
          <KeyRound size={12} />
          {open ? t('ui_cancel') : t('ui_attach_bot')}
        </button>
      </div>

      {open && (
        <div
          style={{
            marginTop: '8px',
            padding: '10px',
            borderRadius: '10px',
            border: '1px solid #334155',
            backgroundColor: '#0f172a',
          }}
        >
          <div style={hintStyle}>
            {t('ui_paste_the_token_botfather_gave_you_it_is_sent_once_checked_with')}
          </div>
          <div style={rowStyle}>
            <input
              style={{ ...inputStyle, maxWidth: '34%' }}
              placeholder={t('ui_label_optional')}
              value={label}
              onChange={(e) => setLabel(e.target.value)}
            />
            <input
              style={{ ...inputStyle, flex: 1 }}
              type="password"
              autoComplete="off"
              spellCheck={false}
              placeholder="123456789:AA…"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault()
                  void attach()
                }
              }}
            />
            <button
              type="button"
              onClick={() => void attach()}
              disabled={busy || !token.trim()}
              style={{
                ...smallButton,
                backgroundColor: token.trim() && !busy ? '#2563eb' : '#1e293b',
                color: token.trim() && !busy ? '#fff' : 'var(--color-text-muted)',
                cursor: token.trim() && !busy ? 'pointer' : 'not-allowed',
              }}
            >
              {busy ? <Loader2 size={12} /> : <Check size={12} />} {t('ui_add')}
            </button>
          </div>
          {botRoute && (
            <div style={{ ...hintStyle, marginTop: '6px' }}>
              {t('ui_this_task_delivers_as_a_bot_so_it_needs_one_attached_before_the')}
            </div>
          )}
          {bots.length > 0 && (
            <div style={{ ...hintStyle, marginTop: '6px' }}>
              {t('ui_attached')} {bots.map((b) => b.label).join(', ')}
            </div>
          )}
        </div>
      )}

      {error && (
        <div
          style={{
            display: 'flex',
            gap: '6px',
            alignItems: 'center',
            color: '#fca5a5',
            fontSize: '11px',
            marginTop: '6px',
          }}
        >
          <AlertCircle size={12} />
          <span>{error}</span>
        </div>
      )}
      {notice && !error && (
        <div style={{ fontSize: '11px', color: '#86efac', marginTop: '6px' }}>{notice}</div>
      )}
      {botRoute && !bots.length && !open && (
        <div style={{ ...hintStyle, color: '#fbbf24' }}>
          {t('ui_this_task_is_set_to_deliver_as_a_bot_but_none_is_attached_attach')}
        </div>
      )}
    </div>
  )
}
