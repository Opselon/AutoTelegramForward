import { useCallback, useEffect, useMemo, useState } from 'react'
import { AlertCircle, Check, Loader2, Plus, Save, X } from 'lucide-react'
import { api } from '../api'
import type { ForwardRule, RuleSettingSpec, RuleSettingsSchema } from '../types'

interface Props {
  rule: ForwardRule
  lang?: 'fa' | 'en'
  /** Sources are rule fields, so they flow into the modal's own draft + save. */
  onRulePatch: (patch: Partial<ForwardRule>) => void
  onSaved?: () => void
}

const GROUP_LABELS: Record<string, string> = {
  general: 'General',
  cleanup: 'Cleanup',
  media: 'Media',
  sending: 'Sending',
  keywords: 'Keywords & Replies',
  audience: 'Audience Filters',
  ai: 'AI',
  automation: 'Automation',
}

function groupLabel(group: string): string {
  return GROUP_LABELS[group] || group.charAt(0).toUpperCase() + group.slice(1)
}

function sourcesOf(rule: ForwardRule): string[] {
  const list = rule.source_chat_ids?.length ? rule.source_chat_ids : []
  if (list.length) return list
  return rule.source_chat_id ? [rule.source_chat_id] : []
}

/**
 * The complete per-task control surface, in two halves that behave differently:
 *
 * - Sources are fields on the rule itself, so they are edited into the parent's
 *   draft and saved with it (one request, no partial state).
 * - Settings are validated by the server against its own registry, so they are
 *   written immediately through the settings endpoint and the answer is the
 *   truth: a rejected value is shown instead of being kept optimistically.
 */
export function TaskControlPanel({ rule, lang = 'en', onRulePatch, onSaved }: Props) {
  const isRtl = lang === 'fa'
  const [schema, setSchema] = useState<RuleSettingsSchema | null>(null)
  const [settings, setSettings] = useState<Record<string, any>>({})
  const [edits, setEdits] = useState<Record<string, any>>({})
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [newSource, setNewSource] = useState('')

  const sources = useMemo(() => sourcesOf(rule), [rule])

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await api.getRuleSettings(rule.id)
      if (res?.settings) setSettings(res.settings)
      if (res?.schema) setSchema(res.schema)
      setEdits({})
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load task settings')
    } finally {
      setLoading(false)
    }
  }, [rule.id])

  useEffect(() => {
    void load()
  }, [load])

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
            : 'The server rejected these settings',
        )
        return
      }
      if (res?.settings) setSettings(res.settings)
      setEdits({})
      setNotice(res?.message || 'Settings saved')
      onSaved?.()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not save settings')
    } finally {
      setSaving(false)
    }
  }

  const addSource = () => {
    const value = newSource.trim()
    if (!value) return
    if (sources.includes(value)) {
      setNewSource('')
      return
    }
    const next = [...sources, value]
    onRulePatch({
      source_chat_ids: next,
      source_chat_names: [...(rule.source_chat_names || []), value],
    })
    setNewSource('')
  }

  const removeSource = (value: string) => {
    // A task needs at least one source: the primary is what the dispatcher and
    // older clients read, so it is never dropped by the UI.
    if (sources.length <= 1) return
    const next = sources.filter((s) => s !== value)
    onRulePatch({
      source_chat_ids: next,
      source_chat_ids_primary: undefined,
      source_chat_id: next[0],
      source_chat_names: (rule.source_chat_names || []).filter((_, i) => sources[i] !== value),
    } as Partial<ForwardRule>)
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

  return (
    <div dir={isRtl ? 'rtl' : 'ltr'}>
      {/* ---------------------------------------------------------- sources */}
      <div style={card}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '4px' }}>
          <span style={{ fontSize: '13px', fontWeight: 700, color: '#f8fafc' }}>Sources</span>
          <span
            style={{
              fontSize: '11px',
              color: '#93c5fd',
              backgroundColor: '#1e3a8a',
              borderRadius: '999px',
              padding: '2px 8px',
            }}
          >
            {sources.length}
          </span>
        </div>
        <div style={{ ...hint, marginBottom: '10px' }}>
          This task forwards from every source below into its single target. Messages from any of
          them are treated as the same task.
        </div>

        {sources.map((src, idx) => (
          <div
            key={`${src}-${idx}`}
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
              {src}
            </span>
            {idx === 0 && (
              <span style={{ fontSize: '10px', color: '#4ade80', fontWeight: 600 }}>PRIMARY</span>
            )}
            <button
              type="button"
              onClick={() => removeSource(src)}
              disabled={sources.length <= 1}
              title={sources.length <= 1 ? 'A task needs at least one source' : 'Remove source'}
              style={{
                background: 'none',
                border: 'none',
                cursor: sources.length <= 1 ? 'not-allowed' : 'pointer',
                color: sources.length <= 1 ? '#475569' : '#f87171',
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
            placeholder="@channel, t.me/xxx or -1001234567890"
            value={newSource}
            onChange={(e) => setNewSource(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') {
                e.preventDefault()
                addSource()
              }
            }}
          />
          <button
            type="button"
            onClick={addSource}
            disabled={!newSource.trim()}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: '4px',
              backgroundColor: newSource.trim() ? '#2563eb' : '#1e293b',
              color: newSource.trim() ? '#fff' : '#64748b',
              border: '1px solid #334155',
              borderRadius: '8px',
              padding: '8px 12px',
              fontSize: '12px',
              fontWeight: 600,
              cursor: newSource.trim() ? 'pointer' : 'not-allowed',
              whiteSpace: 'nowrap',
            }}
          >
            <Plus size={14} /> Add
          </button>
        </div>
        <div style={hint}>Save the task to apply source changes.</div>
      </div>

      {/* --------------------------------------------------------- settings */}
      <div style={card}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <div>
            <div style={{ fontSize: '13px', fontWeight: 700, color: '#f8fafc' }}>
              All task settings
            </div>
            <div style={hint}>
              {schema
                ? `Every setting this task supports (${schema.count}), validated by the server.`
                : 'Loading the settings schema…'}
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
              {groupLabel(group)}
            </div>
            {specs.map((spec) => (
              <SettingRow
                key={spec.key}
                spec={spec}
                value={spec.key in edits ? edits[spec.key] : settings[spec.key]}
                changed={spec.key in edits}
                isRtl={isRtl}
                labelStyle={labelStyle}
                inputStyle={inputStyle}
                hintStyle={hint}
                onChange={(value) => setEdits((prev) => ({ ...prev, [spec.key]: value }))}
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
              color: dirtyKeys.length && !saving ? '#fff' : '#64748b',
              border: 'none',
              borderRadius: '10px',
              padding: '9px 16px',
              fontSize: '12px',
              fontWeight: 600,
              cursor: dirtyKeys.length && !saving ? 'pointer' : 'not-allowed',
            }}
          >
            {saving ? <Loader2 size={14} className="spin" /> : <Save size={14} />}
            {saving ? 'Saving…' : `Save settings${dirtyKeys.length ? ` (${dirtyKeys.length})` : ''}`}
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
              Discard
            </button>
          )}
        </div>
      </div>
    </div>
  )
}

interface RowProps {
  spec: RuleSettingSpec
  value: any
  changed: boolean
  isRtl: boolean
  labelStyle: React.CSSProperties
  inputStyle: React.CSSProperties
  hintStyle: React.CSSProperties
  onChange: (value: any) => void
}

function SettingRow({ spec, value, changed, isRtl, inputStyle, hintStyle, onChange }: RowProps) {
  const rowStyle: React.CSSProperties = {
    display: 'flex',
    alignItems: 'center',
    gap: '10px',
    padding: '7px 0',
    borderBottom: '1px solid #1f2937',
  }
  const label = (
    <div style={{ flex: 1, minWidth: 0 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
        <span style={{ fontSize: '12px', color: '#e2e8f0', fontWeight: changed ? 700 : 500 }}>
          {spec.label}
        </span>
        {changed && (
          <span style={{ fontSize: '9px', color: '#fbbf24', fontWeight: 700 }}>EDITED</span>
        )}
      </div>
      {spec.help && <div style={hintStyle}>{spec.help}</div>}
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
            backgroundColor: value ? '#22c55e' : '#475569',
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
        <textarea
          style={{ ...inputStyle, minHeight: '54px', fontFamily: 'monospace', marginTop: '6px' }}
          value={typeof value === 'string' ? value : JSON.stringify(value ?? {}, null, 2)}
          onChange={(e) => {
            const raw = e.target.value
            try {
              onChange(JSON.parse(raw))
            } catch {
              // Keep the raw text so a half-typed object is not thrown away.
              onChange(raw)
            }
          }}
        />
      </div>
    )
  }

  if (spec.kind === 'list') {
    const text = Array.isArray(value) ? value.join(', ') : ''
    return (
      <div style={rowStyle}>
        {label}
        <input
          style={{ ...inputStyle, direction: 'ltr', maxWidth: '52%' }}
          value={text}
          placeholder="comma, separated"
          onChange={(e) =>
            onChange(
              e.target.value
                .split(',')
                .map((part) => part.trim())
                .filter(Boolean),
            )
          }
        />
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
