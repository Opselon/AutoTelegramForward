import { useCallback, useEffect, useMemo, useState } from 'react'
import { AlertCircle, Check, Loader2, Plus, Save, X } from 'lucide-react'
import { api } from '../api'
import { ChatPicker } from './ChatPicker'
import type {
  BotTokenInfo,
  ForwardRule,
  RuleSettingSpec,
  RuleSettingsSchema,
  SettingSourceInfo,
} from '../types'
import { BotRouteField } from './BotRouteField'
import { badgeFor, canReset, isInherited, rowValue } from '../settingProvenance'
import { isRtlLang, makeT, type UiLang } from '../locales/engine'
import { SETTING_GROUPS, SETTING_HELP, SETTING_LABELS } from '../settingLabels.generated'

interface Props {
  rule: ForwardRule
  lang?: UiLang
  /** Sources/targets are rule fields, so they flow into the modal's own draft + save. */
  onRulePatch: (patch: Partial<ForwardRule>) => void
  onSaved?: () => void
}

/**
 * The heading of a settings group.
 *
 * The bot's catalog carries `ui_reg_group_<group>` for every group the core
 * ships — the same registry the panel reads its rows from — so the dashboard
 * takes the group's name from there, in all ten languages. A group added on the
 * server lands on the English fallback, never on a raw snake_case key.
 */
function groupLabel(group: string, lang: UiLang): string {
  return SETTING_GROUPS[group]?.[lang] ?? group.charAt(0).toUpperCase() + group.slice(1)
}

/**
 * The one normalisation the server applies before it compares two chats
 * (`normalizeChatID` in the Go API): drop the scheme, drop the t.me / telegram.me
 * host, drop a leading '@', trim, lowercase. The client mirrors it so a repeat
 * written two ways ("@Foo", "https://t.me/foo") is caught before the round-trip.
 */
function normalizeChat(raw: string): string {
  let s = String(raw ?? '').trim().toLowerCase()
  s = s.replace(/^[a-z]+:\/\//, '') // https:// / http://
  s = s.replace(/^(telegram\.me|t\.me)\//, '')
  s = s.replace(/^@+/, '')
  return s.trim()
}

function sourcesOf(rule: ForwardRule): string[] {
  const list = rule.source_chat_ids?.length ? rule.source_chat_ids : []
  if (list.length) return list
  return rule.source_chat_id ? [rule.source_chat_id] : []
}

/**
 * The target list, mirroring sourcesOf: `target_chat_ids` is authoritative,
 * `target_chat_id` is the fallback for a task that only ever set the scalar.
 */
function targetsOf(rule: ForwardRule): string[] {
  const list = rule.target_chat_ids?.length ? rule.target_chat_ids : []
  if (list.length) return list
  return rule.target_chat_id ? [rule.target_chat_id] : []
}

/** The names list is parallel to the ids list; keep it aligned on every edit. */
function namesFor(rule: ForwardRule, side: 'source' | 'target'): string[] {
  const names = side === 'source' ? rule.source_chat_names : rule.target_chat_names
  return Array.isArray(names) ? names : []
}

/**
 * The complete per-task control surface, in two halves that behave differently:
 *
 * - Sources and targets are fields on the rule itself, so they are edited into
 *   the parent's draft and saved with it (one request, no partial state). A task
 *   forwards FROM every source INTO every target.
 * - Settings are validated by the server against its own registry, so they are
 *   written immediately through the settings endpoint and the answer is the
 *   truth: a rejected value is shown instead of being kept optimistically.
 */
export function TaskControlPanel({ rule, lang = 'en', onRulePatch, onSaved }: Props) {
  const isRtl = lang === 'fa'
  const t = makeT(lang)
  const [schema, setSchema] = useState<RuleSettingsSchema | null>(null)
  // What the task itself stores, and separately what the forwarder will use with
  // its bot's defaults applied. The two are shown apart on purpose: saving a
  // value the task merely inherits would pin it and end the inheritance.
  const [settings, setSettings] = useState<Record<string, any>>({})
  // Named settingSources: `sources` is already the task's source chat list.
  const [settingSources, setSettingSources] = useState<Record<string, SettingSourceInfo>>({})
  const [edits, setEdits] = useState<Record<string, any>>({})
  const [resetting, setResetting] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  // The owner's attached bots. Loaded with the settings because the route's
  // `bot_token_id` field offers them by name; a failed read must not hide the
  // rest of the panel, so it is tracked separately from `error`.
  const [bots, setBots] = useState<BotTokenInfo[]>([])
  const [botsError, setBotsError] = useState<string | null>(null)

  const loadBots = useCallback(async () => {
    try {
      setBots(await api.listBotTokens())
      setBotsError(null)
    } catch (err) {
      setBots([])
      setBotsError(err instanceof Error ? err.message : 'Could not load your bots')
    }
  }, [])

  const sources = useMemo(() => sourcesOf(rule), [rule])
  const targets = useMemo(() => targetsOf(rule), [rule])

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await api.getRuleSettings(rule.id)
      if (res?.settings) setSettings(res.settings)
      if (res?.schema) setSchema(res.schema)
      // Where each value comes from. Absent on an older API, which simply means
      // the panel shows values without provenance rather than failing.
      setSettingSources(res?.sources ?? {})
      setEdits({})
    } catch (err) {
      setError(err instanceof Error ? err.message : t('ui_could_not_load_task_settings'))
    } finally {
      setLoading(false)
    }
  }, [rule.id])

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    void loadBots()
  }, [loadBots])

  const dirtyKeys = Object.keys(edits)

  const handleSaveSettings = async () => {
    if (!dirtyKeys.length) return
    setSaving(true)
    setError(null)
    setNotice(null)
    try {
      const res = await api.updateRuleSettings(rule.id, edits)
      if (res?.success === false) {
        // The server validates; surfacing its reason is the point of this panel.
        setError(
          res.message
            ? `${res.error_code ? res.error_code + ': ' : ''}${res.message}`
            : t('ui_the_server_rejected_these_settings'),
        )
        return
      }
      if (res?.settings) setSettings(res.settings)
      setSettingSources(res?.sources ?? settingSources)
      setEdits({})
      setNotice(res?.message || t('ui_settings_saved'))
      onSaved?.()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('ui_could_not_save_settings'))
    } finally {
      setSaving(false)
    }
  }

  /**
   * Hand one setting back to the bot's default.
   *
   * A reset is not a write: the server drops the task's own value so the bot
   * (or the registry) supplies it again. Saving the value the task was shown
   * instead would pin it and the task would stop following the bot.
   */
  const handleResetSetting = async (key: string) => {
    setResetting(key)
    setError(null)
    setNotice(null)
    try {
      const res = await api.updateRuleSettings(rule.id, {}, false, [key])
      if (res?.success === false) {
        setError(
          res.message
            ? `${res.error_code ? res.error_code + ': ' : ''}${res.message}`
            : t('ui_the_server_refused_to_reset_this_setting'),
        )
        return
      }
      if (res?.settings) setSettings(res.settings)
      setSettingSources(res?.sources ?? {})
      setEdits((prev: Record<string, any>) => {
        if (!(key in prev)) return prev
        const next = { ...prev }
        delete next[key]
        return next
      })
      setNotice(res?.message || t('ui_handed_back_to_the_default'))
      onSaved?.()
    } catch (err) {
      setError(err instanceof Error ? err.message : t('ui_could_not_reset_setting'))
    } finally {
      setResetting(null)
    }
  }

  const addSource = (value: string) => {
    const next = [...sources, value]
    onRulePatch({
      source_chat_ids: next,
      source_chat_id: next[0],
      source_chat_names: [...namesFor(rule, 'source'), value],
    })
  }

  const removeSource = (value: string) => {
    // A task needs at least one source: the primary is what the dispatcher and
    // older clients read, so it is never dropped by the UI.
    if (sources.length <= 1) return
    const idx = sources.indexOf(value)
    const next = sources.filter((s) => s !== value)
    onRulePatch({
      source_chat_ids: next,
      source_chat_id: next[0],
      source_chat_names: namesFor(rule, 'source').filter((_, i) => i !== idx),
    })
  }

  const addTarget = (value: string) => {
    const next = [...targets, value]
    onRulePatch({
      target_chat_ids: next,
      target_chat_id: next[0],
      target_chat_names: [...namesFor(rule, 'target'), value],
    })
  }

  const removeTarget = (value: string) => {
    // A task needs at least one target, mirroring the source side.
    if (targets.length <= 1) return
    const idx = targets.indexOf(value)
    const next = targets.filter((t) => t !== value)
    onRulePatch({
      target_chat_ids: next,
      target_chat_id: next[0],
      target_chat_names: namesFor(rule, 'target').filter((_, i) => i !== idx),
    })
  }

  // Client-side mirrors of the server's endpoint checks. They name the offending
  // entry and use the same normalisation the server does, so a linter here and
  // the server agree on what counts as a repeat or an overlap.
  const validateSource = (raw: string): string | null => {
    const n = normalizeChat(raw)
    if (!n) return t('ui_chat_required')
    if (sources.some((s) => normalizeChat(s) === n)) {
      return t('ui_chat_already_source', { chat: raw.trim() })
    }
    if (targets.some((t2) => normalizeChat(t2) === n)) {
      return t('ui_chat_source_target_overlap', { chat: raw.trim() })
    }
    return null
  }

  const validateTarget = (raw: string): string | null => {
    const n = normalizeChat(raw)
    if (!n) return t('ui_chat_required')
    if (targets.some((t2) => normalizeChat(t2) === n)) {
      return t('ui_chat_already_target', { chat: raw.trim() })
    }
    if (sources.some((s) => normalizeChat(s) === n)) {
      return t('ui_chat_source_target_overlap', { chat: raw.trim() })
    }
    return null
  }

  const grouped = useMemo(() => {
    if (!schema) return []
    const order = schema.groups.length
      ? schema.groups
      : Array.from(new Set(schema.settings.map((s) => s.group)))
    return order
      .map((group) => ({
        group,
        specs: schema.settings.filter((s) => s.group === group),
      }))
      .filter((g) => g.specs.length > 0)
  }, [schema])

  const card: React.CSSProperties = {
    backgroundColor: '#1e293b',
    borderRadius: '16px',
    padding: '16px',
    marginBottom: '14px',
    border: '1px solid #334155',
  }
  const labelStyle: React.CSSProperties = {
    fontSize: '12px',
    fontWeight: 600,
    color: '#e2e8f0',
  }
  const inputStyle: React.CSSProperties = {
    width: '100%',
    backgroundColor: '#0f172a',
    border: '1px solid #334155',
    borderRadius: '8px',
    color: '#f1f5f9',
    padding: '8px 10px',
    fontSize: '12px',
  }
  const hint: React.CSSProperties = { fontSize: '11px', color: '#94a3b8', marginTop: '2px' }

  /**
   * Add several chats at once from the browser.
   *
   * The browser already dims what this task uses, but the check is repeated
   * here with the same normalisation the server applies: one write per pick
   * would be N renders, and a duplicate would then be rejected by the server
   * with a message about a row the user never typed.
   */
  const addMany = (side: 'source' | 'target', picks: { id: string; title: string }[]) => {
    const current = side === 'source' ? sources : targets
    const seen = new Set(current.map((entry) => normalizeChat(entry)))
    const ids: string[] = []
    const names: string[] = []
    for (const pick of picks) {
      const key = normalizeChat(pick.id)
      if (!key || seen.has(key)) continue
      seen.add(key)
      ids.push(pick.id)
      names.push((pick.title || '').trim() || pick.id)
    }
    if (ids.length === 0) return
    const next = [...current, ...ids]
    const nextNames = [...namesFor(rule, side), ...names]
    if (side === 'source') {
      onRulePatch({ source_chat_ids: next, source_chat_id: next[0], source_chat_names: nextNames })
    } else {
      onRulePatch({ target_chat_ids: next, target_chat_id: next[0], target_chat_names: nextNames })
    }
  }

  return (
    <div dir={isRtl ? 'rtl' : 'ltr'}>
      {/* ---------------------------------------------------------- sources */}
      <div style={card}>
        <ChatListEditor
          title={t('ui_sources')}
          count={sources.length}
          hint={t('ui_this_task_forwards_from_every_source_below_into_every_target_mes')}
          items={sources}
          primaryLabel={t('ui_primary')}
          placeholder={t('ui_channel_t_me_xxx_or_1001234567890')}
          lang={lang}
          inputStyle={inputStyle}
          hintStyle={hint}
          canRemove={sources.length > 1}
          removeDisabledHint={t('ui_a_task_needs_at_least_one_source')}
          onAdd={addSource}
          onRemove={removeSource}
          validate={validateSource}
          sessionId={rule.session_id}
          existing={[...sources, ...targets]}
          onAddMany={(picks) => addMany('source', picks)}
        />
        <div style={hint}>{t('ui_save_the_task_to_apply_source_changes')}</div>
      </div>

      {/* ---------------------------------------------------------- targets */}
      <div style={card}>
        <ChatListEditor
          title={t('ui_targets')}
          count={targets.length}
          hint={t('ui_every_message_is_delivered_into_every_target_below_order_is_kept')}
          items={targets}
          primaryLabel={t('ui_primary')}
          placeholder={t('ui_channel_t_me_xxx_or_1001234567890')}
          lang={lang}
          inputStyle={inputStyle}
          hintStyle={hint}
          canRemove={targets.length > 1}
          removeDisabledHint={t('ui_a_task_needs_at_least_one_target')}
          onAdd={addTarget}
          onRemove={removeTarget}
          validate={validateTarget}
          sessionId={rule.session_id}
          existing={[...sources, ...targets]}
          onAddMany={(picks) => addMany('target', picks)}
        />
        <div style={hint}>{t('ui_save_the_task_to_apply_target_changes')}</div>
      </div>

      {/* --------------------------------------------------------- settings */}
      <div style={card}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <div>
            <div style={{ fontSize: '13px', fontWeight: 700, color: '#f8fafc' }}>
              {t('ui_all_task_settings')}
            </div>
            <div style={hint}>
              {schema
                ? t('ui_every_setting_this_task_supports_schema_2_validated_by_the_serve', { schema_2: schema.count })
                : t('ui_loading_the_settings_schema')}
            </div>
          </div>
          {loading && <Loader2 size={16} className="spin" color="#93c5fd" />}
        </div>

        {error && (
          <div
            style={{
              display: 'flex',
              gap: '8px',
              alignItems: 'flex-start',
              backgroundColor: '#450a0a',
              border: '1px solid #7f1d1d',
              borderRadius: '10px',
              padding: '10px',
              marginTop: '10px',
              color: '#fecaca',
              fontSize: '12px',
            }}
          >
            <AlertCircle size={14} style={{ marginTop: '2px', flexShrink: 0 }} />
            <span>{error}</span>
          </div>
        )}
        {notice && !error && (
          <div
            style={{
              display: 'flex',
              gap: '8px',
              alignItems: 'center',
              backgroundColor: '#052e16',
              border: '1px solid #166534',
              borderRadius: '10px',
              padding: '10px',
              marginTop: '10px',
              color: '#bbf7d0',
              fontSize: '12px',
            }}
          >
            <Check size={14} />
            <span>{notice}</span>
          </div>
        )}

        {grouped.map(({ group, specs }) => (
          <div key={group} style={{ marginTop: '14px' }}>
            <div
              style={{
                fontSize: '11px',
                fontWeight: 700,
                color: '#93c5fd',
                textTransform: 'uppercase',
                letterSpacing: '0.04em',
                marginBottom: '8px',
              }}
            >
              {groupLabel(group, lang)}
            </div>
            {specs.map((spec) => (
              <SettingRow
                key={spec.key}
                spec={spec}
                value={rowValue(spec.key, edits, settings, settingSources)}
                changed={spec.key in edits}
                sourceInfo={settingSources[spec.key]}
                resetting={resetting === spec.key}
                onReset={
                  canReset(spec.key, edits, settingSources)
                    ? () => void handleResetSetting(spec.key)
                    : undefined
                }
                lang={lang}
                labelStyle={labelStyle}
                inputStyle={inputStyle}
                hintStyle={hint}
                onChange={(value) => setEdits((prev) => ({ ...prev, [spec.key]: value }))}
                bots={bots}
                reloadBots={loadBots}
                botsError={botsError}
                botRoute={
                  String(
                    'delivery_transport' in edits ? edits.delivery_transport : settings.delivery_transport ?? '',
                  ) === 'BOT'
                }
              />
            ))}
          </div>
        ))}

        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: '10px',
            marginTop: '16px',
            borderTop: '1px solid #334155',
            paddingTop: '12px',
          }}
        >
          <button
            type="button"
            onClick={handleSaveSettings}
            disabled={!dirtyKeys.length || saving}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: '6px',
              backgroundColor: dirtyKeys.length && !saving ? '#2563eb' : '#1e293b',
              color: dirtyKeys.length && !saving ? '#fff' : 'var(--color-text-muted)',
              border: 'none',
              borderRadius: '10px',
              padding: '9px 16px',
              fontSize: '12px',
              fontWeight: 600,
              cursor: dirtyKeys.length && !saving ? 'pointer' : 'not-allowed',
            }}
          >
            {saving ? <Loader2 size={14} className="spin" /> : <Save size={14} />}
            {saving
              ? t('ui_app_saving')
              : `${t('ui_save_settings')}${dirtyKeys.length ? ` (${dirtyKeys.length})` : ''}`}
          </button>
          {dirtyKeys.length > 0 && (
            <button
              type="button"
              onClick={() => setEdits({})}
              style={{
                background: 'none',
                border: 'none',
                color: '#94a3b8',
                fontSize: '12px',
                cursor: 'pointer',
              }}
            >
              {t('ui_discard')}
            </button>
          )}
        </div>
      </div>
    </div>
  )
}

interface ChatListEditorProps {
  title: string
  count: number
  hint: string
  items: string[]
  primaryLabel: string
  placeholder: string
  lang: UiLang
  inputStyle: React.CSSProperties
  hintStyle: React.CSSProperties
  canRemove: boolean
  removeDisabledHint: string
  onAdd: (value: string) => void
  onRemove: (value: string) => void
  /** Returns an error message naming the offending entry, or null when it is ok. */
  validate: (raw: string) => string | null
  /** The rule's session, whose dialogs the chat browser lists. */
  sessionId: string
  /** Every id already used by this task (either side), so the browser can dim them. */
  existing: string[]
  /** Confirm in the browser with one or more picked chats. */
  onAddMany: (picks: { id: string; title: string }[]) => void
}

/**
 * One endpoint list (sources or targets): add an entry, remove one, keep the
 * order, show the mirrored primary. The same component renders both sides so
 * they cannot drift apart.
 */
function ChatListEditor({
  title,
  count,
  hint,
  items,
  primaryLabel,
  placeholder,
  lang,
  inputStyle,
  hintStyle,
  canRemove,
  removeDisabledHint,
  onAdd,
  onRemove,
  validate,
  sessionId,
  existing,
  onAddMany,
}: ChatListEditorProps) {
  const isRtl = isRtlLang(lang)
  const t = makeT(lang)
  const [draft, setDraft] = useState('')
  const [localError, setLocalError] = useState<string | null>(null)
  const [browsing, setBrowsing] = useState(false)

  const submit = () => {
    const value = draft.trim()
    if (!value) return
    const problem = validate(value)
    if (problem) {
      setLocalError(problem)
      return
    }
    onAdd(value)
    setDraft('')
    setLocalError(null)
  }

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '4px' }}>
        <span style={{ fontSize: '13px', fontWeight: 700, color: '#f8fafc' }}>{title}</span>
        <span
          style={{
            fontSize: '11px',
            color: '#93c5fd',
            backgroundColor: '#1e3a8a',
            borderRadius: '999px',
            padding: '2px 8px',
          }}
        >
          {count}
        </span>
      </div>
      <div style={{ ...hintStyle, marginBottom: '10px' }}>{hint}</div>

      {items.map((entry, idx) => (
        <div
          key={`${entry}-${idx}`}
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: '8px',
            backgroundColor: '#0f172a',
            borderRadius: '10px',
            padding: '8px 10px',
            marginBottom: '6px',
          }}
        >
          <span
            style={{
              fontFamily: 'monospace',
              fontSize: '12px',
              color: '#e2e8f0',
              flex: 1,
              direction: 'ltr',
              textAlign: isRtl ? 'right' : 'left',
            }}
          >
            {entry}
          </span>
          {idx === 0 && (
            <span style={{ fontSize: '10px', color: '#4ade80', fontWeight: 600 }}>
              {primaryLabel}
            </span>
          )}
          <button
            type="button"
            onClick={() => onRemove(entry)}
            disabled={!canRemove}
            title={canRemove ? t('ui_remove_name', { name: entry }) : removeDisabledHint}
            style={{
              background: 'none',
              border: 'none',
              cursor: canRemove ? 'pointer' : 'not-allowed',
              color: canRemove ? '#f87171' : 'var(--color-text-muted)',
              display: 'flex',
            }}
          >
            <X size={14} />
          </button>
        </div>
      ))}

      <div style={{ display: 'flex', gap: '8px', marginTop: '8px' }}>
        <input
          style={inputStyle}
          placeholder={placeholder}
          value={draft}
          onChange={(e) => {
            setDraft(e.target.value)
            if (localError) setLocalError(null)
          }}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault()
              submit()
            }
          }}
        />
        <button
          type="button"
          onClick={submit}
          disabled={!draft.trim()}
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: '4px',
            backgroundColor: draft.trim() ? '#2563eb' : '#1e293b',
            color: draft.trim() ? '#fff' : 'var(--color-text-muted)',
            border: '1px solid #334155',
            borderRadius: '8px',
            padding: '8px 12px',
            fontSize: '12px',
            fontWeight: 600,
            cursor: draft.trim() ? 'pointer' : 'not-allowed',
            whiteSpace: 'nowrap',
          }}
        >
          <Plus size={14} /> {t('ui_add')}
        </button>
      </div>
      {localError && (
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
          <span>{localError}</span>
        </div>
      )}

      {/*
        The browse affordance sits *below* the id field on purpose: the sheet is
        a convenience over typing, never a replacement for it. A session with no
        live client still configures a task by id.
      */}
      <button
        type="button"
        onClick={() => setBrowsing(true)}
        style={{
          marginTop: '8px',
          display: 'flex',
          alignItems: 'center',
          gap: '6px',
          background: 'none',
          border: '1px dashed #334155',
          borderRadius: '8px',
          color: '#93c5fd',
          padding: '7px 10px',
          fontSize: '11px',
          cursor: 'pointer',
          width: '100%',
          justifyContent: 'center',
        }}
      >
        {t('ui_browse_chats')}
      </button>

      {browsing && (
        <ChatPicker
          sessionId={sessionId}
          lang={lang}
          alreadyIn={existing}
          onPick={onAddMany}
          onClose={() => setBrowsing(false)}
        />
      )}
    </div>
  )
}

interface RowProps {
  spec: RuleSettingSpec
  value: any
  changed: boolean
  lang: UiLang
  /** Owner's bots, the pending route (for the BOT-without-token warning) and a reload hook. */
  bots?: BotTokenInfo[]
  botsError?: string | null
  botRoute?: boolean
  reloadBots?: () => Promise<void> | void
  /** Where this row's value comes from, when the API reports provenance. */
  sourceInfo?: SettingSourceInfo
  /** True while this row's reset is in flight. */
  resetting?: boolean
  /** Absent when there is nothing to hand back (the value is already a default). */
  onReset?: () => void
  labelStyle: React.CSSProperties
  inputStyle: React.CSSProperties
  hintStyle: React.CSSProperties
  onChange: (value: any) => void
}

function SettingRow({
  spec,
  value,
  changed,
  sourceInfo,
  resetting = false,
  onReset,
  lang,
  inputStyle,
  hintStyle,
  onChange,
  bots = [],
  botsError = null,
  botRoute = false,
  reloadBots,
}: RowProps) {
  const isRtl = isRtlLang(lang)
  const t = makeT(lang)
  const rowStyle: React.CSSProperties = {
    display: 'flex',
    alignItems: 'center',
    gap: '10px',
    padding: '7px 0',
    borderBottom: '1px solid #1f2937',
  }
  const badge = badgeFor(sourceInfo)
  const label = (
    <div style={{ flex: 1, minWidth: 0 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '6px', flexWrap: 'wrap' }}>
        <span
          style={{
            fontSize: '12px',
            // An inherited value is dimmer than one this task chose, so the two
            // never look alike.
            color: isInherited(sourceInfo) ? '#cbd5e1' : '#e2e8f0',
            fontWeight: changed ? 700 : 500,
          }}
        >
          {SETTING_LABELS[spec.key]?.[lang] ?? spec.label}
        </span>
        {changed && (
          <span style={{ fontSize: '9px', color: '#fbbf24', fontWeight: 700 }}>{t('ui_edited')}</span>
        )}
        {badge && !changed && (
          <span
            title={t(badge.titleKey)}
            style={{
              fontSize: '9px',
              color: badge.color,
              backgroundColor: badge.background,
              border: `1px solid ${badge.color}33`,
              borderRadius: '4px',
              padding: '1px 5px',
              fontWeight: 700,
              whiteSpace: 'nowrap',
            }}
          >
            {t(badge.textKey)}
            {badge.suffix ? ` ${badge.suffix}` : ''}
          </span>
        )}
        {onReset && !changed && (
          <button
            type="button"
            onClick={onReset}
            disabled={resetting}
            title={t('ui_use_the_default_for_this_setting')}
            style={{
              fontSize: '9px',
              color: resetting ? 'var(--color-text-muted)' : '#7dd3fc',
              background: 'none',
              border: '1px solid #334155',
              borderRadius: '4px',
              padding: '1px 5px',
              cursor: resetting ? 'default' : 'pointer',
            }}
          >
            {resetting ? '…' : t('ui_use_default')}
          </button>
        )}
      </div>
      {spec.help && (
        <div style={hintStyle}>
          {SETTING_HELP[spec.key]?.[lang] ?? spec.help}
        </div>
      )}
    </div>
  )

  if (spec.kind === 'bool') {
    return (
      <div style={rowStyle}>
        {label}
        <button
          type="button"
          onClick={() => onChange(!value)}
          aria-pressed={!!value}
          style={{
            width: '42px',
            height: '22px',
            borderRadius: '999px',
            border: 'none',
            cursor: 'pointer',
            backgroundColor: value ? '#22c55e' : 'var(--color-text-muted)',
            position: 'relative',
            flexShrink: 0,
          }}
        >
          <span
            style={{
              position: 'absolute',
              top: '3px',
              [isRtl ? 'right' : 'left']: value ? '23px' : '3px',
              width: '16px',
              height: '16px',
              borderRadius: '50%',
              backgroundColor: '#fff',
              transition: 'all 0.15s ease',
            }}
          />
        </button>
      </div>
    )
  }

  if (spec.kind === 'dict') {
    return (
      <div style={{ ...rowStyle, flexDirection: 'column', alignItems: 'stretch' }}>
        {label}
        <DictEditor
          spec={spec}
          value={value}
          lang={lang}
          inputStyle={inputStyle}
          hintStyle={hintStyle}
          onChange={onChange}
        />
      </div>
    )
  }

  if (spec.kind === 'list') {
    return (
      <div style={{ ...rowStyle, flexDirection: 'column', alignItems: 'stretch' }}>
        {label}
        <ListEditor
          spec={spec}
          value={value}
          lang={lang}
          inputStyle={inputStyle}
          hintStyle={hintStyle}
          onChange={onChange}
        />
      </div>
    )
  }

  if (spec.key === 'bot_token_id') {
    // A raw id box would be unusable: the value is a server-side id the user
    // never sees. Offer the attached bots by name, and an inline way to attach
    // a new one without leaving the task.
    return (
      <div style={{ ...rowStyle, alignItems: 'flex-start' }}>
        {label}
        <div style={{ maxWidth: '58%', flex: 1, minWidth: 0 }}>
          <BotRouteField
            lang={lang}
            bots={bots}
            value={value ?? ''}
            inputStyle={inputStyle}
            hintStyle={hintStyle}
            onChange={onChange}
            reloadBots={reloadBots ?? (() => undefined)}
            botRoute={botRoute}
          />
          {botsError && <div style={{ ...hintStyle, color: '#fca5a5' }}>{botsError}</div>}
        </div>
      </div>
    )
  }

  const numeric = spec.kind === 'int' || spec.kind === 'float'
  return (
    <div style={rowStyle}>
      {label}
      {spec.choices?.length ? (
        <select
          style={{ ...inputStyle, maxWidth: '52%' }}
          value={value ?? ''}
          onChange={(e) => onChange(e.target.value)}
        >
          <option value="">—</option>
          {spec.choices.map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </select>
      ) : (
        <input
          style={{ ...inputStyle, maxWidth: '52%' }}
          type={numeric ? 'number' : 'text'}
          step={spec.kind === 'float' ? 'any' : undefined}
          min={spec.minimum}
          max={spec.maximum}
          value={value ?? ''}
          onChange={(e) => {
            if (!numeric) {
              onChange(e.target.value)
              return
            }
            const raw = e.target.value
            if (raw === '') {
              onChange(spec.kind === 'int' ? 0 : 0)
              return
            }
            const parsed = Number(raw)
            if (!Number.isNaN(parsed)) onChange(spec.kind === 'int' ? Math.trunc(parsed) : parsed)
          }}
        />
      )}
    </div>
  )
}

interface ListEditorProps {
  spec: RuleSettingSpec
  lang: UiLang
  value: any
  inputStyle: React.CSSProperties
  hintStyle: React.CSSProperties
  onChange: (value: any) => void
}

/**
 * A list of words/patterns: add one entry at a time, no cap, remove any entry.
 * The whole list is kept in the edit buffer and written as an array, so a word
 * containing a comma (a regex alternation, say) is never split in half.
 */
function ListEditor({ spec, value, lang, inputStyle, hintStyle, onChange }: ListEditorProps) {
  const t = makeT(lang)
  const items: string[] = Array.isArray(value) ? value.map((v) => String(v)) : []
  const [draft, setDraft] = useState('')
  const [localError, setLocalError] = useState<string | null>(null)

  const add = () => {
    const entry = draft.trim()
    if (!entry) return
    if (items.includes(entry)) {
      setLocalError(t('ui_already_in_list', { entry }))
      return
    }
    onChange([...items, entry])
    setDraft('')
    setLocalError(null)
  }

  const removeAt = (idx: number) => {
    onChange(items.filter((_, i) => i !== idx))
    setLocalError(null)
  }

  return (
    <div style={{ marginTop: '6px' }}>
      {items.length > 0 && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px', marginBottom: '6px' }}>
          {items.map((entry, idx) => (
            <span
              key={`${entry}-${idx}`}
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: '4px',
                backgroundColor: '#0f172a',
                border: '1px solid #334155',
                borderRadius: '999px',
                padding: '2px 6px 2px 10px',
                fontSize: '11px',
                color: '#e2e8f0',
              }}
              dir="ltr"
            >
              {entry}
              <button
                type="button"
                onClick={() => removeAt(idx)}
                title={t('ui_remove_name', { name: entry })}
                style={{
                  background: 'none',
                  border: 'none',
                  cursor: 'pointer',
                  color: '#f87171',
                  display: 'flex',
                  padding: 0,
                }}
              >
                <X size={12} />
              </button>
            </span>
          ))}
        </div>
      )}
      <div style={{ display: 'flex', gap: '8px' }}>
        <input
          style={{ ...inputStyle, direction: 'ltr' }}
          value={draft}
          placeholder={
            spec.item_kind === 'str' ? t('ui_add_one_word') : t('ui_add_one_entry')
          }
          onChange={(e) => {
            setDraft(e.target.value)
            if (localError) setLocalError(null)
          }}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault()
              add()
            }
          }}
        />
        <button
          type="button"
          onClick={add}
          disabled={!draft.trim()}
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: '4px',
            backgroundColor: draft.trim() ? '#2563eb' : '#1e293b',
            color: draft.trim() ? '#fff' : 'var(--color-text-muted)',
            border: '1px solid #334155',
            borderRadius: '8px',
            padding: '8px 12px',
            fontSize: '12px',
            fontWeight: 600,
            cursor: draft.trim() ? 'pointer' : 'not-allowed',
            whiteSpace: 'nowrap',
          }}
        >
          <Plus size={14} /> {t('ui_add')}
        </button>
      </div>
      <div style={hintStyle}>
        {items.length === 0 ? t('ui_no_entries_yet') : t('ui_entries_count', { count: items.length })}
      </div>
      {localError && (
        <div
          style={{
            display: 'flex',
            gap: '6px',
            alignItems: 'center',
            color: '#fca5a5',
            fontSize: '11px',
            marginTop: '4px',
          }}
        >
          <AlertCircle size={12} />
          <span>{localError}</span>
        </div>
      )}
    </div>
  )
}

interface DictEditorProps {
  spec: RuleSettingSpec
  value: any
  lang: UiLang
  inputStyle: React.CSSProperties
  hintStyle: React.CSSProperties
  onChange: (value: any) => void
}

/**
 * A key -> value map (replacements, per-source / per-target overrides). Edited
 * as JSON: the raw text is kept while it does not parse yet, so a half-typed
 * object is not thrown away, and a red hint marks the invalid state.
 */
function DictEditor({ spec, value, lang, inputStyle, hintStyle, onChange }: DictEditorProps) {
  const t = makeT(lang)
  const text = typeof value === 'string' ? value : JSON.stringify(value ?? {}, null, 2)
  const invalid = typeof value === 'string'
  return (
    <div style={{ marginTop: '6px' }}>
      <textarea
        style={{
          ...inputStyle,
          minHeight: '64px',
          fontFamily: 'monospace',
          direction: 'ltr',
          borderColor: invalid ? '#7f1d1d' : '#334155',
        }}
        value={text}
        spellCheck={false}
        placeholder='{"key": "value"}'
        onChange={(e) => {
          const raw = e.target.value
          try {
            const parsed = JSON.parse(raw)
            // Only an object is a dict; a bare number/array is not this setting.
            onChange(parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : raw)
          } catch {
            // Keep the raw text so a half-typed object is not thrown away.
            onChange(raw)
          }
        }}
      />
      <div style={{ ...hintStyle, color: invalid ? '#fca5a5' : hintStyle.color }}>
        {invalid
          ? t('ui_not_valid_json_yet_fix_it_before_saving')
          : spec.help
            ? SETTING_HELP[spec.key]?.[lang] ?? spec.help
            : t('ui_json_object')}
      </div>
    </div>
  )
}
