/**
 * Text-replacement rules (`/replace`): the dashboard surface of the manager.
 *
 * What a user edits here is what the forwarder applies and what the bot's
 * `/replace` panel lists, because all three go through the same core service
 * over `/api/replace-rules*`. Nothing about *which* rule wins, what a regex
 * flag does or how a copy is shared between tasks is decided in this file — the
 * server owns those answers and this panel only renders them.
 *
 * The one rule that is local: **a rule is one row**. It is stored once per task
 * it runs on, so a naive list would show it once per task and an edit would
 * touch one copy. The API already groups the copies by id and returns
 * `task_ids`/`task_names`, so the panel edits the group.
 *
 * Every visible string resolves through `t()`: `scripts/audit_web_ui_copy.js`
 * fails the build on a literal in a copy position, which is what keeps the ten
 * locales honest. Layout mirrors for Persian through the shell's `dir`, so this
 * file uses logical properties (`marginInlineStart`) rather than `left`/`right`.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  ChevronLeft,
  ChevronRight,
  Copy,
  FlaskConical,
  Pencil,
  Plus,
  Power,
  RefreshCw,
  Search,
  Trash2,
  X,
} from 'lucide-react'
import type { DashboardCtx } from '../App'
import { api } from '../api'
import type {
  ReplaceRule,
  ReplaceRuleInput,
  ReplaceRulePage,
  ReplaceRuleTestResult,
  ReplaceTaskOption,
} from '../types'
import { Badge, Button, Card, EmptyState, Field, SkeletonRows } from '../components/ui/Primitives'

type StatusFilter = 'all' | 'enabled' | 'disabled'

const STATUSES: StatusFilter[] = ['all', 'enabled', 'disabled']

const PER_PAGE = 12

const gridStyle: React.CSSProperties = {
  display: 'grid',
  gridTemplateColumns: 'repeat(auto-fill, minmax(300px, 1fr))',
  gap: 14,
}

const toolbarStyle: React.CSSProperties = {
  display: 'flex',
  flexWrap: 'wrap',
  gap: 10,
  alignItems: 'center',
  marginBottom: 16,
}

const chipStyle = (active: boolean): React.CSSProperties => ({
  padding: '6px 12px',
  borderRadius: 999,
  border: `1px solid ${active ? 'var(--color-info)' : 'var(--color-border)'}`,
  background: active ? 'var(--color-info-soft)' : 'transparent',
  color: active ? 'var(--color-info)' : 'var(--color-text)',
  fontSize: '0.78rem',
  fontWeight: 600,
  cursor: 'pointer',
})

const patternStyle: React.CSSProperties = {
  fontFamily: "'JetBrains Mono', Consolas, monospace",
  fontSize: '0.76rem',
  color: 'var(--color-text)',
  background: 'var(--color-surface-inset)',
  border: '1px solid var(--color-border)',
  borderRadius: 8,
  padding: '2px 6px',
  wordBreak: 'break-all',
}

const monoStyle: React.CSSProperties = {
  fontFamily: "'JetBrains Mono', Consolas, monospace",
  fontSize: '0.78rem',
  whiteSpace: 'pre-wrap',
  wordBreak: 'break-word',
}

/** A rule's display name: the name the user gave it, else the pattern. */
function ruleTitle(rule: ReplaceRule): string {
  return (rule.name || rule.source || '').trim()
}

function draftFrom(rule: ReplaceRule): ReplaceRuleInput {
  return {
    name: rule.name,
    source: rule.source,
    to: rule.to,
    mode: rule.mode,
    flags: rule.flags,
    whole_word: rule.whole_word,
    enabled: rule.enabled,
    priority: rule.priority,
    task_ids: [...rule.task_ids],
  }
}

const emptyDraft = (): ReplaceRuleInput => ({
  name: '',
  source: '',
  to: '',
  mode: 'literal',
  flags: '',
  whole_word: false,
  enabled: true,
  priority: 100,
  task_ids: [],
})

export default function ReplacePanel({ d }: { d: DashboardCtx }) {
  const { t, isRtl, showToast, showError } = d

  const [page, setPage] = useState<ReplaceRulePage | null>(null)
  const [query, setQuery] = useState('')
  const [pendingQuery, setPendingQuery] = useState('')
  const [status, setStatus] = useState<StatusFilter>('all')
  const [taskFilter, setTaskFilter] = useState('')
  const [pageNo, setPageNo] = useState(1)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  // editor / dialogs
  const [editor, setEditor] = useState<{ id: string; draft: ReplaceRuleInput } | null>(null)
  const [formError, setFormError] = useState('')
  const [confirming, setConfirming] = useState<ReplaceRule | null>(null)
  const [testing, setTesting] = useState<ReplaceRule | null>(null)
  const [sample, setSample] = useState('')
  const [result, setResult] = useState<ReplaceRuleTestResult | null>(null)
  const [testBusy, setTestBusy] = useState(false)

  // Search is submitted, not typed-through: every keystroke would be a round
  // trip, and a slow one would land out of order and repaint the list with an
  // older query's rows.
  const searchRef = useRef<HTMLInputElement | null>(null)

  const load = useCallback(
    async (opts: { page?: number; q?: string; status?: StatusFilter; taskId?: string } = {}) => {
      const nextPage = opts.page ?? pageNo
      const nextQuery = opts.q ?? query
      const nextStatus = opts.status ?? status
      const nextTask = opts.taskId ?? taskFilter
      setLoading(true)
      setError('')
      try {
        const data = await api.listReplaceRules({
          q: nextQuery,
          status: nextStatus,
          taskId: nextTask,
          page: nextPage,
          perPage: PER_PAGE,
        })
        setPage(data)
        // The server clamps the page (a delete can empty page 3), so the pager
        // follows the answer rather than the request.
        setPageNo(Number(data.page) || 1)
      } catch (e: any) {
        setError(e?.message || t('ui_rp_load_failed'))
        setPage(null)
      } finally {
        setLoading(false)
      }
    },
    [pageNo, query, status, taskFilter, t],
  )

  useEffect(() => {
    void load({ page: 1 })
    // Deliberately first-mount only: every later refresh goes through an
    // explicit call so a filter change cannot race the reload.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const rules = Array.isArray(page?.rules) ? page!.rules : []
  const counts = page?.counts ?? { total: 0, enabled: 0, disabled: 0 }
  const tasks: ReplaceTaskOption[] = Array.isArray(page?.tasks) ? page!.tasks : []
  const pages = Math.max(1, Number(page?.pages) || 1)
  const filtered = Boolean(query.trim()) || status !== 'all' || Boolean(taskFilter)

  const statusLabel = useMemo(
    () => ({
      all: t('ui_rp_filter_all'),
      enabled: t('ui_rp_filter_enabled'),
      disabled: t('ui_rp_filter_disabled'),
    }),
    [t],
  )

  /* ------------------------------------------------------------ writes --- */

  const applySaved = async (rule: ReplaceRule, message: string) => {
    showToast(message)
    setEditor(null)
    setFormError('')
    await load()
    // Counts and the row itself come from the server: a local splice would
    // drift the moment the same rule was changed from the bot.
    void rule
  }

  const submitEditor = async () => {
    if (!editor) return
    const draft = editor.draft
    if (!String(draft.source ?? '').trim()) {
      setFormError(t('ui_rp_required_find'))
      return
    }
    if (!Array.isArray(draft.task_ids) || draft.task_ids.length === 0) {
      setFormError(t('ui_rp_required_tasks'))
      return
    }
    setBusy(true)
    setFormError('')
    try {
      const saved = editor.id
        ? await api.updateReplaceRule(editor.id, draft)
        : await api.createReplaceRule(draft)
      await applySaved(saved.rule, editor.id ? t('ui_rp_saved') : t('ui_rp_created'))
    } catch (e: any) {
      setFormError(e?.message || t('ui_rp_save_failed'))
    } finally {
      setBusy(false)
    }
  }

  const toggle = async (rule: ReplaceRule) => {
    setBusy(true)
    try {
      await api.setReplaceRuleEnabled(rule.id, !rule.enabled)
      showToast(rule.enabled ? t('ui_rp_disabled_toast') : t('ui_rp_enabled_toast'))
      await load()
    } catch (e: any) {
      showError(e?.message || t('ui_rp_toggle_failed'))
    } finally {
      setBusy(false)
    }
  }

  const duplicate = async (rule: ReplaceRule) => {
    setBusy(true)
    try {
      await api.duplicateReplaceRule(rule.id)
      showToast(t('ui_rp_duplicated'))
      await load()
    } catch (e: any) {
      showError(e?.message || t('ui_rp_dup_failed'))
    } finally {
      setBusy(false)
    }
  }

  const remove = async (rule: ReplaceRule) => {
    setBusy(true)
    try {
      await api.deleteReplaceRule(rule.id)
      setConfirming(null)
      showToast(t('ui_rp_deleted'))
      // A delete on the last row of the last page leaves that page empty.
      const lastPage = rules.length === 1 && pageNo > 1
      await load({ page: lastPage ? pageNo - 1 : pageNo })
    } catch (e: any) {
      showError(e?.message || t('ui_rp_delete_failed'))
    } finally {
      setBusy(false)
    }
  }

  const runTest = async () => {
    if (!testing) return
    if (!sample.trim()) {
      setFormError(t('ui_rp_test_need_sample'))
      return
    }
    setTestBusy(true)
    setFormError('')
    try {
      const res = await api.testReplaceRule(testing.id, sample)
      setResult(res)
    } catch (e: any) {
      setFormError(e?.message || t('ui_rp_test_failed'))
    } finally {
      setTestBusy(false)
    }
  }

  /* ------------------------------------------------------------- render --- */

  const openCreate = () => {
    const draft = emptyDraft()
    // Pre-tick every task when there is exactly one: the common case is a
    // single-route tenant, and an empty picker would just be a step.
    draft.task_ids = tasks.length === 1 ? [tasks[0].id] : []
    setEditor({ id: '', draft })
    setFormError('')
  }

  const openEdit = (rule: ReplaceRule) => {
    setEditor({ id: rule.id, draft: draftFrom(rule) })
    setFormError('')
  }

  const openTest = (rule: ReplaceRule) => {
    setTesting(rule)
    setSample('')
    setResult(null)
    setFormError('')
  }

  const patchDraft = (patch: Partial<ReplaceRuleInput>) => {
    setEditor((prev) => (prev ? { ...prev, draft: { ...prev.draft, ...patch } } : prev))
  }

  const toggleTask = (taskId: string) => {
    const current = editor?.draft.task_ids ?? []
    const next = current.includes(taskId)
      ? current.filter((id) => id !== taskId)
      : [...current, taskId]
    patchDraft({ task_ids: next })
  }

  return (
    <div>
      <div
        style={{
          display: 'flex',
          flexWrap: 'wrap',
          gap: 12,
          alignItems: 'flex-start',
          justifyContent: 'space-between',
          marginBottom: 14,
        }}
      >
        <div style={{ minWidth: 220 }}>
          <h2 style={{ fontSize: '1.2rem', fontWeight: 800 }}>{t('ui_rp_title')}</h2>
          <p style={{ fontSize: '0.82rem', color: 'var(--color-text-muted)', marginTop: 4, maxWidth: 620 }}>
            {t('ui_rp_subtitle')}
          </p>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          <Button variant="ghost" onClick={() => void load()} disabled={loading || busy} title={t('ui_refresh')}>
            <RefreshCw size={15} aria-hidden="true" />
          </Button>
          <Button variant="primary" onClick={openCreate}>
            <Plus size={16} aria-hidden="true" />
            <span style={{ marginInlineStart: 6 }}>{t('ui_rp_new')}</span>
          </Button>
        </div>
      </div>

      {/* Counts are the server's, over the filtered set: a chip beside a task
          filter must not count a rule that task does not run. */}
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginBottom: 14 }}>
        <Badge tone="neutral">
          {t('ui_rp_counts', { total: counts.total, on: counts.enabled, off: counts.disabled })}
        </Badge>
      </div>

      <div style={toolbarStyle}>
        <div style={{ position: 'relative', flex: '1 1 240px', minWidth: 200 }}>
          <Search
            size={15}
            aria-hidden="true"
            style={{
              position: 'absolute',
              insetInlineStart: 10,
              top: '50%',
              transform: 'translateY(-50%)',
              color: 'var(--color-text-muted)',
            }}
          />
          <input
            ref={searchRef}
            type="search"
            className="input-field"
            value={pendingQuery}
            placeholder={t('ui_rp_search_placeholder')}
            aria-label={t('ui_rp_search_placeholder')}
            onChange={(e) => setPendingQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key !== 'Enter') return
              setQuery(pendingQuery)
              void load({ page: 1, q: pendingQuery })
            }}
            style={{ paddingInlineStart: 32, width: '100%' }}
          />
        </div>

        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
          {STATUSES.map((value) => (
            <button
              key={value}
              type="button"
              onClick={() => {
                setStatus(value)
                void load({ page: 1, status: value })
              }}
              style={chipStyle(status === value)}
              aria-pressed={status === value}
            >
              {statusLabel[value]}
            </button>
          ))}
        </div>

        {tasks.length > 1 && (
          <select
            className="input-field"
            value={taskFilter}
            aria-label={t('ui_rp_task_filter')}
            onChange={(e) => {
              setTaskFilter(e.target.value)
              void load({ page: 1, taskId: e.target.value })
            }}
            style={{ maxWidth: 240 }}
          >
            <option value="">{t('ui_rp_task_filter_all')}</option>
            {tasks.map((task) => (
              <option key={task.id} value={task.id}>
                {task.name || task.id}
              </option>
            ))}
          </select>
        )}
      </div>

      {error ? (
        <EmptyState
          title={t('ui_rp_load_failed')}
          description={error}
          action={
            <Button variant="primary" onClick={() => void load()}>
              {t('ui_refresh')}
            </Button>
          }
        />
      ) : loading && !page ? (
        <SkeletonRows rows={4} lang={d.lang} />
      ) : rules.length === 0 ? (
        <EmptyState
          title={filtered ? t('ui_rp_empty_filtered') : t('ui_rp_empty_title')}
          description={filtered ? undefined : t('ui_rp_empty_body')}
          action={
            filtered ? (
              <Button
                variant="ghost"
                onClick={() => {
                  setQuery('')
                  setPendingQuery('')
                  setStatus('all')
                  setTaskFilter('')
                  void load({ page: 1, q: '', status: 'all', taskId: '' })
                }}
              >
                {t('ui_rp_filter_all')}
              </Button>
            ) : (
              <Button variant="primary" onClick={openCreate}>
                {t('ui_rp_new')}
              </Button>
            )
          }
        />
      ) : (
        <div style={gridStyle} className="grid-collapse-1">
          {rules.map((rule) => (
            <Card key={rule.id}>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8 }}>
                  <span aria-hidden="true" style={{ fontSize: 'var(--type-md)', marginTop: 1 }}>
                    {rule.enabled ? '🟢' : '🔴'}
                  </span>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontWeight: 700, fontSize: '0.92rem', wordBreak: 'break-word' }}>
                      {ruleTitle(rule)}
                    </div>
                    <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginTop: 6 }}>
                      <Badge tone={rule.enabled ? 'success' : 'neutral'}>
                        {rule.enabled ? t('ui_rp_status_on') : t('ui_rp_status_off')}
                      </Badge>
                      <Badge tone="info">
                        {rule.mode === 'regex' ? t('ui_rp_mode_regex') : t('ui_rp_mode_literal')}
                      </Badge>
                      <Badge tone="neutral">
                        {t('ui_rp_priority_short', { priority: rule.priority })}
                      </Badge>
                    </div>
                  </div>
                </div>

                {/* source → replacement, the pair the rule actually applies */}
                <div dir="ltr" style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                  <code style={patternStyle}>{rule.badge || rule.source}</code>
                  <code style={{ ...patternStyle, color: rule.to ? 'var(--color-success)' : 'var(--color-danger-text)' }}>
                    {rule.to || t('ui_rp_removed_match')}
                  </code>
                </div>

                <div style={{ fontSize: 'var(--type-xs)', color: 'var(--color-text-muted)' }}>
                  {rule.task_names.length
                    ? t('ui_rp_runs_on', { n: rule.task_ids.length, names: rule.task_names.join(', ') })
                    : t('ui_rp_no_tasks')}
                </div>

                <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginTop: 2 }}>
                  <Button
                    variant={rule.enabled ? 'ghost' : 'primary'}
                    size="sm"
                    onClick={() => void toggle(rule)}
                    disabled={busy}
                    title={t(rule.enabled ? 'ui_rp_disable' : 'ui_rp_enable')}
                  >
                    <Power size={14} aria-hidden="true" />
                    <span style={{ marginInlineStart: 4 }}>
                      {t(rule.enabled ? 'ui_rp_disable' : 'ui_rp_enable')}
                    </span>
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => openEdit(rule)}
                    title={t('ui_rp_edit')}
                  >
                    <Pencil size={14} aria-hidden="true" />
                    <span style={{ marginInlineStart: 4 }}>{t('ui_rp_edit')}</span>
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => openTest(rule)}
                    title={t('ui_rp_test')}
                  >
                    <FlaskConical size={14} aria-hidden="true" />
                    <span style={{ marginInlineStart: 4 }}>{t('ui_rp_test')}</span>
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => void duplicate(rule)}
                    disabled={busy}
                    title={t('ui_rp_duplicate')}
                  >
                    <Copy size={14} aria-hidden="true" />
                  </Button>
                  <Button
                    variant="danger"
                    size="sm"
                    onClick={() => setConfirming(rule)}
                    disabled={busy}
                    title={t('ui_rp_delete')}
                  >
                    <Trash2 size={14} aria-hidden="true" />
                  </Button>
                </div>
              </div>
            </Card>
          ))}
        </div>
      )}

      {pages > 1 && (
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            gap: 10,
            marginTop: 18,
          }}
        >
          <Button
            variant="ghost"
            size="sm"
            disabled={pageNo <= 1 || loading}
            onClick={() => void load({ page: pageNo - 1 })}
            title={t('ui_rp_prev')}
          >
            {isRtl ? <ChevronRight size={15} aria-hidden="true" /> : <ChevronLeft size={15} aria-hidden="true" />}
          </Button>
          <span style={{ fontSize: 'var(--type-sm)', color: 'var(--color-text)' }}>
            {t('ui_rp_page_of', { page: pageNo, pages })}
          </span>
          <Button
            variant="ghost"
            size="sm"
            disabled={pageNo >= pages || loading}
            onClick={() => void load({ page: pageNo + 1 })}
            title={t('ui_rp_next')}
          >
            {isRtl ? <ChevronLeft size={15} aria-hidden="true" /> : <ChevronRight size={15} aria-hidden="true" />}
          </Button>
        </div>
      )}

      {/* ---------------------------------------------------- editor sheet --- */}
      {editor && (
        <div
          className="modal-overlay"
          style={{
            position: 'fixed',
            inset: 0,
            background: 'var(--color-scrim)',
            backdropFilter: 'blur(8px)',
            zIndex: 999,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            padding: 20,
          }}
          role="dialog"
          aria-modal="true"
          aria-label={editor.id ? t('ui_rp_edit_title') : t('ui_rp_create_title')}
        >
          <div
            className="glass-panel modal-sheet"
            style={{ width: '100%', maxWidth: 620, maxHeight: '92vh', overflowY: 'auto', padding: 24 }}
          >
            <div
              style={{
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'center',
                marginBottom: 16,
              }}
            >
              <h3 style={{ fontSize: '1.05rem', fontWeight: 800 }}>
                {editor.id ? t('ui_rp_edit_title') : t('ui_rp_create_title')}
              </h3>
              <button
                type="button"
                className="btn btn--ghost btn--icon"
                onClick={() => setEditor(null)}
                aria-label={t('ui_rp_close')}
                title={t('ui_rp_close')}
              >
                <X size={16} aria-hidden="true" />
              </button>
            </div>

            <div className="modal-sheet-body" style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
              <Field label={t('ui_rp_name_label')} hint={t('ui_rp_name_hint')} htmlFor="rp-name">
                <input
                  id="rp-name"
                  type="text"
                  className="input-field"
                  value={editor.draft.name ?? ''}
                  placeholder={t('ui_rp_name_placeholder')}
                  onChange={(e) => patchDraft({ name: e.target.value })}
                />
              </Field>

              <Field label={t('ui_rp_mode_label')} hint={t('ui_rp_mode_hint')}>
                <div style={{ display: 'flex', gap: 8 }}>
                  {(['literal', 'regex'] as const).map((mode) => (
                    <button
                      key={mode}
                      type="button"
                      onClick={() => patchDraft({ mode })}
                      style={chipStyle((editor.draft.mode ?? 'literal') === mode)}
                      aria-pressed={(editor.draft.mode ?? 'literal') === mode}
                    >
                      {t(mode === 'regex' ? 'ui_rp_mode_regex' : 'ui_rp_mode_literal')}
                    </button>
                  ))}
                </div>
              </Field>

              <Field label={t('ui_rp_find_label')} htmlFor="rp-find">
                <input
                  id="rp-find"
                  type="text"
                  dir="ltr"
                  className="input-field"
                  value={editor.draft.source ?? ''}
                  placeholder={t('ui_rp_find_placeholder')}
                  onChange={(e) => patchDraft({ source: e.target.value })}
                />
              </Field>

              <Field label={t('ui_rp_replace_label')} hint={t('ui_rp_replace_hint')} htmlFor="rp-to">
                <input
                  id="rp-to"
                  type="text"
                  dir="ltr"
                  className="input-field"
                  value={editor.draft.to ?? ''}
                  placeholder={t('ui_rp_replace_placeholder')}
                  onChange={(e) => patchDraft({ to: e.target.value })}
                />
              </Field>

              {(editor.draft.mode ?? 'literal') === 'regex' && (
                <Field label={t('ui_rp_flags_label')} hint={t('ui_rp_flags_hint')} htmlFor="rp-flags">
                  <input
                    id="rp-flags"
                    type="text"
                    dir="ltr"
                    className="input-field"
                    value={editor.draft.flags ?? ''}
                    onChange={(e) => patchDraft({ flags: e.target.value })}
                  />
                </Field>
              )}

              <Field label={t('ui_rp_whole_word')} hint={t('ui_rp_whole_word_hint')} htmlFor="rp-whole">
                <label style={{ display: 'flex', gap: 8, alignItems: 'center', fontSize: '0.82rem' }}>
                  <input
                    id="rp-whole"
                    type="checkbox"
                    checked={Boolean(editor.draft.whole_word)}
                    onChange={(e) => patchDraft({ whole_word: e.target.checked })}
                  />
                  <span>{t('ui_rp_whole_word')}</span>
                </label>
              </Field>

              <Field label={t('ui_rp_priority_label')} hint={t('ui_rp_priority_hint')} htmlFor="rp-priority">
                <input
                  id="rp-priority"
                  type="number"
                  min={0}
                  max={1000}
                  className="input-field"
                  value={editor.draft.priority ?? 100}
                  onChange={(e) => patchDraft({ priority: Number(e.target.value) })}
                />
              </Field>

              <Field label={t('ui_rp_tasks_label')} hint={t('ui_rp_tasks_hint')}>
                {tasks.length === 0 ? (
                  <p style={{ fontSize: 'var(--type-sm)', color: 'var(--color-danger-text)' }}>{t('ui_rp_tasks_none')}</p>
                ) : (
                  <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                    {tasks.map((task) => {
                      const checked = (editor.draft.task_ids ?? []).includes(task.id)
                      return (
                        <label
                          key={task.id}
                          style={{
                            display: 'flex',
                            gap: 8,
                            alignItems: 'center',
                            fontSize: '0.82rem',
                            padding: '6px 8px',
                            borderRadius: 8,
                            background: checked ? 'var(--color-success-soft)' : 'transparent',
                          }}
                        >
                          <input
                            type="checkbox"
                            checked={checked}
                            onChange={() => toggleTask(task.id)}
                          />
                          <span>{task.name || task.id}</span>
                        </label>
                      )
                    })}
                  </div>
                )}
              </Field>

              <Field label={t('ui_status')} htmlFor="rp-enabled">
                <label style={{ display: 'flex', gap: 8, alignItems: 'center', fontSize: '0.82rem' }}>
                  <input
                    id="rp-enabled"
                    type="checkbox"
                    checked={editor.draft.enabled !== false}
                    onChange={(e) => patchDraft({ enabled: e.target.checked })}
                  />
                  <span>{editor.draft.enabled !== false ? t('ui_rp_status_on') : t('ui_rp_status_off')}</span>
                </label>
              </Field>

              {formError && (
                <p role="alert" style={{ fontSize: 'var(--type-sm)', color: 'var(--color-danger-text)' }}>
                  {formError}
                </p>
              )}
            </div>

            <div
              className="modal-sheet-actions"
              style={{ display: 'flex', justifyContent: 'flex-end', gap: 10, marginTop: 18 }}
            >
              <Button variant="ghost" onClick={() => setEditor(null)} disabled={busy}>
                {t('ui_cancel')}
              </Button>
              <Button variant="primary" onClick={() => void submitEditor()} loading={busy}>
                {editor.id ? t('ui_rp_save') : t('ui_rp_create')}
              </Button>
            </div>
          </div>
        </div>
      )}

      {/* ------------------------------------------------- delete confirm --- */}
      {confirming && (
        <div
          className="modal-overlay"
          style={{
            position: 'fixed',
            inset: 0,
            background: 'var(--color-scrim)',
            backdropFilter: 'blur(8px)',
            zIndex: 999,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            padding: 20,
          }}
          role="dialog"
          aria-modal="true"
          aria-label={t('ui_rp_delete_title')}
        >
          <div className="glass-panel modal-sheet" style={{ width: '100%', maxWidth: 460, padding: 24 }}>
            <h3 style={{ fontSize: 'var(--type-md)', fontWeight: 800, marginBottom: 8 }}>{t('ui_rp_delete_title')}</h3>
            <p style={{ fontSize: '0.84rem', color: 'var(--color-text)', lineHeight: 1.5 }}>
              {t('ui_rp_delete_body', {
                name: ruleTitle(confirming),
                n: confirming.task_ids.length,
              })}
            </p>
            <div
              className="modal-sheet-actions"
              style={{ display: 'flex', justifyContent: 'flex-end', gap: 10, marginTop: 18 }}
            >
              <Button variant="ghost" onClick={() => setConfirming(null)} disabled={busy}>
                {t('ui_cancel')}
              </Button>
              <Button variant="danger" onClick={() => void remove(confirming)} loading={busy}>
                {t('ui_rp_delete_confirm')}
              </Button>
            </div>
          </div>
        </div>
      )}

      {/* ---------------------------------------------------- test preview --- */}
      {testing && (
        <div
          className="modal-overlay"
          style={{
            position: 'fixed',
            inset: 0,
            background: 'var(--color-scrim)',
            backdropFilter: 'blur(8px)',
            zIndex: 999,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            padding: 20,
          }}
          role="dialog"
          aria-modal="true"
          aria-label={t('ui_rp_test_title')}
        >
          <div
            className="glass-panel modal-sheet"
            style={{ width: '100%', maxWidth: 620, maxHeight: '92vh', overflowY: 'auto', padding: 24 }}
          >
            <div
              style={{
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'center',
                marginBottom: 12,
              }}
            >
              <h3 style={{ fontSize: '1.05rem', fontWeight: 800 }}>{t('ui_rp_test_title')}</h3>
              <button
                type="button"
                className="btn btn--ghost btn--icon"
                onClick={() => setTesting(null)}
                aria-label={t('ui_rp_close')}
                title={t('ui_rp_close')}
              >
                <X size={16} aria-hidden="true" />
              </button>
            </div>

            <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }} className="modal-sheet-body">
              <div dir="ltr" style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                <code style={patternStyle}>{testing.badge || testing.source}</code>
                <code style={{ ...patternStyle, color: testing.to ? 'var(--color-success)' : 'var(--color-danger-text)' }}>
                  {testing.to || t('ui_rp_removed_match')}
                </code>
              </div>

              <Field label={t('ui_rp_test_sample_label')} htmlFor="rp-sample">
                <textarea
                  id="rp-sample"
                  className="input-field"
                  rows={4}
                  value={sample}
                  placeholder={t('ui_rp_test_sample_placeholder')}
                  onChange={(e) => setSample(e.target.value)}
                />
              </Field>

              {result && (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                  <Badge tone={result.changed ? 'success' : 'warning'}>
                    {result.changed
                      ? t('ui_rp_test_matches', { n: result.matches })
                      : t('ui_rp_test_unchanged')}
                  </Badge>
                  <div>
                    <div style={{ fontSize: 'var(--type-xs)', color: 'var(--color-text-muted)', marginBottom: 4 }}>
                      {t('ui_rp_test_input')}
                    </div>
                    <pre
                      dir="auto"
                      style={{
                        ...monoStyle,
                        background: 'var(--color-surface-inset)',
                        borderRadius: 8,
                        padding: 10,
                        margin: 0,
                      }}
                    >
                      {result.input}
                    </pre>
                  </div>
                  <div>
                    <div style={{ fontSize: 'var(--type-xs)', color: 'var(--color-text-muted)', marginBottom: 4 }}>
                      {t('ui_rp_test_output')}
                    </div>
                    <pre
                      dir="auto"
                      style={{
                        ...monoStyle,
                        background: 'var(--color-success-soft)',
                        border: '1px solid color-mix(in oklab, var(--color-success) 40%, transparent)',
                        borderRadius: 8,
                        padding: 10,
                        margin: 0,
                        color: 'var(--color-success)',
                      }}
                    >
                      {result.output}
                    </pre>
                  </div>
                </div>
              )}

              {formError && (
                <p role="alert" style={{ fontSize: 'var(--type-sm)', color: 'var(--color-danger-text)' }}>
                  {formError}
                </p>
              )}
            </div>

            <div
              className="modal-sheet-actions"
              style={{ display: 'flex', justifyContent: 'flex-end', gap: 10, marginTop: 16 }}
            >
              <Button variant="ghost" onClick={() => setTesting(null)}>
                {t('ui_rp_close')}
              </Button>
              <Button variant="primary" onClick={() => void runTest()} loading={testBusy}>
                {t('ui_rp_test_run')}
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
