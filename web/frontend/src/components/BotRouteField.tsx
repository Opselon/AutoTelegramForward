import { useState } from 'react'
import { AlertCircle, Check, KeyRound, Loader2, Trash2 } from 'lucide-react'
import { api } from '../api'
import type { BotTokenInfo } from '../types'

interface Props {
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
  bots,
  value,
  inputStyle,
  hintStyle,
  onChange,
  reloadBots,
  botRoute,
}: Props) {
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
      setNotice(`Attached ${info?.label || 'the bot'} — Telegram accepted it.`)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Telegram refused this token')
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
      setNotice('Bot removed. Its token is gone from this account.')
    } catch (err) {
      // "still used by N task(s)" arrives here: the server refuses to pull a
      // credential out from under a running task.
      setError(err instanceof Error ? err.message : 'Could not remove this bot')
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
          <option value="">{bots.length ? '— none —' : 'no bot attached yet'}</option>
          {bots.map((b) => (
            <option key={b.id} value={b.id}>
              {b.label}
            </option>
          ))}
        </select>
        {selected && (
          <button
            type="button"
            title="Remove this bot from the account"
            onClick={() => void remove(selected.id)}
            disabled={busy}
            style={{ ...smallButton, color: '#fca5a5' }}
          >
            <Trash2 size={12} />
          </button>
        )}
        <button type="button" onClick={() => setOpen((v) => !v)} style={smallButton}>
          <KeyRound size={12} />
          {open ? 'Cancel' : 'Attach bot'}
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
            Paste the token @BotFather gave you. It is sent once, checked with Telegram, and
            stored encrypted — it is never shown again, not even here.
          </div>
          <div style={rowStyle}>
            <input
              style={{ ...inputStyle, maxWidth: '34%' }}
              placeholder="label (optional)"
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
                color: token.trim() && !busy ? '#fff' : '#64748b',
                cursor: token.trim() && !busy ? 'pointer' : 'not-allowed',
              }}
            >
              {busy ? <Loader2 size={12} /> : <Check size={12} />} Add
            </button>
          </div>
          {botRoute && (
            <div style={{ ...hintStyle, marginTop: '6px' }}>
              This task delivers as a bot, so it needs one attached before the token can be
              cleared.
            </div>
          )}
          {bots.length > 0 && (
            <div style={{ ...hintStyle, marginTop: '6px' }}>
              Attached: {bots.map((b) => b.label).join(', ')}
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
          This task is set to deliver as a bot but none is attached — attach one, or switch the
          route back to the account.
        </div>
      )}
    </div>
  )
}
