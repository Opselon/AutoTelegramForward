/**
 * Shell widgets that are not panels: the user bar, the provider-key probe and
 * the prompt-template editor.
 *
 * The AI panel needs two of them; a lazy chunk importing the shell would be a
 * cycle, so they live in their own module. Moved verbatim from `App.tsx`.
 */
import { Fragment, useState } from 'react'
import { CheckCircle2, Crown, FlaskConical, HeartPulse, Loader2, LogOut, PenLine, Play, Save, Star, Trash2, User, XCircle } from 'lucide-react'
import { api } from '../api'
import { clearSession } from '../session'
import { ConfirmDialog } from './ConfirmDialog'
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

/**
 * The signed-in identity, rendered inline in the header's top row (it used to
 * be a separate card above the header — a whole extra row of chrome on a
 * phone, and a second /api/auth/me call for data the shell already had).
 * The identity comes from the shell's verified session; only logout lives here.
 */
export function UserBar({
  lang,
  user,
}: {
  lang: UiLang
  /** The shell's verified identity; null until /api/auth/me resolves. */
  user: { username: string; is_admin: boolean } | null
}) {
  const t = makeT(lang)

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
    // Styling lives in the `.userbar` classes (index.css): an inline identity,
    // not a card — the header already provides the surface.
    <div className="userbar">
      <div className="userbar__id">
        <span className="userbar__dot" aria-hidden="true" />
        <span className="userbar__name">
          <span
            className={`userbar__glyph${user.is_admin ? ' userbar__glyph--admin' : ''}`}
            aria-hidden="true"
          >
            {user.is_admin ? <Crown size={13} /> : <User size={13} />}
          </span>
          {user.username}
        </span>
        {user.is_admin && <span className="userbar__role">{t('ui_role_admin')}</span>}
      </div>
      <button
        type="button"
        onClick={handleLogout}
        className="btn btn--danger btn--sm userbar__logout"
        aria-label={t('ui_logout')}
        title={t('ui_logout')}
      >
        <LogOut size={14} aria-hidden="true" />
        <span className="userbar__logout-label">{t('ui_logout')}</span>
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
        style={{ alignSelf: 'flex-start', padding: compact ? '4px 8px' : '6px 10px', minHeight: 44, fontSize: '0.78rem' }}
      >
        <HeartPulse size={14} />
        <span>
          {busy
            ? t('ui_checking')
            : t('ui_check_key_endpoint')}
        </span>
      </button>
      {failure && <span style={{ fontSize: 'var(--type-xs)', color: 'var(--color-danger)' }}>{failure}</span>}
      {view.text && (
        <span
          style={{
            fontSize: 'var(--type-xs)',
            color: view.ok ? 'var(--color-success)' : 'var(--color-danger)',
            display: 'flex',
            gap: 6,
            flexWrap: 'wrap',
          }}
        >
          <span>
            {view.ok ? '✓' : '✕'} {view.text}
          </span>
          {view.detail && <span style={{ color: 'var(--color-text-muted)', overflowWrap: 'anywhere' }}>{view.detail}</span>}
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
  // The test message starts empty: the operator sends their own text, no
  // prefilled sample ships in the bundle.
  const [testText, setTestText] = useState('')
  const [testResult, setTestResult] = useState<TestPromptResponse | null>(null)
  const [testing, setTesting] = useState(false)
  const [saving, setSaving] = useState(false)
  // Deleting a prompt is destructive: the trash button only arms the shared
  // alertdialog; the actual DELETE happens on confirm.
  const [confirmDelete, setConfirmDelete] = useState<PromptTemplate | null>(null)

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
          <h2 style={{ fontSize: 'var(--type-xl)', fontWeight: 800, display: 'flex', alignItems: 'center', gap: 10 }}>
            <PenLine size={20} aria-hidden="true" /> {t('ui_rewrite_prompt_editor')}
          </h2>
          <p style={{ fontSize: 'var(--type-sm)', color: 'var(--color-text-muted)', marginTop: 4 }}>
            {/*
              The catalog sentence carries a ⭐ legend marker; it is split out here
              and rendered as the same SVG star the prompt rows use, so no emoji
              reaches the screen.
            */}
            {(() => {
              const parts = t('ui_reusable_rewrite_prompts_for_forward_rules_system_prompts_read_o').split('⭐')
              return parts.map((part, index) => (
                <Fragment key={index}>
                  {part}
                  {index < parts.length - 1 && (
                    <Star
                      size={12}
                      aria-hidden="true"
                      style={{ display: 'inline-flex', verticalAlign: '-1px', marginInline: 3, color: 'var(--color-warning)' }}
                    />
                  )}
                </Fragment>
              ))
            })()}
          </p>
        </div>
        <button onClick={() => openEditor(null)} className="btn btn-primary" style={{ padding: '8px 14px' }}>
          + {t('ui_new_prompt')}
        </button>
      </div>

      {/* Prompt list */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(260px, 1fr))', gap: 12, marginBottom: 20 }}>
        {(Array.isArray(prompts) ? prompts : []).map((p) => (
          <div
            key={p.id}
            role="button"
            tabIndex={0}
            aria-label={p.name}
            style={{
              background: 'var(--color-surface-2)', border: selected?.id === p.id ? '1px solid var(--color-accent-violet)' : '1px solid var(--color-border)',
              borderRadius: 10, padding: 14, cursor: 'pointer',
            }}
            onClick={() => openEditor(p)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault()
                openEditor(p)
              }
            }}
          >
            <div style={{ fontWeight: 700, fontSize: 'var(--type-sm)', marginBottom: 4, display: 'flex', alignItems: 'center', gap: 5 }}>
              <span style={{ display: 'inline-flex', color: p.is_system ? 'var(--color-warning)' : 'var(--color-text-muted)' }} aria-hidden="true">
                {p.is_system ? <Star size={12} /> : <User size={12} />}
              </span>
              {p.name} <span style={{ color: 'var(--color-text-muted)', fontWeight: 400 }}>v{p.current_version}</span>
            </div>
            <div style={{ fontSize: 'var(--type-xs)', color: 'var(--color-text-muted)', overflow: 'hidden', display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical' }}>
              {p.description || p.system_prompt.slice(0, 100)}
            </div>
            {!p.is_system && (
              <button
                onClick={(e) => { e.stopPropagation(); setConfirmDelete(p) }}
                className="btn btn-danger icon-btn"
                aria-label={t('ui_delete')}
                style={{ padding: 4, marginTop: 8, float: 'right', minHeight: 44, minWidth: 44, display: 'inline-flex', alignItems: 'center', justifyContent: 'center' }}
                title={t('ui_delete')}
              >
                <Trash2 size={13} aria-hidden="true" />
              </button>
            )}
          </div>
        ))}
      </div>

      {/* Editor form */}
      {form && form.system_prompt !== undefined && (
        <div style={{ borderTop: '1px solid var(--color-border)', paddingTop: 18 }}>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginBottom: 12 }}>
            <div>
              <label style={{ display: 'block', fontSize: 'var(--type-xs)', color: 'var(--color-text)', marginBottom: 4 }}>
                {t('ui_name')} *
              </label>
              <input className="input-field" style={{ width: '100%' }} value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder={t('ui_e_g_formal_rewrite')} />
            </div>
            <div>
              <label style={{ display: 'block', fontSize: 'var(--type-xs)', color: 'var(--color-text)', marginBottom: 4 }}>
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
            <label style={{ display: 'block', fontSize: 'var(--type-xs)', color: 'var(--color-text)', marginBottom: 4 }}>
              {t('ui_description')}
            </label>
            <input className="input-field" style={{ width: '100%' }} value={form.description}
              onChange={(e) => setForm({ ...form, description: e.target.value })} />
          </div>
          <div style={{ marginBottom: 12 }}>
            <label style={{ display: 'block', fontSize: 'var(--type-xs)', color: 'var(--color-text)', marginBottom: 4 }}>
              {t('ui_system_prompt')} *
              {selected?.is_system && <span style={{ color: 'var(--color-warning)' }}> — {t('ui_system_read_only')}</span>}
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
            <label style={{ display: 'block', fontSize: 'var(--type-xs)', color: 'var(--color-text)', marginBottom: 4 }}>
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
          <div style={{ background: 'var(--color-surface-2)', borderRadius: 10, padding: 14, border: '1px solid var(--color-border)' }}>
            <div style={{ fontSize: '0.78rem', fontWeight: 700, color: 'var(--color-primary-strong)', marginBottom: 8, display: 'flex', alignItems: 'center', gap: 6 }}>
              <FlaskConical size={13} aria-hidden="true" /> {t('ui_prompt_test_no_message_sent')}
            </div>
            <textarea className="input-field" style={{ width: '100%', minHeight: 60, marginBottom: 8, fontSize: '0.78rem' }}
              value={testText} onChange={(e) => setTestText(e.target.value)}
              placeholder={t('ui_prompt_test_no_message_sent')} />
            <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
              <button
                onClick={handleTest}
                disabled={testing || !selected || !aiConfigs.length}
                className="btn btn-secondary" style={{ padding: '6px 12px' }}
              >
                {testing ? <Loader2 size={13} className="spin" aria-hidden="true" /> : <Play size={13} aria-hidden="true" />} {t('ui_run_ai_test')}
              </button>
              {!aiConfigs.length && <span style={{ fontSize: 'var(--type-xs)', color: 'var(--color-danger)' }}>
                {t('ui_create_an_ai_config_first')}
              </span>}
            </div>
            {testResult && (
              <div style={{ marginTop: 10 }}>
                {testResult.success ? (
                  <div>
                    <div style={{ fontSize: 'var(--type-xs)', color: 'var(--color-text-muted)', marginBottom: 4, display: 'flex', alignItems: 'center', gap: 5 }}>
                      <CheckCircle2 size={12} aria-hidden="true" style={{ color: 'var(--color-success)', flexShrink: 0 }} />
                      {testResult.model_used} | {testResult.execution_duration_ms.toFixed(0)}ms | v{testResult.prompt_version_used}
                    </div>
                    <div style={{ background: 'var(--color-success-soft)', border: '1px solid color-mix(in oklab, var(--color-success) 40%, transparent)', borderRadius: 8, padding: 10, fontSize: 'var(--type-sm)', whiteSpace: 'pre-wrap' }}>
                      {testResult.transformed_text}
                    </div>
                  </div>
                ) : (
                  <div style={{ color: 'var(--color-danger)', fontSize: '0.78rem', display: 'flex', alignItems: 'center', gap: 5 }}>
                    <XCircle size={12} aria-hidden="true" style={{ flexShrink: 0 }} />
                    {testResult.error_code}: {testResult.error_message}
                  </div>
                )}
              </div>
            )}
          </div>

          {!selected?.is_system && form.system_prompt && (
            <div style={{ display: 'flex', gap: 10, marginTop: 14 }}>
              <button onClick={handleSave} disabled={saving || !form.name.trim()} className="btn btn-primary" style={{ padding: '8px 16px' }}>
                <Save size={13} aria-hidden="true" /> {saving ? (t('ui_app_saving')) : (t('ui_save_prompt'))}
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

      <ConfirmDialog
        open={confirmDelete !== null}
        lang={lang}
        title={t('ui_delete')}
        body={confirmDelete?.name}
        confirmLabel={t('ui_delete')}
        cancelLabel={t('ui_cancel')}
        onConfirm={() => {
          const target = confirmDelete
          setConfirmDelete(null)
          if (target) void handleDelete(target.id)
        }}
        onCancel={() => setConfirmDelete(null)}
      />
    </div>
  )
}

