/**
 * AI auto-reply (v28): the dashboard surface of the tenant-scoped subsystem.
 *
 * Five sections over the flat `/api/auto-reply/*` surface:
 *   Overview — behaviour, limits, schedule and the live counters.
 *   Prompts  — the account's private prompt library, incl. import/export.
 *   Contacts — per-chat switches with search and bulk selection.
 *   Rules    — ranked REPLY/SKIP rules with prompt/provider overrides.
 *   Activity — the durable queue's recent jobs with retry/cancel.
 *
 * Every visible string resolves through the catalog (the t() lookup): the JSX copy
 * gate in scripts/audit_web_ui_copy.js fails the build on anything else, which
 * is what keeps the ten locales honest.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import type { DashboardCtx } from '../App'
import { api } from '../api'
import type {
  AutoReplyJob,
  AutoReplyPrompt,
  AutoReplyPromptBundle,
  AutoReplyRule,
  AutoReplySettings,
  AutoReplyStats,
  AutoReplyTarget,
} from '../types'

type Section = 'overview' | 'prompts' | 'contacts' | 'rules' | 'activity'

const MODES = ['EVERYONE', 'SELECTED_ONLY', 'ALL_EXCEPT', 'MANUAL'] as const
const TONES = ['', 'friendly', 'casual', 'professional', 'formal', 'playful', 'empathetic', 'direct'] as const
const LENGTHS = ['', 'short', 'medium', 'long'] as const
const DAYS = [0, 1, 2, 3, 4, 5, 6] as const

const card: React.CSSProperties = { padding: 20 }
const rowStyle: React.CSSProperties = {
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'space-between',
  gap: 12,
  padding: '10px 0',
  borderBottom: '1px solid rgba(148, 163, 184, 0.12)',
}
const fieldStyle: React.CSSProperties = { display: 'flex', flexDirection: 'column', gap: 6 }
const labelStyle: React.CSSProperties = { fontSize: '0.78rem', fontWeight: 600, color: '#cbd5e1' }
const hintStyle: React.CSSProperties = { fontSize: '0.75rem', color: '#94a3b8', lineHeight: 1.4 }
const inputStyle: React.CSSProperties = {
  width: '100%',
  padding: '8px 10px',
  borderRadius: 10,
  border: '1px solid rgba(148, 163, 184, 0.25)',
  background: 'rgba(15, 23, 42, 0.6)',
  color: '#e2e8f0',
  fontSize: '0.85rem',
}

function fmtTime(ts: number): string {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  return d.toLocaleString()
}

export default function AutoReplyPanel({ d }: { d: DashboardCtx }) {
  const { t, isRtl, aiConfigs, showToast } = d

  const [section, setSection] = useState<Section>('overview')
  const [settings, setSettings] = useState<AutoReplySettings | null>(null)
  const [prompts, setPrompts] = useState<AutoReplyPrompt[]>([])
  const [targets, setTargets] = useState<AutoReplyTarget[]>([])
  const [rules, setRules] = useState<AutoReplyRule[]>([])
  const [stats, setStats] = useState<AutoReplyStats | null>(null)
  const [jobs, setJobs] = useState<AutoReplyJob[]>([])
  const [jobsTotal, setJobsTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  // prompts editor
  const [promptDraft, setPromptDraft] = useState<Partial<AutoReplyPrompt> | null>(null)
  const [importText, setImportText] = useState('')

  // contacts
  const [contactQuery, setContactQuery] = useState('')
  const [resolved, setResolved] = useState<AutoReplyTarget | null>(null)
  const [resolveError, setResolveError] = useState('')
  const [selectedChats, setSelectedChats] = useState<string[]>([])

  // rules
  const [ruleDraft, setRuleDraft] = useState<Partial<AutoReplyRule> | null>(null)

  // preview
  const [sampleText, setSampleText] = useState('')
  const [preview, setPreview] = useState('')
  const [previewBusy, setPreviewBusy] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const [s, p, tg, r, st, j] = await Promise.all([
        api.getAutoReplySettings(),
        api.listAutoReplyPrompts(),
        api.listAutoReplyTargets(),
        api.listAutoReplyRules(),
        api.getAutoReplyStats(),
        api.listAutoReplyJobs({ limit: 25 }),
      ])
      setSettings(s.settings)
      setPrompts(Array.isArray(p.prompts) ? p.prompts : [])
      setTargets(Array.isArray(tg.targets) ? tg.targets : [])
      setRules(Array.isArray(r.rules) ? r.rules : [])
      setStats(st.stats)
      setJobs(Array.isArray(j.jobs) ? j.jobs : [])
      setJobsTotal(Number(j.total || 0))
    } catch (e: any) {
      setError(e?.message || t('ui_ar_load_failed'))
    } finally {
      setLoading(false)
    }
  }, [t])

  useEffect(() => {
    void load()
  }, [load])

  const saveSettings = async (patch: Partial<AutoReplySettings>) => {
    if (!settings) return
    setBusy(true)
    try {
      const res = await api.updateAutoReplySettings(patch)
      setSettings(res.settings)
      showToast(t('ui_ar_settings_saved'))
    } catch (e: any) {
      showToast(e?.message || t('ui_ar_save_failed'))
    } finally {
      setBusy(false)
    }
  }

  const providerLabel = useMemo(() => {
    if (!settings?.ai_config_id) return t('ui_ar_provider_none_short')
    const found = (aiConfigs || []).find((c) => c.id === settings.ai_config_id)
    return found ? `${found.name} · ${found.model}` : settings.ai_config_id
  }, [aiConfigs, settings?.ai_config_id, t])

  const defaultPromptName = useMemo(() => {
    const hit = prompts.find((p) => p.id === settings?.prompt_id)
    return hit ? hit.name : t('ui_ar_prompt_builtin')
  }, [prompts, settings?.prompt_id, t])

  // ---------------------------------------------------------------- prompts
  const startNewPrompt = () => {
    setPromptDraft({
      name: '',
      description: '',
      system_prompt: '',
      user_prompt_template: '{text}',
      category: '',
      tags: [],
      tone: '',
      language: '',
      response_length: '',
    })
  }

  const savePrompt = async () => {
    if (!promptDraft) return
    setBusy(true)
    try {
      const res = await api.saveAutoReplyPrompt({
        id: promptDraft.id,
        name: (promptDraft.name || '').trim(),
        description: promptDraft.description || '',
        system_prompt: promptDraft.system_prompt || '',
        user_prompt_template: promptDraft.user_prompt_template || '{text}',
        category: promptDraft.category || '',
        tags: promptDraft.tags || [],
        tone: promptDraft.tone || '',
        language: promptDraft.language || '',
        response_length: promptDraft.response_length || '',
      })
      setPromptDraft(null)
      showToast(t('ui_ar_prompt_saved'))
      await load()
      return res
    } catch (e: any) {
      showToast(e?.message || t('ui_ar_save_failed'))
    } finally {
      setBusy(false)
    }
  }

  const makeDefault = async (promptId: string) => {
    await saveSettings({ prompt_id: promptId })
  }

  const duplicatePrompt = async (promptId: string) => {
    setBusy(true)
    try {
      await api.duplicateAutoReplyPrompt(promptId)
      showToast(t('ui_ar_prompt_duplicated'))
      await load()
    } catch (e: any) {
      showToast(e?.message || t('ui_ar_save_failed'))
    } finally {
      setBusy(false)
    }
  }

  const deletePrompt = async (promptId: string) => {
    setBusy(true)
    try {
      await api.deleteAutoReplyPrompt(promptId)
      showToast(t('ui_ar_prompt_deleted'))
      await load()
    } catch (e: any) {
      showToast(e?.message || t('ui_ar_save_failed'))
    } finally {
      setBusy(false)
    }
  }

  const exportPrompts = async () => {
    try {
      const res = await api.exportAutoReplyPrompts()
      const blob = new Blob([JSON.stringify(res.document, null, 2)], { type: 'application/json' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = 'atf-auto-reply-prompts.json'
      a.click()
      URL.revokeObjectURL(url)
      showToast(t('ui_ar_prompts_exported'))
    } catch (e: any) {
      showToast(e?.message || t('ui_ar_save_failed'))
    }
  }

  const importPrompts = async () => {
    let parsed: AutoReplyPromptBundle
    try {
      parsed = JSON.parse(importText)
    } catch {
      showToast(t('ui_ar_import_invalid_json'))
      return
    }
    setBusy(true)
    try {
      const res = await api.importAutoReplyPrompts(parsed)
      showToast(
        t('ui_ar_prompts_imported', {
          imported: res.imported ?? 0,
          skipped: res.skipped ?? 0,
          failed: res.failed ?? 0,
        }),
      )
      setImportText('')
      await load()
    } catch (e: any) {
      showToast(e?.message || t('ui_ar_save_failed'))
    } finally {
      setBusy(false)
    }
  }

  const runPreview = async () => {
    if (!sampleText.trim()) return
    setPreviewBusy(true)
    try {
      const res = await api.previewAutoReply(sampleText, settings?.prompt_id || '', settings?.ai_config_id || '')
      const r = res.result
      setPreview(r.ok ? r.reply_text : `${r.error_code || r.error}`)
    } catch (e: any) {
      setPreview(e?.message || t('ui_ar_preview_failed'))
    } finally {
      setPreviewBusy(false)
    }
  }

  // --------------------------------------------------------------- contacts
  const resolveContact = async () => {
    setResolveError('')
    setResolved(null)
    if (!contactQuery.trim()) return
    try {
      const res = await api.resolveAutoReplyChat(contactQuery.trim())
      if (res.found && res.target) setResolved(res.target)
      else setResolveError(t('ui_ar_resolve_failed'))
    } catch (e: any) {
      setResolveError(e?.message || t('ui_ar_resolve_failed'))
    }
  }

  const addResolved = async () => {
    if (!resolved) return
    setBusy(true)
    try {
      await api.addAutoReplyTargets([
        { chat_id: resolved.chat_id, title: resolved.title, username: resolved.username },
      ])
      setResolved(null)
      setContactQuery('')
      showToast(t('ui_ar_contact_added'))
      await load()
    } catch (e: any) {
      showToast(e?.message || t('ui_ar_save_failed'))
    } finally {
      setBusy(false)
    }
  }

  const toggleTarget = async (target: AutoReplyTarget) => {
    setBusy(true)
    try {
      await api.updateAutoReplyTarget(target.chat_id, { is_enabled: !target.is_enabled })
      await load()
    } catch (e: any) {
      showToast(e?.message || t('ui_ar_save_failed'))
    } finally {
      setBusy(false)
    }
  }

  const setTargetPrompt = async (target: AutoReplyTarget, promptId: string) => {
    try {
      await api.updateAutoReplyTarget(target.chat_id, { prompt_id: promptId })
      await load()
    } catch (e: any) {
      showToast(e?.message || t('ui_ar_save_failed'))
    }
  }

  const bulk = async (mode: 'enable' | 'disable' | 'remove') => {
    if (!selectedChats.length) return
    setBusy(true)
    try {
      if (mode === 'remove') {
        await api.removeAutoReplyTargets(selectedChats)
        showToast(t('ui_ar_contacts_removed', { count: selectedChats.length }))
      } else {
        await Promise.all(
          selectedChats.map((id) => api.updateAutoReplyTarget(id, { is_enabled: mode === 'enable' })),
        )
        showToast(t('ui_ar_settings_saved'))
      }
      setSelectedChats([])
      await load()
    } catch (e: any) {
      showToast(e?.message || t('ui_ar_save_failed'))
    } finally {
      setBusy(false)
    }
  }

  const visibleTargets = useMemo(() => {
    const q = contactQuery.trim().toLowerCase()
    const list = q && !resolved
      ? targets.filter(
          (x) =>
            (x.title || '').toLowerCase().includes(q) ||
            (x.username || '').toLowerCase().includes(q) ||
            x.chat_id.includes(q),
        )
      : targets
    return [...list].sort((a, b) => Number(b.is_enabled) - Number(a.is_enabled))
  }, [contactQuery, resolved, targets])

  // ------------------------------------------------------------------ rules
  const saveRule = async (draft: Partial<AutoReplyRule>) => {
    setBusy(true)
    try {
      let conditions: Record<string, any> = {}
      let exclusions: Record<string, any> = {}
      try {
        conditions = draft.conditions ? (draft.conditions as any) : {}
        exclusions = draft.exclusions ? (draft.exclusions as any) : {}
      } catch {
        showToast(t('ui_ar_rule_json_invalid'))
        setBusy(false)
        return
      }
      await api.saveAutoReplyRule({
        id: draft.id,
        name: (draft.name || '').trim(),
        is_enabled: draft.is_enabled !== false,
        priority: Number(draft.priority || 0),
        action: draft.action || 'REPLY',
        conditions,
        exclusions,
        prompt_id: draft.prompt_id || '',
        ai_config_id: draft.ai_config_id || '',
        cooldown_seconds: Number(draft.cooldown_seconds || 0),
        language: draft.language || '',
        tone: draft.tone || '',
        response_length: draft.response_length || '',
      })
      setRuleDraft(null)
      showToast(t('ui_ar_rule_saved'))
      await load()
    } catch (e: any) {
      showToast(e?.message || t('ui_ar_save_failed'))
    } finally {
      setBusy(false)
    }
  }

  const deleteRule = async (ruleId: string) => {
    setBusy(true)
    try {
      await api.deleteAutoReplyRule(ruleId)
      showToast(t('ui_ar_rule_deleted'))
      await load()
    } catch (e: any) {
      showToast(e?.message || t('ui_ar_save_failed'))
    } finally {
      setBusy(false)
    }
  }

  const moveRule = async (index: number, delta: number) => {
    const next = [...rules]
    const target = index + delta
    if (target < 0 || target >= next.length) return
    ;[next[index], next[target]] = [next[target], next[index]]
    setRules(next)
    try {
      const res = await api.reorderAutoReplyRules(next.map((r) => r.id))
      setRules(res.rules)
    } catch (e: any) {
      showToast(e?.message || t('ui_ar_save_failed'))
      await load()
    }
  }

  // --------------------------------------------------------------- activity
  const retryJob = async (jobId: string) => {
    try {
      await api.retryAutoReplyJob(jobId)
      showToast(t('ui_ar_job_retried'))
      await load()
    } catch (e: any) {
      showToast(e?.message || t('ui_ar_save_failed'))
    }
  }

  const cancelJob = async (jobId: string) => {
    try {
      await api.cancelAutoReplyJob(jobId)
      showToast(t('ui_ar_job_cancelled'))
      await load()
    } catch (e: any) {
      showToast(e?.message || t('ui_ar_save_failed'))
    }
  }

  // ------------------------------------------------------------------ render
  const tabs: { id: Section; key: string }[] = [
    { id: 'overview', key: 'ui_ar_tab_overview' },
    { id: 'prompts', key: 'ui_ar_tab_prompts' },
    { id: 'contacts', key: 'ui_ar_tab_contacts' },
    { id: 'rules', key: 'ui_ar_tab_rules' },
    { id: 'activity', key: 'ui_ar_tab_activity' },
  ]

  if (loading && !settings) {
    return (
      <div className="glass-panel" style={card}>
        <p style={{ color: '#94a3b8' }}>{t('ui_loading')}</p>
      </div>
    )
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 18 }}>
      {/* header */}
      <div className="glass-panel" style={card}>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 16, alignItems: 'center', justifyContent: 'space-between' }}>
          <div>
            <h2 style={{ fontSize: '1.2rem', fontWeight: 800 }}>{t('ui_ar_title')}</h2>
            <p style={{ ...hintStyle, marginTop: 4 }}>{t('ui_ar_subtitle')}</p>
          </div>
          <button
            className="btn"
            disabled={busy || !settings}
            onClick={() => saveSettings({ enabled: !settings?.enabled })}
            style={{
              padding: '8px 16px',
              fontWeight: 700,
              background: settings?.enabled ? 'rgba(16, 185, 129, 0.2)' : 'rgba(239, 68, 68, 0.2)',
              color: settings?.enabled ? '#34d399' : '#f87171',
              border: settings?.enabled
                ? '1px solid rgba(16, 185, 129, 0.4)'
                : '1px solid rgba(239, 68, 68, 0.4)',
            }}
          >
            {settings?.enabled ? t('ui_ar_turn_off') : t('ui_ar_turn_on')}
          </button>
        </div>

        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))',
            gap: 12,
            marginTop: 18,
          }}
        >
          <Stat label={t('ui_ar_stat_sent_today')} value={stats?.sent_today ?? 0} />
          <Stat label={t('ui_ar_stat_pending')} value={stats?.pending ?? 0} />
          <Stat label={t('ui_ar_stat_retrying')} value={stats?.retrying ?? 0} />
          <Stat label={t('ui_ar_stat_dead')} value={stats?.dead_letter ?? 0} />
          <Stat
            label={t('ui_ar_stat_latency')}
            value={stats?.average_latency_ms ? `${Math.round(stats.average_latency_ms)} ms` : '—'}
          />
          <Stat label={t('ui_ar_stat_chats')} value={stats?.chats_enabled ?? 0} />
        </div>

        {error && <p style={{ color: '#f87171', fontSize: '0.8rem', marginTop: 12 }}>{error}</p>}
        {!settings?.ai_config_id && (
          <p style={{ color: '#fbbf24', fontSize: '0.8rem', marginTop: 12 }}>{t('ui_ar_provider_missing')}</p>
        )}
      </div>

      {/* tabs */}
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
        {tabs.map((tab) => (
          <button
            key={tab.id}
            className="btn"
            onClick={() => setSection(tab.id)}
            style={{
              padding: '7px 14px',
              fontSize: '0.82rem',
              fontWeight: 700,
              background: section === tab.id ? 'rgba(99, 102, 241, 0.25)' : 'rgba(148, 163, 184, 0.1)',
              border: section === tab.id ? '1px solid rgba(99, 102, 241, 0.5)' : '1px solid transparent',
              color: section === tab.id ? '#c7d2fe' : '#cbd5e1',
            }}
          >
            {t(tab.key)}
          </button>
        ))}
      </div>

      {/* ---------------------------------------------------------- overview */}
      {section === 'overview' && settings && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))', gap: 18 }}>
          <div className="glass-panel" style={card}>
            <h3 style={{ fontWeight: 800, marginBottom: 12 }}>{t('ui_ar_group_behaviour')}</h3>

            <div style={{ ...fieldStyle, marginBottom: 14 }}>
              <label style={labelStyle}>{t('ui_ar_provider')}</label>
              <select
                style={inputStyle}
                value={settings.ai_config_id || ''}
                onChange={(e) => saveSettings({ ai_config_id: e.target.value })}
              >
                <option value="">{t('ui_none')}</option>
                {(aiConfigs || []).map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name} · {c.model}
                  </option>
                ))}
              </select>
              <span style={hintStyle}>{providerLabel}</span>
            </div>

            <div style={{ ...fieldStyle, marginBottom: 14 }}>
              <label style={labelStyle}>{t('ui_ar_default_prompt')}</label>
              <select
                style={inputStyle}
                value={settings.prompt_id || ''}
                onChange={(e) => saveSettings({ prompt_id: e.target.value })}
              >
                <option value="">{t('ui_ar_prompt_builtin')}</option>
                {prompts.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.name}
                  </option>
                ))}
              </select>
              <span style={hintStyle}>{defaultPromptName}</span>
            </div>

            <div style={{ ...fieldStyle, marginBottom: 14 }}>
              <label style={labelStyle}>{t('ui_ar_target_mode')}</label>
              <select
                style={inputStyle}
                value={settings.target_mode}
                onChange={(e) => saveSettings({ target_mode: e.target.value as AutoReplySettings['target_mode'] })}
              >
                {MODES.map((m) => (
                  <option key={m} value={m}>
                    {t(`ui_ar_mode_${m.toLowerCase()}`)}
                  </option>
                ))}
              </select>
              <span style={hintStyle}>{t(`ui_ar_mode_${settings.target_mode.toLowerCase()}_hint`)}</span>
            </div>

            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
              <div style={fieldStyle}>
                <label style={labelStyle}>{t('ui_ar_tone')}</label>
                <select style={inputStyle} value={settings.tone} onChange={(e) => saveSettings({ tone: e.target.value })}>
                  {TONES.map((tone) => (
                    <option key={tone || 'auto'} value={tone}>
                      {tone ? t(`ui_ar_tone_${tone}`) : t('ui_ar_tone_default')}
                    </option>
                  ))}
                </select>
              </div>
              <div style={fieldStyle}>
                <label style={labelStyle}>{t('ui_ar_length')}</label>
                <select
                  style={inputStyle}
                  value={settings.response_length}
                  onChange={(e) => saveSettings({ response_length: e.target.value })}
                >
                  {LENGTHS.map((len) => (
                    <option key={len || 'auto'} value={len}>
                      {len ? t(`ui_ar_length_${len}`) : t('ui_ar_length_default')}
                    </option>
                  ))}
                </select>
              </div>
            </div>

            <div style={{ ...fieldStyle, marginTop: 14 }}>
              <label style={labelStyle}>{t('ui_ar_language')}</label>
              <input
                style={inputStyle}
                value={settings.language}
                placeholder={t('ui_ar_language_placeholder')}
                onChange={(e) => setSettings({ ...settings, language: e.target.value })}
                onBlur={(e) => saveSettings({ language: e.target.value })}
              />
              <span style={hintStyle}>{t('ui_ar_language_hint')}</span>
            </div>

            <div style={{ ...fieldStyle, marginTop: 14 }}>
              <label style={labelStyle}>{t('ui_ar_extra_instructions')}</label>
              <textarea
                rows={3}
                style={inputStyle}
                value={settings.extra_instructions}
                placeholder={t('ui_ar_extra_instructions_placeholder')}
                onChange={(e) => setSettings({ ...settings, extra_instructions: e.target.value })}
                onBlur={(e) => saveSettings({ extra_instructions: e.target.value })}
              />
            </div>

            <div style={{ ...rowStyle, marginTop: 10 }}>
              <div>
                <div style={labelStyle}>{t('ui_ar_memory')}</div>
                <div style={hintStyle}>{t('ui_ar_memory_hint')}</div>
              </div>
              <input
                type="checkbox"
                checked={!!settings.memory_enabled}
                onChange={(e) => saveSettings({ memory_enabled: e.target.checked })}
              />
            </div>

            <div style={{ ...rowStyle }}>
              <div>
                <div style={labelStyle}>{t('ui_ar_ignore_bots')}</div>
                <div style={hintStyle}>{t('ui_ar_ignore_bots_hint')}</div>
              </div>
              <input
                type="checkbox"
                checked={!!settings.ignore_bots}
                onChange={(e) => saveSettings({ ignore_bots: e.target.checked })}
              />
            </div>
          </div>

          <div className="glass-panel" style={card}>
            <h3 style={{ fontWeight: 800, marginBottom: 12 }}>{t('ui_ar_group_style')}</h3>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
              <Num
                label={t('ui_ar_temperature')}
                hint={t('ui_ar_temperature_hint')}
                value={settings.temperature}
                step={0.1}
                min={0}
                max={2}
                onCommit={(v) => saveSettings({ temperature: v })}
              />
              <Num
                label={t('ui_ar_max_tokens')}
                value={settings.max_tokens}
                step={50}
                min={50}
                max={4000}
                onCommit={(v) => saveSettings({ max_tokens: v })}
              />
              <Num
                label={t('ui_ar_context_messages')}
                hint={t('ui_ar_context_messages_hint')}
                value={settings.context_messages}
                step={1}
                min={0}
                max={40}
                onCommit={(v) => saveSettings({ context_messages: v })}
              />
              <Num
                label={t('ui_ar_max_reply_chars')}
                value={settings.max_reply_chars}
                step={50}
                min={100}
                max={4000}
                onCommit={(v) => saveSettings({ max_reply_chars: v })}
              />
            </div>

            <h3 style={{ fontWeight: 800, margin: '20px 0 12px' }}>{t('ui_ar_group_limits')}</h3>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
              <Num
                label={t('ui_ar_cooldown')}
                value={settings.cooldown_seconds}
                step={5}
                min={0}
                max={86400}
                onCommit={(v) => saveSettings({ cooldown_seconds: v })}
              />
              <Num
                label={t('ui_ar_daily_limit')}
                hint={t('ui_ar_daily_limit_hint')}
                value={settings.daily_limit_per_chat}
                step={10}
                min={0}
                max={10000}
                onCommit={(v) => saveSettings({ daily_limit_per_chat: v })}
              />
              <Num
                label={t('ui_ar_rate_limit')}
                value={settings.rate_limit_per_minute}
                step={1}
                min={0}
                max={600}
                onCommit={(v) => saveSettings({ rate_limit_per_minute: v })}
              />
              <Num
                label={t('ui_ar_concurrency')}
                value={settings.max_concurrency}
                step={1}
                min={1}
                max={50}
                onCommit={(v) => saveSettings({ max_concurrency: v })}
              />
              <Num
                label={t('ui_ar_max_attempts')}
                value={settings.max_attempts}
                step={1}
                min={1}
                max={10}
                onCommit={(v) => saveSettings({ max_attempts: v })}
              />
              <Num
                label={t('ui_ar_backoff')}
                value={settings.retry_backoff_seconds}
                step={5}
                min={0}
                max={3600}
                onCommit={(v) => saveSettings({ retry_backoff_seconds: v })}
              />
            </div>

            <h3 style={{ fontWeight: 800, margin: '20px 0 12px' }}>{t('ui_ar_group_schedule')}</h3>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
              <Num
                label={t('ui_ar_quiet_start')}
                hint={t('ui_ar_quiet_hint')}
                value={settings.quiet_hours_start}
                step={1}
                min={-1}
                max={23}
                onCommit={(v) => saveSettings({ quiet_hours_start: v })}
              />
              <Num
                label={t('ui_ar_quiet_end')}
                value={settings.quiet_hours_end}
                step={1}
                min={-1}
                max={23}
                onCommit={(v) => saveSettings({ quiet_hours_end: v })}
              />
            </div>
            <div style={{ ...fieldStyle, marginTop: 12 }}>
              <label style={labelStyle}>{t('ui_ar_active_days')}</label>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                {DAYS.map((day) => {
                  const on = settings.active_days.includes(day)
                  return (
                    <button
                      key={day}
                      className="btn"
                      style={{
                        padding: '4px 10px',
                        fontSize: '0.75rem',
                        background: on ? 'rgba(99, 102, 241, 0.28)' : 'rgba(148, 163, 184, 0.1)',
                        color: on ? '#c7d2fe' : '#94a3b8',
                      }}
                      onClick={() => {
                        const next = on
                          ? settings.active_days.filter((x) => x !== day)
                          : [...settings.active_days, day].sort((a, b) => a - b)
                        saveSettings({ active_days: next })
                      }}
                    >
                      {t(`ui_ar_day_${day}`)}
                    </button>
                  )
                })}
              </div>
              <span style={hintStyle}>{t('ui_ar_active_days_hint')}</span>
            </div>

            <div style={{ ...fieldStyle, marginTop: 14 }}>
              <label style={labelStyle}>{t('ui_ar_fallback_text')}</label>
              <input
                style={inputStyle}
                value={settings.fallback_text}
                placeholder={t('ui_ar_fallback_placeholder')}
                onChange={(e) => setSettings({ ...settings, fallback_text: e.target.value })}
                onBlur={(e) => saveSettings({ fallback_text: e.target.value })}
              />
            </div>

            <div style={{ ...rowStyle, marginTop: 10 }}>
              <div>
                <div style={labelStyle}>{t('ui_ar_typing')}</div>
                <div style={hintStyle}>{t('ui_ar_typing_hint')}</div>
              </div>
              <input
                type="checkbox"
                checked={!!settings.typing_simulation}
                onChange={(e) => saveSettings({ typing_simulation: e.target.checked })}
              />
            </div>
            {settings.typing_simulation && (
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginTop: 10 }}>
                <Num
                  label={t('ui_ar_typing_min')}
                  value={settings.typing_delay_min}
                  step={0.5}
                  min={0}
                  max={60}
                  onCommit={(v) => saveSettings({ typing_delay_min: v })}
                />
                <Num
                  label={t('ui_ar_typing_max')}
                  value={settings.typing_delay_max}
                  step={0.5}
                  min={0}
                  max={60}
                  onCommit={(v) => saveSettings({ typing_delay_max: v })}
                />
              </div>
            )}
          </div>

          {/* preview */}
          <div className="glass-panel" style={{ ...card, gridColumn: '1 / -1' }}>
            <h3 style={{ fontWeight: 800, marginBottom: 8 }}>{t('ui_ar_preview_title')}</h3>
            <p style={{ ...hintStyle, marginBottom: 10 }}>{t('ui_ar_preview_hint')}</p>
            <textarea
              rows={2}
              style={inputStyle}
              value={sampleText}
              placeholder={t('ui_ar_preview_placeholder')}
              onChange={(e) => setSampleText(e.target.value)}
            />
            <div style={{ display: 'flex', gap: 10, marginTop: 10 }}>
              <button className="btn" disabled={previewBusy || !sampleText.trim()} onClick={runPreview}>
                {previewBusy ? t('ui_loading') : t('ui_ar_preview_run')}
              </button>
            </div>
            {preview && (
              <div
                style={{
                  marginTop: 12,
                  padding: 12,
                  borderRadius: 12,
                  background: 'rgba(15, 23, 42, 0.65)',
                  border: '1px solid rgba(148, 163, 184, 0.2)',
                  whiteSpace: 'pre-wrap',
                  fontSize: '0.85rem',
                  color: '#e2e8f0',
                }}
              >
                {preview}
              </div>
            )}
          </div>
        </div>
      )}

      {/* ----------------------------------------------------------- prompts */}
      {section === 'prompts' && (
        <div className="glass-panel" style={card}>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 10, alignItems: 'center', justifyContent: 'space-between' }}>
            <div>
              <h3 style={{ fontWeight: 800 }}>{t('ui_ar_prompts_title')}</h3>
              <p style={{ ...hintStyle, marginTop: 4 }}>{t('ui_ar_prompts_hint')}</p>
            </div>
            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
              <button className="btn" onClick={startNewPrompt}>{t('ui_ar_new_prompt')}</button>
              <button className="btn" onClick={exportPrompts}>{t('ui_ar_export')}</button>
            </div>
          </div>

          {promptDraft && (
            <div
              style={{
                marginTop: 14,
                padding: 14,
                borderRadius: 12,
                border: '1px solid rgba(99, 102, 241, 0.35)',
                background: 'rgba(30, 41, 59, 0.45)',
              }}
            >
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
                <div style={fieldStyle}>
                  <label style={labelStyle}>{t('ui_name')}</label>
                  <input
                    style={inputStyle}
                    value={promptDraft.name || ''}
                    onChange={(e) => setPromptDraft({ ...promptDraft, name: e.target.value })}
                  />
                </div>
                <div style={fieldStyle}>
                  <label style={labelStyle}>{t('ui_ar_prompt_category')}</label>
                  <input
                    style={inputStyle}
                    value={promptDraft.category || ''}
                    onChange={(e) => setPromptDraft({ ...promptDraft, category: e.target.value })}
                  />
                </div>
              </div>
              <div style={{ ...fieldStyle, marginTop: 12 }}>
                <label style={labelStyle}>{t('ui_ar_prompt_description')}</label>
                <input
                  style={inputStyle}
                  value={promptDraft.description || ''}
                  onChange={(e) => setPromptDraft({ ...promptDraft, description: e.target.value })}
                />
              </div>
              <div style={{ ...fieldStyle, marginTop: 12 }}>
                <label style={labelStyle}>{t('ui_ar_prompt_system')}</label>
                <textarea
                  rows={6}
                  style={inputStyle}
                  value={promptDraft.system_prompt || ''}
                  onChange={(e) => setPromptDraft({ ...promptDraft, system_prompt: e.target.value })}
                />
              </div>
              <div style={{ ...fieldStyle, marginTop: 12 }}>
                <label style={labelStyle}>{t('ui_ar_prompt_template')}</label>
                <input
                  style={inputStyle}
                  value={promptDraft.user_prompt_template || ''}
                  onChange={(e) => setPromptDraft({ ...promptDraft, user_prompt_template: e.target.value })}
                />
                <span style={hintStyle}>{t('ui_ar_prompt_template_hint')}</span>
              </div>
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 12, marginTop: 12 }}>
                <div style={fieldStyle}>
                  <label style={labelStyle}>{t('ui_ar_tone')}</label>
                  <select
                    style={inputStyle}
                    value={promptDraft.tone || ''}
                    onChange={(e) => setPromptDraft({ ...promptDraft, tone: e.target.value })}
                  >
                    {TONES.map((tone) => (
                      <option key={tone || 'auto'} value={tone}>
                        {tone ? t(`ui_ar_tone_${tone}`) : t('ui_ar_tone_default')}
                      </option>
                    ))}
                  </select>
                </div>
                <div style={fieldStyle}>
                  <label style={labelStyle}>{t('ui_ar_length')}</label>
                  <select
                    style={inputStyle}
                    value={promptDraft.response_length || ''}
                    onChange={(e) => setPromptDraft({ ...promptDraft, response_length: e.target.value })}
                  >
                    {LENGTHS.map((len) => (
                      <option key={len || 'auto'} value={len}>
                        {len ? t(`ui_ar_length_${len}`) : t('ui_ar_length_default')}
                      </option>
                    ))}
                  </select>
                </div>
                <div style={fieldStyle}>
                  <label style={labelStyle}>{t('ui_ar_language')}</label>
                  <input
                    style={inputStyle}
                    value={promptDraft.language || ''}
                    onChange={(e) => setPromptDraft({ ...promptDraft, language: e.target.value })}
                  />
                </div>
              </div>
              <div style={{ display: 'flex', gap: 8, marginTop: 14 }}>
                <button className="btn" disabled={busy} onClick={savePrompt}>{t('ui_save')}</button>
                <button className="btn" onClick={() => setPromptDraft(null)}>{t('ui_cancel')}</button>
              </div>
            </div>
          )}

          <div style={{ marginTop: 14 }}>
            {prompts.length === 0 && <p style={{ ...hintStyle }}>{t('ui_ar_prompts_empty')}</p>}
            {prompts.map((p) => (
              <div key={p.id} style={rowStyle}>
                <div style={{ minWidth: 0 }}>
                  <div style={{ ...labelStyle, fontSize: '0.88rem' }}>
                    {p.name}
                    {settings?.prompt_id === p.id && (
                      <span style={{ marginInlineStart: 8, color: '#34d399', fontSize: '0.75rem' }}>
                        {t('ui_ar_default_badge')}
                      </span>
                    )}
                    {p.is_archived && (
                      <span style={{ marginInlineStart: 8, color: '#94a3b8', fontSize: '0.75rem' }}>
                        {t('ui_ar_archived_badge')}
                      </span>
                    )}
                  </div>
                  <div style={{ ...hintStyle, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {p.system_prompt}
                  </div>
                </div>
                <div style={{ display: 'flex', gap: 6, flexShrink: 0 }}>
                  {settings?.prompt_id !== p.id && (
                    <button className="btn" style={{ fontSize: '0.75rem' }} onClick={() => makeDefault(p.id)}>
                      {t('ui_ar_make_default')}
                    </button>
                  )}
                  <button className="btn" style={{ fontSize: '0.75rem' }} onClick={() => setPromptDraft(p)}>
                    {t('ui_edit')}
                  </button>
                  <button className="btn" style={{ fontSize: '0.75rem' }} onClick={() => duplicatePrompt(p.id)}>
                    {t('ui_ar_duplicate')}
                  </button>
                  <button className="btn" style={{ fontSize: '0.75rem', color: '#f87171' }} onClick={() => deletePrompt(p.id)}>
                    {t('ui_delete')}
                  </button>
                </div>
              </div>
            ))}
          </div>

          <div style={{ marginTop: 18 }}>
            <label style={labelStyle}>{t('ui_ar_import')}</label>
            <p style={{ ...hintStyle, marginBottom: 8 }}>{t('ui_ar_import_hint')}</p>
            <textarea
              rows={3}
              style={inputStyle}
              value={importText}
              placeholder={t('ui_ar_import_placeholder')}
              onChange={(e) => setImportText(e.target.value)}
            />
            <button className="btn" style={{ marginTop: 8 }} disabled={busy || !importText.trim()} onClick={importPrompts}>
              {t('ui_ar_import_run')}
            </button>
          </div>
        </div>
      )}

      {/* ---------------------------------------------------------- contacts */}
      {section === 'contacts' && (
        <div className="glass-panel" style={card}>
          <h3 style={{ fontWeight: 800 }}>{t('ui_ar_targets_title')}</h3>
          <p style={{ ...hintStyle, marginTop: 4 }}>{t('ui_ar_targets_hint')}</p>

          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginTop: 14 }}>
            <input
              style={{ ...inputStyle, maxWidth: 320 }}
              value={contactQuery}
              placeholder={t('ui_ar_add_contact_placeholder')}
              onChange={(e) => {
                setContactQuery(e.target.value)
                setResolved(null)
                setResolveError('')
              }}
            />
            <button className="btn" onClick={resolveContact}>{t('ui_ar_find')}</button>
            {resolved && (
              <button className="btn" disabled={busy} onClick={addResolved}>
                {t('ui_ar_add_contact')}
              </button>
            )}
          </div>
          {resolveError && <p style={{ color: '#f87171', fontSize: '0.78rem', marginTop: 8 }}>{resolveError}</p>}
          {resolved && (
            <p style={{ ...hintStyle, marginTop: 8 }}>
              {resolved.title} · {resolved.chat_id}
            </p>
          )}

          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginTop: 14, alignItems: 'center' }}>
            <span style={hintStyle}>{t('ui_ar_selected_count', { count: selectedChats.length })}</span>
            <button
              className="btn"
              style={{ fontSize: '0.75rem' }}
              onClick={() =>
                setSelectedChats(
                  selectedChats.length === visibleTargets.length ? [] : visibleTargets.map((x) => x.chat_id),
                )
              }
            >
              {t('ui_ar_select_all')}
            </button>
            <button className="btn" style={{ fontSize: '0.75rem' }} disabled={!selectedChats.length} onClick={() => bulk('enable')}>
              {t('ui_enabled')}
            </button>
            <button className="btn" style={{ fontSize: '0.75rem' }} disabled={!selectedChats.length} onClick={() => bulk('disable')}>
              {t('ui_disabled')}
            </button>
            <button
              className="btn"
              style={{ fontSize: '0.75rem', color: '#f87171' }}
              disabled={!selectedChats.length}
              onClick={() => bulk('remove')}
            >
              {t('ui_remove')}
            </button>
          </div>

          <div style={{ marginTop: 12 }}>
            {visibleTargets.length === 0 && <p style={hintStyle}>{t('ui_ar_targets_empty')}</p>}
            {visibleTargets.map((target) => (
              <div key={target.chat_id} style={rowStyle}>
                <div style={{ display: 'flex', gap: 10, alignItems: 'center', minWidth: 0 }}>
                  <input
                    type="checkbox"
                    checked={selectedChats.includes(target.chat_id)}
                    onChange={(e) =>
                      setSelectedChats(
                        e.target.checked
                          ? [...selectedChats, target.chat_id]
                          : selectedChats.filter((x) => x !== target.chat_id),
                      )
                    }
                  />
                  <div style={{ minWidth: 0 }}>
                    <div style={labelStyle}>{target.title || target.chat_id}</div>
                    <div style={hintStyle}>
                      {target.username ? `@${target.username} · ` : ''}
                      {target.chat_id}
                    </div>
                  </div>
                </div>
                <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexShrink: 0 }}>
                  <select
                    style={{ ...inputStyle, width: 170 }}
                    value={target.prompt_id || ''}
                    onChange={(e) => setTargetPrompt(target, e.target.value)}
                  >
                    <option value="">{t('ui_ar_prompt_inherit')}</option>
                    {prompts.map((p) => (
                      <option key={p.id} value={p.id}>
                        {p.name}
                      </option>
                    ))}
                  </select>
                  <button
                    className="btn"
                    style={{
                      fontSize: '0.75rem',
                      color: target.is_enabled ? '#34d399' : '#94a3b8',
                    }}
                    onClick={() => toggleTarget(target)}
                  >
                    {target.is_enabled ? t('ui_enabled') : t('ui_disabled')}
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* ------------------------------------------------------------- rules */}
      {section === 'rules' && (
        <div className="glass-panel" style={card}>
          <div style={{ display: 'flex', justifyContent: 'space-between', flexWrap: 'wrap', gap: 10 }}>
            <div>
              <h3 style={{ fontWeight: 800 }}>{t('ui_ar_rules_title')}</h3>
              <p style={{ ...hintStyle, marginTop: 4 }}>{t('ui_ar_rules_hint')}</p>
            </div>
            <button
              className="btn"
              onClick={() =>
                setRuleDraft({
                  name: '',
                  is_enabled: true,
                  priority: 0,
                  action: 'REPLY',
                  conditions: {},
                  exclusions: {},
                  prompt_id: '',
                  ai_config_id: '',
                  cooldown_seconds: 0,
                  language: '',
                  tone: '',
                  response_length: '',
                })
              }
            >
              {t('ui_ar_new_rule')}
            </button>
          </div>

          {ruleDraft && (
            <div
              style={{
                marginTop: 14,
                padding: 14,
                borderRadius: 12,
                border: '1px solid rgba(99, 102, 241, 0.35)',
                background: 'rgba(30, 41, 59, 0.45)',
              }}
            >
              <div style={{ display: 'grid', gridTemplateColumns: '2fr 1fr 1fr', gap: 12 }}>
                <div style={fieldStyle}>
                  <label style={labelStyle}>{t('ui_ar_rule_name')}</label>
                  <input
                    style={inputStyle}
                    value={ruleDraft.name || ''}
                    onChange={(e) => setRuleDraft({ ...ruleDraft, name: e.target.value })}
                  />
                </div>
                <div style={fieldStyle}>
                  <label style={labelStyle}>{t('ui_ar_rule_priority')}</label>
                  <input
                    type="number"
                    style={inputStyle}
                    value={Number(ruleDraft.priority || 0)}
                    onChange={(e) => setRuleDraft({ ...ruleDraft, priority: Number(e.target.value) })}
                  />
                </div>
                <div style={fieldStyle}>
                  <label style={labelStyle}>{t('ui_ar_rule_action')}</label>
                  <select
                    style={inputStyle}
                    value={ruleDraft.action || 'REPLY'}
                    onChange={(e) => setRuleDraft({ ...ruleDraft, action: e.target.value })}
                  >
                    <option value="REPLY">{t('ui_ar_action_reply')}</option>
                    <option value="SKIP">{t('ui_ar_action_skip')}</option>
                  </select>
                </div>
              </div>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginTop: 12 }}>
                <div style={fieldStyle}>
                  <label style={labelStyle}>{t('ui_ar_rule_prompt')}</label>
                  <select
                    style={inputStyle}
                    value={ruleDraft.prompt_id || ''}
                    onChange={(e) => setRuleDraft({ ...ruleDraft, prompt_id: e.target.value })}
                  >
                    <option value="">{t('ui_ar_prompt_inherit')}</option>
                    {prompts.map((p) => (
                      <option key={p.id} value={p.id}>
                        {p.name}
                      </option>
                    ))}
                  </select>
                </div>
                <div style={fieldStyle}>
                  <label style={labelStyle}>{t('ui_ar_rule_provider')}</label>
                  <select
                    style={inputStyle}
                    value={ruleDraft.ai_config_id || ''}
                    onChange={(e) => setRuleDraft({ ...ruleDraft, ai_config_id: e.target.value })}
                  >
                    <option value="">{t('ui_ar_prompt_inherit')}</option>
                    {(aiConfigs || []).map((c) => (
                      <option key={c.id} value={c.id}>
                        {c.name}
                      </option>
                    ))}
                  </select>
                </div>
              </div>
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginTop: 12 }}>
                <div style={fieldStyle}>
                  <label style={labelStyle}>{t('ui_ar_rule_conditions')}</label>
                  <textarea
                    rows={4}
                    style={{ ...inputStyle, fontFamily: 'monospace' }}
                    value={JSON.stringify(ruleDraft.conditions || {}, null, 2)}
                    onChange={(e) => {
                      try {
                        setRuleDraft({ ...ruleDraft, conditions: JSON.parse(e.target.value) })
                      } catch {
                        /* keep the last valid object until the JSON parses */
                      }
                    }}
                  />
                </div>
                <div style={fieldStyle}>
                  <label style={labelStyle}>{t('ui_ar_rule_exclusions')}</label>
                  <textarea
                    rows={4}
                    style={{ ...inputStyle, fontFamily: 'monospace' }}
                    value={JSON.stringify(ruleDraft.exclusions || {}, null, 2)}
                    onChange={(e) => {
                      try {
                        setRuleDraft({ ...ruleDraft, exclusions: JSON.parse(e.target.value) })
                      } catch {
                        /* keep the last valid object until the JSON parses */
                      }
                    }}
                  />
                </div>
              </div>
              <div style={{ ...fieldStyle, marginTop: 12, maxWidth: 220 }}>
                <label style={labelStyle}>{t('ui_ar_rule_cooldown')}</label>
                <input
                  type="number"
                  style={inputStyle}
                  value={Number(ruleDraft.cooldown_seconds || 0)}
                  onChange={(e) => setRuleDraft({ ...ruleDraft, cooldown_seconds: Number(e.target.value) })}
                />
              </div>
              <div style={{ display: 'flex', gap: 8, marginTop: 14 }}>
                <button className="btn" disabled={busy} onClick={() => saveRule(ruleDraft)}>{t('ui_save')}</button>
                <button className="btn" onClick={() => setRuleDraft(null)}>{t('ui_cancel')}</button>
              </div>
            </div>
          )}

          <div style={{ marginTop: 14 }}>
            {rules.length === 0 && <p style={hintStyle}>{t('ui_ar_rules_empty')}</p>}
            {rules.map((rule, index) => (
              <div key={rule.id} style={rowStyle}>
                <div style={{ minWidth: 0 }}>
                  <div style={{ ...labelStyle, fontSize: '0.88rem' }}>
                    {rule.name}
                    <span style={{ marginInlineStart: 8, ...hintStyle }}>
                      {t('ui_ar_rule_priority_short', { priority: rule.priority })}
                    </span>
                    <span style={{ marginInlineStart: 8, color: rule.action === 'SKIP' ? '#fbbf24' : '#34d399', fontSize: '0.75rem' }}>
                      {rule.action}
                    </span>
                  </div>
                  <div style={hintStyle}>
                    {Object.keys(rule.conditions || {}).length
                      ? Object.keys(rule.conditions).join(', ')
                      : t('ui_ar_rule_no_conditions')}
                  </div>
                </div>
                <div style={{ display: 'flex', gap: 6, flexShrink: 0 }}>
                  <button className="btn" style={{ fontSize: '0.75rem' }} onClick={() => moveRule(index, -1)}>↑</button>
                  <button className="btn" style={{ fontSize: '0.75rem' }} onClick={() => moveRule(index, 1)}>↓</button>
                  <button className="btn" style={{ fontSize: '0.75rem' }} onClick={() => setRuleDraft(rule)}>{t('ui_edit')}</button>
                  <button
                    className="btn"
                    style={{ fontSize: '0.75rem' }}
                    onClick={() => saveRule({ ...rule, is_enabled: !rule.is_enabled })}
                  >
                    {rule.is_enabled ? t('ui_enabled') : t('ui_disabled')}
                  </button>
                  <button className="btn" style={{ fontSize: '0.75rem', color: '#f87171' }} onClick={() => deleteRule(rule.id)}>
                    {t('ui_delete')}
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* ---------------------------------------------------------- activity */}
      {section === 'activity' && (
        <div className="glass-panel" style={card}>
          <div style={{ display: 'flex', justifyContent: 'space-between', flexWrap: 'wrap', gap: 10 }}>
            <div>
              <h3 style={{ fontWeight: 800 }}>{t('ui_ar_jobs_title')}</h3>
              <p style={{ ...hintStyle, marginTop: 4 }}>
                {t('ui_ar_jobs_hint', { total: jobsTotal, last: stats?.last_sent_at ? fmtTime(stats.last_sent_at) : '—' })}
              </p>
            </div>
            <button className="btn" onClick={() => void load()}>{t('ui_retry')}</button>
          </div>

          <div style={{ marginTop: 12, overflowX: 'auto' }}>
            {jobs.length === 0 ? (
              <p style={hintStyle}>{t('ui_ar_jobs_empty')}</p>
            ) : (
              <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.8rem' }}>
                <thead>
                  <tr style={{ textAlign: isRtl ? 'right' : 'left', color: '#94a3b8' }}>
                    <th style={{ padding: '6px 8px' }}>{t('ui_ar_col_chat')}</th>
                    <th style={{ padding: '6px 8px' }}>{t('ui_status')}</th>
                    <th style={{ padding: '6px 8px' }}>{t('ui_ar_col_attempt')}</th>
                    <th style={{ padding: '6px 8px' }}>{t('ui_ar_col_latency')}</th>
                    <th style={{ padding: '6px 8px' }}>{t('ui_ar_col_time')}</th>
                    <th style={{ padding: '6px 8px' }}>{t('ui_actions')}</th>
                  </tr>
                </thead>
                <tbody>
                  {jobs.map((job) => (
                    <tr key={job.id} style={{ borderTop: '1px solid rgba(148, 163, 184, 0.12)' }}>
                      <td style={{ padding: '8px' }}>
                        <div style={{ color: '#e2e8f0' }}>{job.chat_title || job.chat_id}</div>
                        <div style={hintStyle}>{job.inbound_text}</div>
                        {job.error && <div style={{ color: '#f87171', fontSize: '0.75rem' }}>{job.error}</div>}
                      </td>
                      <td style={{ padding: '8px', color: statusColor(job.status), fontWeight: 700 }}>{job.status}</td>
                      <td style={{ padding: '8px' }}>
                        {job.attempt}/{job.max_attempts}
                      </td>
                      <td style={{ padding: '8px' }}>{job.latency_ms ? `${job.latency_ms} ms` : '—'}</td>
                      <td style={{ padding: '8px' }}>{fmtTime(job.sent_at || job.created_at)}</td>
                      <td style={{ padding: '8px' }}>
                        <div style={{ display: 'flex', gap: 6 }}>
                          {(job.status === 'DEAD_LETTER' || job.status === 'FAILED') && (
                            <button className="btn" style={{ fontSize: '0.75rem' }} onClick={() => retryJob(job.id)}>
                              {t('ui_retry')}
                            </button>
                          )}
                          {(job.status === 'PENDING' || job.status === 'RETRY_WAIT') && (
                            <button className="btn" style={{ fontSize: '0.75rem' }} onClick={() => cancelJob(job.id)}>
                              {t('ui_cancel')}
                            </button>
                          )}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

function statusColor(status: string): string {
  if (status === 'SENT') return '#34d399'
  if (status === 'DEAD_LETTER') return '#f87171'
  if (status === 'FAILED') return '#fbbf24'
  if (status === 'RUNNING') return '#818cf8'
  return '#94a3b8'
}

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <div
      style={{
        padding: '10px 12px',
        borderRadius: 12,
        background: 'rgba(15, 23, 42, 0.55)',
        border: '1px solid rgba(148, 163, 184, 0.16)',
      }}
    >
      <div style={{ fontSize: '0.75rem', color: '#94a3b8' }}>{label}</div>
      <div style={{ fontSize: '1.15rem', fontWeight: 800, color: '#e2e8f0' }}>{value}</div>
    </div>
  )
}

function Num({
  label,
  hint,
  value,
  step,
  min,
  max,
  onCommit,
}: {
  label: string
  hint?: string
  value: number
  step: number
  min: number
  max: number
  onCommit: (value: number) => void
}) {
  const [draft, setDraft] = useState(String(value ?? 0))
  useEffect(() => {
    setDraft(String(value ?? 0))
  }, [value])
  return (
    <div style={fieldStyle}>
      <label style={labelStyle}>{label}</label>
      <input
        type="number"
        style={inputStyle}
        value={draft}
        step={step}
        min={min}
        max={max}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={() => {
          const parsed = Number(draft)
          if (Number.isFinite(parsed) && parsed !== value) onCommit(parsed)
        }}
      />
      {hint && <span style={hintStyle}>{hint}</span>}
    </div>
  )
}
