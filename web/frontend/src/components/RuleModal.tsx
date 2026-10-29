/**
 * The create/edit rule modal, extracted from the shell.
 *
 * It used to be ~530 lines of inline JSX inside `App.tsx`, fed by a draft that
 * lived in `useDashboard` — and worse, "New rule" seeded the draft with fake
 * demo data (a forex header, four keywords, two invented channel ids) the user
 * had to notice and overwrite. The draft now lives here: it is initialized from
 * the edited rule, or from a clean default with empty strings and arrays, so a
 * new task starts from nothing.
 *
 * Endpoints are the core of the form: chip lists (id + title) fed by the
 * ChatPicker or by manual entry, mirroring the TaskControlPanel editor — the
 * same field names, the same "scalar is the first list entry" rule
 * (`target_chat_id` stays authoritative). The loop warnings that used to be
 * duplicated four times across the shell are one computation over
 * `normalizeEndpoint` from `src/endpoint.ts`.
 *
 * The save payload is byte-compatible with the old `handleSaveRule`: the same
 * field names, the same metadata + `custom_metadata_json` dual-write, plus the
 * `source_chat_ids` / `target_chat_ids` mirrors the API already persists.
 */
import { useEffect, useMemo, useState, type FormEvent } from 'react'
import {
  Bot,
  Check,
  Mic,
  Music,
  PenLine,
  Plus,
  Shield,
  Smile,
  Sticker,
  Trash2,
  Type,
  Video,
  X,
} from 'lucide-react'
import { api } from '../api'
import { normalizeEndpoint } from '../endpoint'
import { ChatPicker } from './ChatPicker'
import { ModalShell } from './ui/Modal'
import {
  BRANDING_HOP_KEY,
  brandingHopSummary,
  brandingListToText,
  brandingTextToList,
  readBrandingHop,
  type BrandingHopConfig,
} from '../dashboard/branding'
import { isRtlLang, makeT, type UiLang } from '../locales/engine'
import type {
  AIConfig,
  ForwardRule,
  PromptTemplate,
  SaveRuleRequest,
  Session,
} from '../types'

/** Bot-parity toggles stored in the rule's metadata blob. */
const MEDIA_TOGGLES = [
  ['block_voice', Mic, 'ui_block_voice'],
  ['block_stickers', Sticker, 'ui_block_stickers'],
  ['block_video', Video, 'ui_block_video'],
  ['block_audio', Music, 'ui_block_audio'],
  ['block_text', Type, 'ui_block_text'],
  ['remove_emojis', Smile, 'ui_strip_emojis'],
  ['sync_deletes', Trash2, 'ui_sync_deletes'],
  ['ignore_edits', PenLine, 'ui_ignore_edits'],
] as const

/** A clean draft: nothing invented, nothing to notice and overwrite. */
const CLEAN_DRAFT: Partial<SaveRuleRequest> = {
  routing_type: 'CHANNEL_TO_CHANNEL',
  forward_mode: 'CUSTOM_HEADER_COPY',
  use_intermediate: false,
  is_active: true,
}

interface Props {
  open: boolean
  lang: UiLang
  /** The account's sessions — the ChatPicker browses one of their dialog lists. */
  sessions: Session[]
  aiConfigs: AIConfig[]
  promptTemplates: PromptTemplate[]
  /** The rule being edited, or null for a new one. */
  rule: ForwardRule | null
  onClose: () => void
  onSaved: (saved: ForwardRule) => void
  showToast: (message: string) => void
}

type EndpointSide = 'source' | 'target'
type BrandingDraftKey = 'origin_ids' | 'origin_titles' | 'keywords'

export function RuleModal({
  open,
  lang,
  sessions,
  aiConfigs,
  promptTemplates,
  rule,
  onClose,
  onSaved,
  showToast,
}: Props) {
  const t = makeT(lang)
  const isRtl = isRtlLang(lang)

  const [form, setForm] = useState<Partial<SaveRuleRequest>>(CLEAN_DRAFT)
  const [meta, setMeta] = useState<Record<string, any>>({})
  // The branding-hop list fields are typed as free text: a draft keeps exactly
  // what was typed, because deriving the input value from the parsed list would
  // eat the ", " separator mid-keystroke. Reset when the modal opens.
  const [brandingDrafts, setBrandingDrafts] = useState<Record<BrandingDraftKey, string>>({
    origin_ids: '',
    origin_titles: '',
    keywords: '',
  })
  const [saving, setSaving] = useState(false)

  // (Re-)initialize the whole draft every time the modal opens: editing loads
  // the rule as it came back from a GET; creating starts from the clean default.
  useEffect(() => {
    if (!open) return
    if (rule) {
      setForm({ ...rule } as Partial<SaveRuleRequest>)
      try {
        const parsed = JSON.parse((rule as any).custom_metadata_json || '{}')
        if ((rule as any).prompt_id && !parsed.prompt_id) {
          parsed.prompt_id = (rule as any).prompt_id
        }
        setMeta(typeof parsed === 'object' && parsed && !Array.isArray(parsed) ? parsed : {})
      } catch {
        setMeta((rule as any).prompt_id ? { prompt_id: (rule as any).prompt_id } : {})
      }
    } else {
      setForm({ ...CLEAN_DRAFT })
      setMeta({})
    }
    setBrandingDrafts({ origin_ids: '', origin_titles: '', keywords: '' })
  }, [open, rule])

  const brandingHop = useMemo(() => readBrandingHop(meta), [meta])
  const patchBrandingHop = (patch: Partial<BrandingHopConfig>) => {
    setMeta((prev) => {
      const raw = prev?.[BRANDING_HOP_KEY]
      const blob = raw && typeof raw === 'object' && !Array.isArray(raw) ? raw : {}
      return { ...prev, [BRANDING_HOP_KEY]: { ...blob, ...patch } }
    })
  }
  const brandingListValue = (key: BrandingDraftKey) =>
    brandingDrafts[key] !== undefined ? brandingDrafts[key] : brandingListToText(brandingHop[key])
  const onBrandingListInput = (key: BrandingDraftKey, raw: string) => {
    setBrandingDrafts((prev) => ({ ...prev, [key]: raw }))
    patchBrandingHop({ [key]: brandingTextToList(raw) } as Partial<BrandingHopConfig>)
  }

  // ---- endpoints: the lists are the truth, the scalars mirror the first chip
  const sourceIds = useMemo(
    () =>
      (Array.isArray(form.source_chat_ids) && form.source_chat_ids.length
        ? form.source_chat_ids.filter(Boolean)
        : form.source_chat_id
          ? [form.source_chat_id]
          : []),
    [form.source_chat_ids, form.source_chat_id],
  )
  const sourceNames = useMemo(
    () =>
      (Array.isArray(form.source_chat_names) && form.source_chat_names.length
        ? form.source_chat_names
        : form.source_chat_name
          ? [form.source_chat_name]
          : []),
    [form.source_chat_names, form.source_chat_name],
  )
  const targetIds = useMemo(
    () =>
      (Array.isArray(form.target_chat_ids) && form.target_chat_ids.length
        ? form.target_chat_ids.filter(Boolean)
        : form.target_chat_id
          ? [form.target_chat_id]
          : []),
    [form.target_chat_ids, form.target_chat_id],
  )
  const targetNames = useMemo(
    () =>
      (Array.isArray(form.target_chat_names) && form.target_chat_names.length
        ? form.target_chat_names
        : form.target_chat_name
          ? [form.target_chat_name]
          : []),
    [form.target_chat_names, form.target_chat_name],
  )

  const setEndpoints = (side: EndpointSide, ids: string[], names: string[]) => {
    const aligned = ids.map((_, i) => names[i] || '')
    setForm((prev) =>
      side === 'source'
        ? {
            ...prev,
            source_chat_ids: ids,
            source_chat_id: ids[0] || '',
            source_chat_names: aligned,
            source_chat_name: aligned[0] || '',
          }
        : {
            ...prev,
            target_chat_ids: ids,
            target_chat_id: ids[0] || '',
            target_chat_names: aligned,
            target_chat_name: aligned[0] || '',
          },
    )
  }

  // ---- loop detection: one shared normalizer, computed once for the form
  const sourceNorms = sourceIds.map(normalizeEndpoint).filter(Boolean)
  const targetNorms = targetIds.map(normalizeEndpoint).filter(Boolean)
  const intermediateNorm = normalizeEndpoint(form.intermediate_channel_id)
  const hasDirectLoop = sourceNorms.some((s) => s && targetNorms.includes(s))
  const hasHopLoop = Boolean(
    form.use_intermediate && intermediateNorm && (sourceNorms.includes(intermediateNorm) || targetNorms.includes(intermediateNorm)),
  )
  const hasLoop = hasDirectLoop || hasHopLoop
  const hasEndpoints = sourceIds.length > 0 && targetIds.length > 0

  const validateEndpoint = (side: EndpointSide, raw: string): string | null => {
    const n = normalizeEndpoint(raw)
    if (!n) return t('ui_chat_required')
    const same = side === 'source' ? sourceNorms : targetNorms
    if (same.some((s) => s === n)) {
      return side === 'source'
        ? t('ui_chat_already_source', { chat: raw.trim() })
        : t('ui_chat_already_target', { chat: raw.trim() })
    }
    const other = side === 'source' ? targetNorms : sourceNorms
    if (other.some((s) => s === n)) {
      return t('ui_chat_source_target_overlap', { chat: raw.trim() })
    }
    return null
  }

  const addEndpoint = (side: EndpointSide, value: string) => {
    const ids = side === 'source' ? sourceIds : targetIds
    const names = side === 'source' ? sourceNames : targetNames
    setEndpoints(side, [...ids, value.trim()], [...names, value.trim()])
  }

  const removeEndpoint = (side: EndpointSide, value: string) => {
    const ids = side === 'source' ? sourceIds : targetIds
    const names = side === 'source' ? sourceNames : targetNames
    const idx = ids.indexOf(value)
    if (idx === -1) return
    const next = ids.filter((id) => id !== value)
    setEndpoints(side, next, names.filter((_, i) => i !== idx))
  }

  /** Add several chats picked in the browser, deduped against both sides. */
  const addMany = (side: EndpointSide, picks: { id: string; title: string }[]) => {
    const ids = side === 'source' ? sourceIds : targetIds
    const names = side === 'source' ? sourceNames : targetNames
    const seen = new Set([...sourceNorms, ...targetNorms])
    const nextIds: string[] = []
    const nextNames: string[] = []
    for (const pick of picks) {
      const key = normalizeEndpoint(pick.id)
      if (!key || seen.has(key)) continue
      seen.add(key)
      nextIds.push(pick.id)
      nextNames.push((pick.title || '').trim() || pick.id)
    }
    if (nextIds.length === 0) return
    setEndpoints(side, [...ids, ...nextIds], [...names, ...nextNames])
  }

  const pickerSessionId = form.session_id || sessions[0]?.id || ''

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (saving) return
    if (!hasEndpoints) {
      showToast(t('ui_endpoint_hint_required'))
      return
    }
    const s = normalizeEndpoint(form.source_chat_id)
    const targetId = normalizeEndpoint(form.target_chat_id)
    if (s === targetId) {
      showToast(t('ui_routing_loop_detected_source_and_target_ids_cannot_be_identical'))
      return
    }
    if (form.use_intermediate && form.intermediate_channel_id) {
      const im = normalizeEndpoint(form.intermediate_channel_id)
      if (im === s || im === targetId) {
        showToast(t('ui_routing_loop_detected_intermediate_channel_cannot_match_source_o'))
        return
      }
    }
    setSaving(true)
    try {
      // Identical payload contract to the old handleSaveRule: the per-task
      // metadata rides the top-level `metadata` object AND the JSON string, so
      // a stale copy in `custom_metadata_json` can never revert an edit.
      const metaToSave: Record<string, any> = { ...meta }
      if (form.ai_config_id) {
        metaToSave.ai_config_id = form.ai_config_id
      }
      if (meta.prompt_id) {
        metaToSave.prompt_id = meta.prompt_id
      }
      const saved = await api.saveRule({
        ...form,
        prompt_id: meta.prompt_id || form.prompt_id,
        metadata: metaToSave,
        custom_metadata_json: JSON.stringify(metaToSave),
      } as SaveRuleRequest)
      onSaved(saved)
    } catch (e: any) {
      let msg = e?.message || t('ui_error')
      if (msg.includes('loop_detected') || msg.includes('Invalid rule endpoints')) {
        msg = t('ui_routing_loop_detected_source_and_target_endpoints_cannot_be_iden')
      }
      if (msg.includes('not_numeric') || msg.includes('resolve_failed')) {
        msg = t('ui_invalid_chat_id_use_a_numeric_id_or_a_username_the_connected_acc')
      }
      showToast(msg)
    } finally {
      setSaving(false)
    }
  }

  return (
    <ModalShell
      open={open}
      lang={lang}
      titleId="rule-modal-title"
      title={form.id ? t('ui_edit_forward_rule') : t('ui_new_forward_rule')}
      onClose={onClose}
      width={760}
    >
      <form onSubmit={submit}>
        {/* ---------------------------------------------------- endpoints */}
        <section className="rule-section">
          <div className="rule-section__title">{t('ui_endpoints')}</div>
          <div className="rule-modal-grid-2" style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14 }}>
            <EndpointPicker
              title={t('ui_sources')}
              chips={sourceIds.map((id, i) => ({ id, name: sourceNames[i] || '' }))}
              validate={(raw) => validateEndpoint('source', raw)}
              onAdd={(value) => addEndpoint('source', value)}
              onRemove={(value) => removeEndpoint('source', value)}
              onAddMany={(picks) => addMany('source', picks)}
              sessionId={pickerSessionId}
              existing={[...sourceIds, ...targetIds]}
              lang={lang}
            />
            <EndpointPicker
              title={t('ui_targets')}
              chips={targetIds.map((id, i) => ({ id, name: targetNames[i] || '' }))}
              validate={(raw) => validateEndpoint('target', raw)}
              onAdd={(value) => addEndpoint('target', value)}
              onRemove={(value) => removeEndpoint('target', value)}
              onAddMany={(picks) => addMany('target', picks)}
              sessionId={pickerSessionId}
              existing={[...sourceIds, ...targetIds]}
              lang={lang}
            />
          </div>
          {!hasEndpoints && (
            <p className="field__hint" style={{ marginTop: 8 }}>
              {t('ui_endpoint_hint_required')}
            </p>
          )}
          {hasDirectLoop && (
            <LoopWarning message={t('ui_routing_loop_detected_source_and_target_chat_ids_cannot_be_ident')} />
          )}
          {hasHopLoop && (
            <LoopWarning message={t('ui_routing_loop_detected_intermediate_hop_channel_cannot_match_sour')} />
          )}
        </section>

        {/* ------------------------------------------------- session scope */}
        {sessions.length > 0 && (
          <div className="field" style={{ marginTop: 12 }}>
            <label className="field__label" htmlFor="rule-session-select">
              {t('ui_session')}
            </label>
            <select
              id="rule-session-select"
              className="input-field"
              value={form.session_id || sessions[0]?.id || ''}
              onChange={(e) => setForm((prev) => ({ ...prev, session_id: e.target.value }))}
            >
              {sessions.map((s) => (
                <option key={s.id} value={s.id}>
                  {(s.first_name || s.username || s.phone_number || s.id) + (s.username ? ` (@${s.username})` : '')}
                </option>
              ))}
            </select>
          </div>
        )}

        {/* ---------------------------------------------- branding hop C */}
        <section
          className="rule-section"
          style={{ borderColor: 'rgba(245, 158, 11, 0.3)' }}
        >
          <label className="field__label" style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer' }}>
            <input
              type="checkbox"
              checked={form.use_intermediate ?? false}
              onChange={(e) => setForm((prev) => ({ ...prev, use_intermediate: e.target.checked }))}
            />
            <span style={{ fontWeight: 700, color: 'var(--color-warning)' }}>
              {t('ui_use_intermediate_branding_channel_hop_via_c')}
            </span>
          </label>

          {form.use_intermediate && (
            <>
              <div className="rule-modal-grid-2" style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginTop: 10 }}>
                <div className="field">
                  <label className="field__label" htmlFor="hop-channel-id">
                    {t('ui_intermediate_channel_id_c')}
                  </label>
                  <input
                    id="hop-channel-id"
                    type="text"
                    value={form.intermediate_channel_id || ''}
                    onChange={(e) => setForm((prev) => ({ ...prev, intermediate_channel_id: e.target.value }))}
                    className="input-field"
                    placeholder="-1002222222222"
                  />
                </div>
                <div className="field">
                  <label className="field__label" htmlFor="hop-channel-name">
                    {t('ui_intermediate_title')}
                  </label>
                  <input
                    id="hop-channel-name"
                    type="text"
                    value={form.intermediate_channel_name || ''}
                    onChange={(e) => setForm((prev) => ({ ...prev, intermediate_channel_name: e.target.value }))}
                    className="input-field"
                    placeholder={t('ui_my_brand_channel')}
                  />
                </div>
              </div>

              {/* Hop triggers — stored in the rule metadata under branding_hop */}
              <div style={{ marginTop: 14, paddingTop: 12, borderTop: '1px dashed rgba(245, 158, 11, 0.25)' }}>
                <div className="rule-section__title" style={{ color: 'var(--color-warning)' }}>
                  {t('ui_branding_hop_triggers_optional')}
                </div>

                <label className="field__label" style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer', marginBottom: 8 }}>
                  <input
                    type="checkbox"
                    checked={brandingHop.triggers_enabled}
                    onChange={(e) => patchBrandingHop({ triggers_enabled: e.target.checked })}
                  />
                  <span>{t('ui_only_hop_messages_matching_the_triggers_below_otherwise_every_ma')}</span>
                </label>

                {brandingHop.triggers_enabled && (
                  <>
                    <div className="rule-modal-grid-2" style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
                      <div className="field">
                        <label className="field__label" htmlFor="hop-origin-titles">
                          {t('ui_forward_origin_channel_names')}
                        </label>
                        <input
                          id="hop-origin-titles"
                          type="text"
                          value={brandingListValue('origin_titles')}
                          onChange={(e) => onBrandingListInput('origin_titles', e.target.value)}
                          className="input-field"
                          placeholder={t('ui_premium_deals_forex_vip')}
                        />
                        <p className="field__hint">
                          {t('ui_comma_separated_matches_the_channel_name_in_the_forward_header_n')}
                        </p>
                      </div>
                      <div className="field">
                        <label className="field__label" htmlFor="hop-origin-ids">
                          {t('ui_forward_origin_channel_ids')}
                        </label>
                        <input
                          id="hop-origin-ids"
                          type="text"
                          value={brandingListValue('origin_ids')}
                          onChange={(e) => onBrandingListInput('origin_ids', e.target.value)}
                          className="input-field"
                          placeholder="-1001111111111, @premium_deals"
                        />
                      </div>
                    </div>

                    <div className="field" style={{ marginTop: 12 }}>
                      <label className="field__label" htmlFor="hop-keywords">
                        {t('ui_message_text_keywords')}
                      </label>
                      <input
                        id="hop-keywords"
                        type="text"
                        value={brandingListValue('keywords')}
                        onChange={(e) => onBrandingListInput('keywords', e.target.value)}
                        className="input-field"
                        placeholder={t('ui_example_keywords')}
                      />
                    </div>

                    <div className="rule-modal-grid-2" style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginTop: 12 }}>
                      <div className="field">
                        <label className="field__label" htmlFor="hop-match-mode">
                          {t('ui_trigger_match_mode')}
                        </label>
                        <select
                          id="hop-match-mode"
                          value={brandingHop.match_mode}
                          onChange={(e) => patchBrandingHop({ match_mode: e.target.value === 'all' ? 'all' : 'any' })}
                          className="input-field"
                        >
                          <option value="any">{t('ui_any_one_matching_group_is_enough')}</option>
                          <option value="all">{t('ui_all_every_matching_group_must_match')}</option>
                        </select>
                      </div>
                      <label className="field__label" style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer', alignSelf: 'end', paddingBottom: 8 }}>
                        <input
                          type="checkbox"
                          checked={brandingHop.require_forward}
                          onChange={(e) => patchBrandingHop({ require_forward: e.target.checked })}
                        />
                        <span>{t('ui_refuse_messages_that_are_not_forwards')}</span>
                      </label>
                    </div>
                  </>
                )}

                <label className="field__label" style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer', marginTop: 12 }}>
                  <input
                    type="checkbox"
                    checked={brandingHop.force_forward}
                    onChange={(e) => patchBrandingHop({ force_forward: e.target.checked })}
                  />
                  <span>{t('ui_native_forward_from_c_the_only_way_b_shows_c_in_the_header')}</span>
                </label>

                <div
                  dir={isRtl ? 'rtl' : 'ltr'}
                  className="rule-hop-summary"
                >
                  {brandingHopSummary(
                    lang,
                    brandingHop,
                    `${form.source_chat_name || form.source_chat_id || 'A'} → ${form.intermediate_channel_name || form.intermediate_channel_id || 'C'} → ${form.target_chat_name || form.target_chat_id || 'B'}`,
                  )}
                </div>
              </div>
            </>
          )}
        </section>

        {/* ------------------------------------------- custom header/footer */}
        <div className="rule-modal-grid-2" style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14, marginTop: 14 }}>
          <div className="field">
            <label className="field__label" htmlFor="rule-custom-header">
              {t('ui_custom_header')}
            </label>
            <input
              id="rule-custom-header"
              type="text"
              value={form.custom_header || ''}
              onChange={(e) => setForm((prev) => ({ ...prev, custom_header: e.target.value }))}
              className="input-field"
              placeholder={t('ui_vip_forex')}
            />
          </div>
          <div className="field">
            <label className="field__label" htmlFor="rule-custom-footer">
              {t('ui_custom_footer')}
            </label>
            <input
              id="rule-custom-footer"
              type="text"
              value={form.custom_footer || ''}
              onChange={(e) => setForm((prev) => ({ ...prev, custom_footer: e.target.value }))}
              className="input-field"
              placeholder="@usdjp"
            />
          </div>
        </div>

        {/* --------------------------------- bot-parity media toggles (metadata) */}
        <section className="rule-section" style={{ borderColor: 'rgba(99, 102, 241, 0.25)', marginTop: 14 }}>
          <div className="rule-section__title" style={{ color: 'var(--color-primary)' }}>
            {t('ui_media_sync_settings_bot_parity')}
          </div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(170px, 1fr))', gap: 10 }}>
            {MEDIA_TOGGLES.map(([key, Icon, labelKey]) => (
              <label
                key={key}
                className="rule-toggle-chip"
                style={{
                  borderColor: meta[key] ? 'rgba(99, 102, 241, 0.5)' : 'var(--color-border)',
                }}
              >
                <input
                  type="checkbox"
                  checked={!!meta[key]}
                  onChange={(e) => setMeta((prev) => ({ ...prev, [key]: e.target.checked }))}
                />
                <Icon size={14} aria-hidden="true" style={{ flexShrink: 0, color: 'var(--color-text-subtle)' }} />
                <span>{t(labelKey)}</span>
              </label>
            ))}
          </div>
          <div className="field" style={{ marginTop: 12 }}>
            <label className="field__label" htmlFor="rule-album-mode">
              {t('ui_album_mode')}
            </label>
            <select
              id="rule-album-mode"
              value={meta.album_mode || 'album'}
              onChange={(e) => setMeta((prev) => ({ ...prev, album_mode: e.target.value }))}
              className="input-field"
            >
              <option value="album">{t('ui_full_album')}</option>
              <option value="first">{t('ui_first_media_only')}</option>
              <option value="split">{t('ui_split_messages')}</option>
            </select>
          </div>
        </section>

        {/* ------------------------------------------------ AI rewrite fields */}
        <section className="rule-section" style={{ marginTop: 14 }}>
          <div className="rule-section__title" style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <Bot size={15} aria-hidden="true" />
            <span>{t('ui_ai_message_rewrite')}</span>
          </div>
          <div className="rule-modal-grid-2" style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14 }}>
            <div className="field">
              <label className="field__label" htmlFor="rule-ai-config">
                {t('ui_ai_config')}
              </label>
              <select
                id="rule-ai-config"
                value={form.ai_config_id || ''}
                onChange={(e) => setForm((prev) => ({ ...prev, ai_config_id: e.target.value || undefined }))}
                className="input-field"
              >
                <option value="">{t('ui_disabled_no_rewrite')}</option>
                {(Array.isArray(aiConfigs) ? aiConfigs : []).map((a) => (
                  <option key={a.id} value={a.id}>
                    {`${a.name} [${a.provider}/${a.model}]${a.is_enabled ? '' : ' · OFF'}`}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label className="field__label" htmlFor="rule-ai-prompt">
                {t('ui_rewrite_prompt')}
              </label>
              <select
                id="rule-ai-prompt"
                value={meta.prompt_id || ''}
                onChange={(e) => setMeta((prev) => ({ ...prev, prompt_id: e.target.value || undefined }))}
                className="input-field"
              >
                <option value="">{t('ui_default_no_system_prompt')}</option>
                {(Array.isArray(promptTemplates) ? promptTemplates : []).map((p) => (
                  <option key={p.id} value={p.id}>
                    {`${p.name} (v${p.current_version})`}
                  </option>
                ))}
              </select>
            </div>
            <div className="field">
              <label className="field__label" htmlFor="rule-ai-fallback">
                {t('ui_on_ai_failure')}
              </label>
              <select
                id="rule-ai-fallback"
                value={form.ai_fallback_policy || 'DROP'}
                onChange={(e) => setForm((prev) => ({ ...prev, ai_fallback_policy: e.target.value }))}
                className="input-field"
              >
                <option value="SEND_ORIGINAL">{t('ui_send_original_fail_open')}</option>
                <option value="DROP">{t('ui_drop_fail_closed')}</option>
                <option value="RETRY">{t('ui_app_retry')}</option>
                <option value="QUARANTINE">{t('ui_quarantine')}</option>
              </select>
            </div>
            <div className="field">
              <label className="field__label" htmlFor="rule-ai-timeout">
                {t('ui_timeout_s')}
              </label>
              <input
                id="rule-ai-timeout"
                type="number"
                min={1}
                max={120}
                value={form.ai_timeout_seconds ?? 15}
                onChange={(e) => setForm((prev) => ({ ...prev, ai_timeout_seconds: Number(e.target.value) || 15 }))}
                className="input-field"
              />
            </div>
          </div>
          <p className="field__hint" style={{ marginTop: 8 }}>
            {t('ui_fail_open_sends_the_original_text_if_ai_fails_and_logs_the_fallb')}
          </p>
        </section>

        <div className="modal-sheet-actions" style={{ display: 'flex', justifyContent: 'flex-end', gap: 10, marginTop: 24 }}>
          <button type="button" onClick={onClose} className="btn btn-secondary">
            {t('ui_cancel')}
          </button>
          <button type="submit" disabled={saving || hasLoop || !hasEndpoints} className="btn btn-primary">
            <Check size={16} aria-hidden="true" />
            <span>{t('ui_save_rule')}</span>
          </button>
        </div>
      </form>
    </ModalShell>
  )
}

/** The red banner under the endpoint lists when a loop is configured live. */
function LoopWarning({ message }: { message: string }) {
  return (
    <div
      role="alert"
      className="rule-loop-warning"
    >
      <Shield size={16} aria-hidden="true" style={{ flexShrink: 0 }} />
      <span>{message}</span>
    </div>
  )
}

interface EndpointPickerProps {
  title: string
  chips: { id: string; name: string }[]
  validate: (raw: string) => string | null
  onAdd: (value: string) => void
  onRemove: (value: string) => void
  onAddMany: (picks: { id: string; title: string }[]) => void
  sessionId: string
  /** Every id already used by the other side, so the browser can dim them. */
  existing: string[]
  lang: UiLang
}

/**
 * One endpoint list (sources or targets): removable chips plus manual entry
 * plus the chat browser — the TaskControlPanel editor's UX, applied to the
 * create/edit flow so both flows behave the same way.
 */
function EndpointPicker({
  title,
  chips,
  validate,
  onAdd,
  onRemove,
  onAddMany,
  sessionId,
  existing,
  lang,
}: EndpointPickerProps) {
  const t = makeT(lang)
  const [draft, setDraft] = useState('')
  const [localError, setLocalError] = useState<string | null>(null)
  const [browsing, setBrowsing] = useState(false)

  const submitDraft = () => {
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
    <div className="endpoint-picker">
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBlockEnd: 6 }}>
        <span className="field__label">{title}</span>
        <span className="badge badge--neutral" style={{ fontSize: 'var(--type-xs)', padding: '1px 8px' }}>
          {chips.length}
        </span>
      </div>

      {chips.map((chip) => (
        <span key={chip.id} className="endpoint-chip">
          <span className="endpoint-chip__name">{chip.name || chip.id}</span>
          <span className="font-mono-ltr endpoint-chip__id">{chip.id}</span>
          <button
            type="button"
            className="btn btn--ghost btn--icon btn--sm endpoint-chip__remove"
            onClick={() => onRemove(chip.id)}
            aria-label={t('ui_remove_name', { name: chip.name || chip.id })}
            title={t('ui_remove_name', { name: chip.name || chip.id })}
          >
            <X size={14} aria-hidden="true" />
          </button>
        </span>
      ))}

      <div className="toolbar" style={{ marginTop: 8 }}>
        <input
          type="text"
          className="input-field"
          style={{ flex: 1, minWidth: 0 }}
          placeholder={t('ui_channel_t_me_xxx_or_1001234567890')}
          value={draft}
          aria-label={title}
          onChange={(e) => {
            setDraft(e.target.value)
            if (localError) setLocalError(null)
          }}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault()
              submitDraft()
            }
          }}
        />
        <button
          type="button"
          className="btn btn--primary btn--sm"
          onClick={submitDraft}
          disabled={!draft.trim()}
        >
          <Plus size={14} aria-hidden="true" />
          <span>{t('ui_add')}</span>
        </button>
      </div>
      {localError && (
        <p className="field__error" role="alert" style={{ marginTop: 6 }}>
          {localError}
        </p>
      )}

      {/* Below the id field on purpose: browse is a convenience over typing — a
          session with no live client still configures a task by id. */}
      <button
        type="button"
        className="btn btn--ghost btn--sm endpoint-picker__browse"
        onClick={() => setBrowsing(true)}
        disabled={!sessionId}
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
