/**
 * The full content-filter editor, extracted from the shell.
 *
 * The inline modal it replaces had two fields (name + blacklist) and nothing
 * else, while the FilterRule the API stores carries whitelist/blacklist
 * keywords, two regex lists, media-type gates, length bounds, the service
 * message switch and the enable flag. Every field is edited here, and a live
 * test posts the draft to `api.testFilterRule` (the Core's canonical matcher,
 * read-only by contract) so the verdict on screen is the verdict the pipeline
 * would produce — never a client-side reimplementation.
 *
 * The test is debounced (700ms after the last keystroke, one request at a
 * time) and skipped while the sample is empty, so typing cannot storm the API.
 */
import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { Check, FlaskConical } from 'lucide-react'
import { api } from '../api'
import { ModalShell } from './ui/Modal'
import { makeT, type UiLang } from '../locales/engine'
import type { FilterRule, FilterTestResult, SaveFilterRuleRequest } from '../types'

/** The media types the core's matcher knows, as chip toggles. */
const MEDIA_TYPES = ['TEXT', 'PHOTO', 'VIDEO', 'AUDIO', 'VOICE', 'ANIMATION', 'DOCUMENT', 'STICKER'] as const

type ListKey = 'whitelist_keywords' | 'blacklist_keywords' | 'regex_block' | 'regex_require'

/** The same separators the engine's split_word_list accepts. */
function parseList(raw: string): string[] {
  return raw
    .split(/[\n,،]/)
    .map((s) => s.trim())
    .filter(Boolean)
}

const CLEAN_DRAFT: Partial<SaveFilterRuleRequest> = {
  name: '',
  whitelist_keywords: [],
  blacklist_keywords: [],
  regex_block: [],
  regex_require: [],
  allowed_media_types: [],
  blocked_media_types: [],
  drop_service_messages: false,
  min_message_length: 0,
  max_message_length: 0,
  is_enabled: true,
}

interface Props {
  open: boolean
  lang: UiLang
  /** The filter being edited, or null for a new one. */
  filter: FilterRule | null
  onClose: () => void
  onSaved: (saved: FilterRule) => void
  /** The shell's toast; `tone: 'danger'` marks failures. */
  showToast: (message: string, tone?: 'success' | 'danger') => void
}

export function FilterModal({ open, lang, filter, onClose, onSaved, showToast }: Props) {
  const t = makeT(lang)

  const [form, setForm] = useState<Partial<SaveFilterRuleRequest>>(CLEAN_DRAFT)
  // The textareas hold what was typed; the parsed lists update live beneath.
  const [lists, setLists] = useState<Record<ListKey, string>>({
    whitelist_keywords: '',
    blacklist_keywords: '',
    regex_block: '',
    regex_require: '',
  })
  const [saving, setSaving] = useState(false)

  const [sample, setSample] = useState('')
  const [testResult, setTestResult] = useState<FilterTestResult | null>(null)
  const [testing, setTesting] = useState(false)
  const testSeq = useRef(0)

  // Re-initialize on every open: editing loads the stored filter, creating
  // starts from an empty draft — no seeded "SPAM" keywords to notice.
  useEffect(() => {
    if (!open) return
    if (filter) {
      setForm({ ...filter })
      setLists({
        whitelist_keywords: (filter.whitelist_keywords || []).join('\n'),
        blacklist_keywords: (filter.blacklist_keywords || []).join('\n'),
        regex_block: (filter.regex_block || []).join('\n'),
        regex_require: (filter.regex_require || []).join('\n'),
      })
    } else {
      setForm({ ...CLEAN_DRAFT })
      setLists({ whitelist_keywords: '', blacklist_keywords: '', regex_block: '', regex_require: '' })
    }
    setSample('')
    setTestResult(null)
  }, [open, filter])

  const parsedLists = useMemo(
    () => ({
      whitelist_keywords: parseList(lists.whitelist_keywords),
      blacklist_keywords: parseList(lists.blacklist_keywords),
      regex_block: parseList(lists.regex_block),
      regex_require: parseList(lists.regex_require),
    }),
    [lists],
  )

  const draft: Partial<SaveFilterRuleRequest> = useMemo(
    () => ({ ...form, ...parsedLists }),
    [form, parsedLists],
  )

  // Live verdict: debounced, latest answer wins, quiet while there is no sample.
  useEffect(() => {
    if (!open || !sample.trim()) {
      setTestResult(null)
      return
    }
    const timer = setTimeout(async () => {
      const seq = ++testSeq.current
      setTesting(true)
      try {
        const res = await api.testFilterRule(filter?.id || '', sample, {
          name: form.name || '',
          whitelist_keywords: parsedLists.whitelist_keywords,
          blacklist_keywords: parsedLists.blacklist_keywords,
          regex_patterns: [],
          regex_block: parsedLists.regex_block,
          regex_require: parsedLists.regex_require,
        })
        if (seq === testSeq.current) setTestResult(res)
      } catch {
        if (seq === testSeq.current) setTestResult(null)
      } finally {
        if (seq === testSeq.current) setTesting(false)
      }
    }, 700)
    return () => clearTimeout(timer)
  }, [open, sample, filter?.id, form.name, parsedLists])

  const toggleMedia = (key: 'allowed_media_types' | 'blocked_media_types', value: string) => {
    setForm((prev) => {
      const list = Array.isArray(prev[key]) ? prev[key]! : []
      const next = list.includes(value) ? list.filter((m) => m !== value) : [...list, value]
      return { ...prev, [key]: next }
    })
  }

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (saving) return
    if (!String(form.name || '').trim()) {
      showToast(t('ui_filter_name_is_required'))
      return
    }
    setSaving(true)
    try {
      const saved = await api.saveFilter({
        ...draft,
        name: String(form.name).trim(),
      } as SaveFilterRuleRequest)
      onSaved(saved)
    } catch (e: any) {
      showToast(e?.message || t('ui_error'), 'danger')
    } finally {
      setSaving(false)
    }
  }

  const listFields: { key: ListKey; label: string; hint?: string; mono?: boolean }[] = [
    { key: 'whitelist_keywords', label: t('ui_filter_whitelist_keywords') },
    { key: 'blacklist_keywords', label: t('ui_blacklist') },
    { key: 'regex_block', label: t('ui_regex_block_list'), mono: true },
    { key: 'regex_require', label: t('ui_regex_require_list'), mono: true },
  ]

  return (
    <ModalShell
      open={open}
      lang={lang}
      titleId="filter-modal-title"
      title={filter ? t('ui_content_filter_rule') : t('ui_new_filter_rule')}
      onClose={onClose}
      width={680}
    >
      <form onSubmit={submit}>
        <div className="rule-modal-grid-2" style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14 }}>
          <div className="field">
            <label className="field__label" htmlFor="filter-name">
              {t('ui_filter_name')}
            </label>
            <input
              id="filter-name"
              type="text"
              required
              value={form.name || ''}
              onChange={(e) => setForm((prev) => ({ ...prev, name: e.target.value }))}
              className="input-field"
              placeholder={t('ui_anti_spam_filter')}
            />
          </div>
          <div className="field">
            <label className="field__label" htmlFor="filter-enabled">
              {t('ui_filter_list_enabled')}
            </label>
            <label className="field__label" style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer', minHeight: 44 }}>
              <input
                id="filter-enabled"
                type="checkbox"
                checked={form.is_enabled !== false}
                onChange={(e) => setForm((prev) => ({ ...prev, is_enabled: e.target.checked }))}
              />
              <span>{form.is_enabled !== false ? t('ui_filter_list_enabled') : t('ui_filter_list_disabled')}</span>
            </label>
          </div>
        </div>

        {/* --------------------------------------------------- list fields */}
        {listFields.map(({ key, label, mono }) => (
          <div className="field" style={{ marginTop: 12 }} key={key}>
            <label className="field__label" htmlFor={`filter-${key}`}>
              {label}
            </label>
            <textarea
              id={`filter-${key}`}
              rows={2}
              className="input-field"
              style={mono ? { fontFamily: 'var(--font-mono)', resize: 'vertical' } : { resize: 'vertical' }}
              value={lists[key]}
              onChange={(e) => setLists((prev) => ({ ...prev, [key]: e.target.value }))}
              placeholder={t('ui_comma_or_newline_separated')}
            />
            <div className="toolbar" style={{ marginTop: 6 }}>
              {parsedLists[key].map((entry, idx) => (
                <span key={`${entry}-${idx}`} className="badge badge--neutral" style={{ fontSize: 'var(--type-xs)', fontFamily: mono ? 'var(--font-mono)' : undefined }}>
                  {entry}
                </span>
              ))}
              {parsedLists[key].length === 0 && <span className="field__hint">{t('ui_comma_or_newline_separated')}</span>}
            </div>
          </div>
        ))}

        {/* ------------------------------------------------- media gates */}
        <div className="rule-modal-grid-2" style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14, marginTop: 14 }}>
          {(['allowed_media_types', 'blocked_media_types'] as const).map((key) => (
            <fieldset key={key} className="filter-media-group">
              <legend className="field__label">{key === 'allowed_media_types' ? t('ui_allowed_media') : t('ui_blocked_media')}</legend>
              <div className="toolbar">
                {MEDIA_TYPES.map((media) => {
                  const active = (Array.isArray(form[key]) ? form[key]! : []).includes(media)
                  return (
                    <button
                      key={media}
                      type="button"
                      className={`filter-media-chip${active ? ' is-on' : ''}`}
                      aria-pressed={active}
                      onClick={() => toggleMedia(key, media)}
                    >
                      {media}
                    </button>
                  )
                })}
              </div>
            </fieldset>
          ))}
        </div>

        {/* ----------------------------------------- length bounds + service */}
        <div className="rule-modal-grid-2" style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 14, marginTop: 14 }}>
          <div className="field">
            <label className="field__label" htmlFor="filter-min-len">
              {t('ui_min_message_length')}
            </label>
            <input
              id="filter-min-len"
              type="number"
              min={0}
              className="input-field"
              value={form.min_message_length ?? 0}
              onChange={(e) => setForm((prev) => ({ ...prev, min_message_length: Math.max(0, Number(e.target.value) || 0) }))}
            />
          </div>
          <div className="field">
            <label className="field__label" htmlFor="filter-max-len">
              {t('ui_max_message_length')}
            </label>
            <input
              id="filter-max-len"
              type="number"
              min={0}
              className="input-field"
              value={form.max_message_length ?? 0}
              onChange={(e) => setForm((prev) => ({ ...prev, max_message_length: Math.max(0, Number(e.target.value) || 0) }))}
            />
          </div>
          <label className="field__label" style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer', alignSelf: 'end', paddingBottom: 8 }}>
            <input
              type="checkbox"
              checked={!!form.drop_service_messages}
              onChange={(e) => setForm((prev) => ({ ...prev, drop_service_messages: e.target.checked }))}
            />
            <span>{t('ui_drop_service_messages')}</span>
          </label>
        </div>

        {/* ------------------------------------------------ live test preview */}
        <section className="rule-section" style={{ marginTop: 16 }}>
          <div className="rule-section__title" style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <FlaskConical size={15} aria-hidden="true" />
            <span>{t('ui_filter_live_test')}</span>
          </div>
          <div className="field">
            <label className="field__label" htmlFor="filter-sample">
              {t('ui_filter_sample_placeholder')}
            </label>
            <textarea
              id="filter-sample"
              rows={2}
              className="input-field"
              style={{ resize: 'vertical' }}
              value={sample}
              onChange={(e) => setSample(e.target.value)}
            />
          </div>
          {(testing || testResult) && (
            <div
              className={`filter-verdict${testResult ? (testResult.allowed ? ' filter-verdict--allowed' : ' filter-verdict--dropped') : ''}`}
              role="status"
            >
              {testing && <span className="field__hint">{t('ui_checking')}</span>}
              {!testing && testResult && (
                <>
                  <span className="filter-verdict__badge">{testResult.allowed ? t('ui_filter_verdict_allowed') : t('ui_filter_verdict_dropped')}</span>
                  {testResult.reason && <span className="field__hint">{testResult.reason}</span>}
                  {testResult.matched && testResult.matched.length > 0 && (
                    <span className="field__hint">
                      {t('ui_filter_matched_entries', { list: testResult.matched.join(' · ') })}
                    </span>
                  )}
                  {testResult.missing && testResult.missing.length > 0 && (
                    <span className="field__hint">
                      {t('ui_filter_missing_entries', { list: testResult.missing.join(' · ') })}
                    </span>
                  )}
                </>
              )}
            </div>
          )}
        </section>

        <div className="modal-sheet-actions" style={{ display: 'flex', justifyContent: 'flex-end', gap: 10, marginTop: 24 }}>
          <button type="button" onClick={onClose} className="btn btn-secondary">
            {t('ui_cancel')}
          </button>
          <button type="submit" disabled={saving} className="btn btn-primary">
            <Check size={16} aria-hidden="true" />
            <span>{t('ui_save_filter')}</span>
          </button>
        </div>
      </form>
    </ModalShell>
  )
}
