/**
 * Shell widgets that are not panels: the user bar, the provider-key probe and
 * the prompt-template editor.
 *
 * The AI panel needs two of them; a lazy chunk importing the shell would be a
 * cycle, so they live in their own module. Moved verbatim from `App.tsx`.
 */
import { useEffect, useState } from 'react'
import { HeartPulse } from 'lucide-react'
import { api } from '../api'
import { clearSession } from '../session'
import { describeKeyCheck } from '../providerKey'
import { SUPPORTED_LANGS, makeT, type UiLang } from '../locales/engine'
import type { AIConfig, CheckProviderKeyResponse, PromptTemplate, TestPromptResponse } from '../types'

/**
 * v18 multi-source tasks forward from every listed channel, but only the
 * primary is rendered in the flow diagram. Surface the remainder as a count so
 * a task fed by 4 channels does not look identical to one fed by a single
 * channel.
 */
export function extraSourceCount(rule: any): number {
  const ids: string[] = Array.isArray(rule?.source_chat_ids) ? rule.source_chat_ids.filter(Boolean) : []
  if (ids.length > 1) return ids.length - 1
  const names: string[] = Array.isArray(rule?.source_chat_names) ? rule.source_chat_names.filter(Boolean) : []
  return names.length > 1 ? names.length - 1 : 0
}

export function UserBar({ lang }: { lang: UiLang }) {
  const t = makeT(lang)
  const [user, setUser] = useState<{ username: string; is_admin: boolean } | null>(null)

  useEffect(() => {
    api.me().then(setUser).catch(() => {})
  }, [])

  const handleLogout = () => {
    api.logout()
    // session-core is the one owner of the token: it removes the stored token
    // (by its real key, not a copy of the literal) and emits `session_cleared`.
    // `auth_required` is what Root listens to, so the dashboard still leaves.
    clearSession()
    window.dispatchEvent(new Event('auth_required'))
  }

  if (!user) return null
  return (
    <div
      style={{
        display: 'flex',
        justifyContent: 'space-between',
        alignItems: 'center',
        padding: '8px 16px',
        marginBottom: 16,
        background: 'rgba(15, 23, 42, 0.6)',
        borderRadius: 12,
        border: '1px solid rgba(255, 255, 255, 0.08)',
        fontSize: '0.85rem',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <span style={{ width: 8, height: 8, borderRadius: '50%', background: '#10b981', boxShadow: '0 0 8px #10b981' }} />
        <span style={{ color: '#e2e8f0', fontWeight: 600 }}>
          {user.is_admin ? '👑 ' : '👤 '}
          {user.username}
        </span>
        {user.is_admin && (
          <span style={{ fontSize: 'var(--type-xs)', padding: '2px 6px', borderRadius: 4, background: 'rgba(99, 102, 241, 0.2)', color: '#818cf8' }}>
            {t('ui_role_admin')}
          </span>
        )}
      </div>
      <button
        onClick={handleLogout}
        style={{
          padding: '4px 12px',
          borderRadius: 8,
          border: '1px solid rgba(239, 68, 68, 0.3)',
          background: 'rgba(239, 68, 68, 0.1)',
          cursor: 'pointer',
          fontSize: '0.8rem',
          color: '#f87171',
          fontWeight: 600,
        }}
      >
        {t('ui_logout')}
      </button>
    </div>
  )
}
// ===========================================================================
// AI Rewrite Prompt Editor & Tester (shared bot + dashboard logic via API)
// ===========================================================================
// ---------------------------------------------------------------- key check
//
// "Can this endpoint and key be used?" — asked before saving, and again for a
// stored config. The probe runs on the server (the key never leaves it) and the
// answer names its own refusal, so a tenant can fix the thing that is wrong
// instead of guessing.
export function ProviderKeyCheck({
  aiConfigId,
  provider,
  model,
  baseUrl,
  apiKey,
  lang,
  compact,
}: {
  aiConfigId?: string
  provider?: string
  model?: string
  baseUrl?: string
  apiKey?: string
  lang: UiLang
  compact?: boolean
}) {
  const t = makeT(lang)
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<CheckProviderKeyResponse | null>(null)
  const [failure, setFailure] = useState('')
  const view = describeKeyCheck(result, lang, !(apiKey || '').trim())

  const run = async () => {
    setBusy(true)
    setFailure('')
    setResult(null)
    try {
      setResult(
        await api.checkProviderKey({
          provider,
          model,
          base_url: baseUrl,
          api_key: apiKey,
          ai_config_id: aiConfigId,
        }),
      )
    } catch (err: any) {
      setFailure(String(err?.message || err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
      <button
        type="button"
        onClick={run}
        disabled={busy}
        className="btn btn-secondary"
        style={{ alignSelf: 'flex-start', padding: compact ? '4px 8px' : '6px 10px', fontSize: '0.78rem' }}
      >
        <HeartPulse size={14} />
        <span>
          {busy
            ? t('ui_checking')
            : t('ui_check_key_endpoint')}
        </span>
      </button>
      {failure && <span style={{ fontSize: '0.75rem', color: '#f87171' }}>{failure}</span>}
      {view.text && (
        <span
          style={{
            fontSize: '0.75rem',
            color: view.ok ? '#34d399' : '#f87171',
            display: 'flex',
            gap: 6,
            flexWrap: 'wrap',
          }}
        >
          <span>
            {view.ok ? '✓' : '✕'} {view.text}
          </span>
          {view.detail && <span style={{ color: '#94a3b8', overflowWrap: 'anywhere' }}>{view.detail}</span>}
        </span>
      )}
    </div>
  )
}

export function PromptEditorPanel({ prompts, aiConfigs, onSaved, onToast, lang }: {
  prompts: PromptTemplate[]
  aiConfigs: AIConfig[]
  onSaved: () => Promise<void>
  onToast: (msg: string) => void
  lang: UiLang
}) {
  const t = makeT(lang)
  const [selected, setSelected] = useState<PromptTemplate | null>(null)
  const [form, setForm] = useState({ name: '', description: '', system_prompt: '', user_prompt_template: '{text}', target_language: 'auto' })
  const [testText, setTestText] = useState('💎 XAUUSD SIGNAL: BUY NOW @ 2655 | TP 2680 | SL 2640 https://t.me/signal #gold')
  const [testResult, setTestResult] = useState<TestPromptResponse | null>(null)
  const [testing, setTesting] = useState(false)
  const [saving, setSaving] = useState(false)

  const openEditor = (p: PromptTemplate | null) => {
    setSelected(p)
    setTestResult(null)
    setForm(p ? {
      name: p.name, description: p.description || '',
      system_prompt: p.system_prompt,
      user_prompt_template: p.user_prompt_template || '{text}',
      target_language: p.target_language || 'auto',
    } : { name: '', description: '', system_prompt: '', user_prompt_template: '{text}', target_language: 'auto' })
  }

  const handleSave = async () => {
    if (!form.system_prompt.trim()) return
    setSaving(true)
    try {
      await api.savePromptTemplate({ ...form, id: selected?.id, current_version: selected?.current_version } as any)
      onToast(t('ui_prompt_saved'))
      await onSaved()
      openEditor(null)
    } catch (e: any) {
      onToast(e.message || t('ui_error_saving_prompt'))
    } finally {
      setSaving(false)
    }
  }

  const handleDelete = async (id: string) => {
    try {
      await api.deletePromptTemplate(id)
      onToast(t('ui_prompt_deleted'))
      await onSaved()
      if (selected?.id === id) openEditor(null)
    } catch (e: any) {
      onToast(e.message || t('ui_cannot_delete_system_prompt'))
    }
  }

  const handleTest = async () => {
    if (!selected || !aiConfigs.length) return
    setTesting(true)
    setTestResult(null)
    try {
      const r = await api.testPrompt({
        prompt_id: selected.id,
        ai_config_id: aiConfigs[0].id,
        sample_text: testText,
        version: 0,
      })
      setTestResult(r)
    } catch (e: any) {
      onToast(e.message || t('ui_test_failed'))
    } finally {
      setTesting(false)
    }
  }

  return (
    <div className="glass-panel" style={{ padding: 24 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
        <div>
          <h2 style={{ fontSize: '1.25rem', fontWeight: 800, display: 'flex', alignItems: 'center', gap: 10 }}>
            ✍️ {t('ui_rewrite_prompt_editor')}
          </h2>
          <p style={{ fontSize: '0.85rem', color: '#94a3b8', marginTop: 4 }}>
            {t('ui_reusable_rewrite_prompts_for_forward_rules_system_prompts_read_o')}
          </p>
        </div>
        <button onClick={() => openEditor(null)} className="btn btn-primary" style={{ padding: '8px 14px' }}>
          + {t('ui_new_prompt')}
        </button>
      </div>

      {/* Prompt list */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(260px, 1fr))', gap: 12, marginBottom: 20 }}>
        {(Array.isArray(prompts) ? prompts : []).map((p) => (
          <div key={p.id} style={{
            background: 'rgba(15, 23, 42, 0.6)', border: selected?.id === p.id ? '1px solid rgba(168,85,247,0.6)' : '1px solid rgba(255,255,255,0.06)',
            borderRadius: 10, padding: 14, cursor: 'pointer',
          }} onClick={() => openEditor(p)}>
            <div style={{ fontWeight: 700, fontSize: '0.85rem', marginBottom: 4 }}>
              {p.is_system ? '⭐' : '👤'} {p.name} <span style={{ color: 'var(--color-text-muted)', fontWeight: 400 }}>v{p.current_version}</span>
            </div>
            <div style={{ fontSize: 'var(--type-xs)', color: '#94a3b8', overflow: 'hidden', display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical' }}>
              {p.description || p.system_prompt.slice(0, 100)}
            </div>
            {!p.is_system && (
              <button
                onClick={(e) => { e.stopPropagation(); handleDelete(p.id) }}
                className="btn btn-danger icon-btn" style={{ padding: 4, marginTop: 8, float: 'right' }}
                title={t('ui_delete')}
              >
                🗑
              </button>
            )}
          </div>
        ))}
      </div>

      {/* Editor form */}
      {form && (selected === null || selected !== null) && form.system_prompt !== undefined && (
        <div style={{ borderTop: '1px solid rgba(255,255,255,0.06)', paddingTop: 18 }}>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginBottom: 12 }}>
            <div>
              <label style={{ display: 'block', fontSize: '0.75rem', color: '#cbd5e1', marginBottom: 4 }}>
                {t('ui_name')} *
              </label>
              <input className="input-field" style={{ width: '100%' }} value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder={t('ui_e_g_formal_rewrite')} />
            </div>
            <div>
              <label style={{ display: 'block', fontSize: '0.75rem', color: '#cbd5e1', marginBottom: 4 }}>
                {t('ui_output_language')}
              </label>
              <select className="input-field" style={{ width: '100%' }} value={form.target_language}
                onChange={(e) => setForm({ ...form, target_language: e.target.value })}>
                <option value="auto">{t('ui_auto_detect')}</option>
                {SUPPORTED_LANGS.filter((l) => l.code === 'fa' || l.code === 'en').map((l) => (
                  <option key={l.code} value={l.code}>{l.endonym}</option>
                ))}
              </select>
            </div>
          </div>
          <div style={{ marginBottom: 12 }}>
            <label style={{ display: 'block', fontSize: '0.75rem', color: '#cbd5e1', marginBottom: 4 }}>
              {t('ui_description')}
            </label>
            <input className="input-field" style={{ width: '100%' }} value={form.description}
              onChange={(e) => setForm({ ...form, description: e.target.value })} />
          </div>
          <div style={{ marginBottom: 12 }}>
            <label style={{ display: 'block', fontSize: '0.75rem', color: '#cbd5e1', marginBottom: 4 }}>
              {t('ui_system_prompt')} *
              {selected?.is_system && <span style={{ color: '#f59e0b' }}> — {t('ui_system_read_only')}</span>}
            </label>
            <textarea
              className="input-field"
              style={{ width: '100%', minHeight: 110, resize: 'vertical', fontFamily: 'monospace', fontSize: '0.78rem' }}
              value={form.system_prompt}
              readOnly={!!selected?.is_system}
              onChange={(e) => setForm({ ...form, system_prompt: e.target.value })}
              placeholder={t('ui_rewrite_the_text_preserving_numbers_links_and_names_never_invent')}
            />
          </div>
          <div style={{ marginBottom: 12 }}>
            <label style={{ display: 'block', fontSize: '0.75rem', color: '#cbd5e1', marginBottom: 4 }}>
              {t('ui_user_prompt_template')}
            </label>
            <input className="input-field" style={{ width: '100%', fontFamily: 'monospace', fontSize: '0.78rem' }}
              value={form.user_prompt_template} readOnly={!!selected?.is_system}
              onChange={(e) => setForm({ ...form, user_prompt_template: e.target.value })} placeholder="{text}" />
            <div style={{ fontSize: 'var(--type-xs)', color: 'var(--color-text-muted)', marginTop: 4 }}>
              {t('ui_text_is_replaced_with_the_message_body')}
            </div>
          </div>

          {/* Test panel */}
          <div style={{ background: 'rgba(15, 23, 42, 0.6)', borderRadius: 10, padding: 14, border: '1px solid rgba(255,255,255,0.06)' }}>
            <div style={{ fontSize: '0.78rem', fontWeight: 700, color: '#a5b4fc', marginBottom: 8 }}>
              🧪 {t('ui_prompt_test_no_message_sent')}
            </div>
            <textarea className="input-field" style={{ width: '100%', minHeight: 60, marginBottom: 8, fontSize: '0.78rem' }}
              value={testText} onChange={(e) => setTestText(e.target.value)} />
            <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
              <button
                onClick={handleTest}
                disabled={testing || !selected || !aiConfigs.length}
                className="btn btn-secondary" style={{ padding: '6px 12px' }}
              >
                {testing ? '⏳' : '▶'} {t('ui_run_ai_test')}
              </button>
              {!aiConfigs.length && <span style={{ fontSize: 'var(--type-xs)', color: '#f87171' }}>
                {t('ui_create_an_ai_config_first')}
              </span>}
            </div>
            {testResult && (
              <div style={{ marginTop: 10 }}>
                {testResult.success ? (
                  <div>
                    <div style={{ fontSize: 'var(--type-xs)', color: 'var(--color-text-muted)', marginBottom: 4 }}>
                      ✅ {testResult.model_used} | {testResult.execution_duration_ms.toFixed(0)}ms | v{testResult.prompt_version_used}
                    </div>
                    <div style={{ background: 'rgba(34,197,94,0.08)', border: '1px solid rgba(34,197,94,0.3)', borderRadius: 8, padding: 10, fontSize: '0.8rem', whiteSpace: 'pre-wrap' }}>
                      {testResult.transformed_text}
                    </div>
                  </div>
                ) : (
                  <div style={{ color: '#f87171', fontSize: '0.78rem' }}>
                    ❌ {testResult.error_code}: {testResult.error_message}
                  </div>
                )}
              </div>
            )}
          </div>

          {!selected?.is_system && form.system_prompt && (
            <div style={{ display: 'flex', gap: 10, marginTop: 14 }}>
              <button onClick={handleSave} disabled={saving || !form.name.trim()} className="btn btn-primary" style={{ padding: '8px 16px' }}>
                💾 {saving ? (t('ui_app_saving')) : (t('ui_save_prompt'))}
              </button>
              {selected && (
                <button onClick={() => openEditor(null)} className="btn btn-secondary" style={{ padding: '8px 16px' }}>
                  {t('ui_cancel')}
                </button>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

