import React, { useState, useEffect, useCallback } from 'react'
import {
  X,
  Play,
  Pause,
  Bug,
  SlidersHorizontal,
  ArrowRight,
  Shield,
  Clock,
  Wand2,
  Lock,
  ChevronRight,
  Sparkles,
  Info,
  Trash2,
  Edit2,
  Hash,
  ScrollText,
  LayoutDashboard,
  Settings2,
  Ban,
  Check,
  Circle,
  Eraser,
  Filter,
  ListChecks,
  Replace,
  Star,
} from 'lucide-react'
import type { ForwardRule } from '../types'
import { makeT, type UiLang } from '../locales/engine'
import { ADDONS, OVERVIEW_ADDONS } from '../addons'
import { TaskControlPanel } from './TaskControlPanel'
import { TaskLogsPanel } from './TaskLogsPanel'
import { api } from '../api'

/*
 * v30 — Whitelist / Blacklist management.
 *
 * The two tabs edit the task's *content policy*, the same store the bot and
 * mobile edit (settings surface keys blacklist_words / whitelist_words /
 * regex_block / regex_require / whitelist_mode, plus the per-list
 * blacklist_off / whitelist_off switches). Two things were broken and are
 * fixed here:
 *
 *  1. The tabs rendered whatever the parent last fetched, so a task another
 *     device had just edited showed stale words. The lists are re-read from
 *     /api/rules/{id}/settings every time the modal opens (and on an explicit
 *     refresh) — the cached rule object is never the source of truth.
 *  2. Only "add" and "remove" existed. Every entry now carries the
 *     management actions the other platforms have — edit, enable/disable,
 *     delete and details — and every one of them persists through the
 *     settings API, so a refresh cannot resurrect a deleted entry.
 */

// The settings keys the backend registers for the content policy. Reading and
// writing through these keys is what keeps the dashboard, the bot and mobile
// pinned to the same store.
const BL_KEY = 'blacklist_words'
const WL_KEY = 'whitelist_words'
const BL_REGEX_KEY = 'regex_block'
const WL_REGEX_KEY = 'regex_require'

type EntryKind = 'word' | 'regex'

interface WordEntry {
  value: string
  kind: EntryKind
}

/** Compact borderless action button used on a list entry's action row. */
function IconButton({ title, onClick, children }: { title: string; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      type="button"
      title={title}
      onClick={onClick}
      className="tom-iconbtn"
    >
      {children}
    </button>
  )
}


/** Split the stored lists into per-entry models the UI can render. */
function toEntries(words: string[] | undefined, regexes: string[] | undefined): WordEntry[] {
  const out: WordEntry[] = []
  for (const w of words || []) out.push({ value: w, kind: 'word' })
  for (const r of regexes || []) out.push({ value: r, kind: 'regex' })
  return out
}

interface Props {
  rule: ForwardRule | null
  isOpen: boolean
  onClose: () => void
  onSave: (updated: ForwardRule) => Promise<void>
  onDelete?: (id: string) => Promise<void>
  lang?: UiLang
}

type TabType =
  | 'overview'
  | 'control'
  | 'logs'
  | 'advanced'
  | 'all_addons'
  | 'replace'
  | 'blacklist'
  | 'whitelist'
  | 'cleaner'
  | 'filters'
  | 'delay'
  | 'every_n'
type FeatureGroupType = 'all' | 'crypto' | 'mmo' | 'content'

export function TaskOverviewModal({
  rule,
  isOpen,
  onClose,
  onSave,
  onDelete,
  lang = 'en',
}: Props) {
  const isRtl = lang === 'fa'
  const t = makeT(lang)
  const [currentTab, setCurrentTab] = useState<TabType>('overview')
  const [activeGroup, setActiveGroup] = useState<FeatureGroupType>('all')
  const [draft, setDraft] = useState<ForwardRule>(rule ? { ...rule } : ({} as ForwardRule))
  const [saving, setSaving] = useState(false)
  const [isEditingName, setIsEditingName] = useState(false)
  const [showTopicPrompt, setShowTopicPrompt] = useState(false)

  // Subview input states for v2.0 cards
  const [newReplaceFrom, setNewReplaceFrom] = useState('')
  const [newReplaceTo, setNewReplaceTo] = useState('')
  const [newBlWord, setNewBlWord] = useState('')
  const [newWlWord, setNewWlWord] = useState('')
  const [newBlRegex, setNewBlRegex] = useState('')
  const [newWlRegex, setNewWlRegex] = useState('')
  const [blRegexError, setBlRegexError] = useState('')
  const [wlRegexError, setWlRegexError] = useState('')
  const [newCleanerPattern, setNewCleanerPattern] = useState('')

  // v30: the content-policy lists, loaded from the task's stored settings —
  // never from the parent's cached copy. A modal opened on a task another
  // device just edited must show that edit, not whatever the dashboard last
  // fetched.
  const [blEntries, setBlEntries] = useState<WordEntry[]>([])
  const [wlEntries, setWlEntries] = useState<WordEntry[]>([])
  const [contentLoading, setContentLoading] = useState(false)
  const [contentError, setContentError] = useState('')
  // The per-list master switches. Both are *stored* settings the engine itself
  // honors (blacklist_off / whitelist_off), so the state survives a reload
  // and every client agrees — this is not UI-only paint.
  const [blEnabled, setBlEnabled] = useState(true)
  const [wlEnabled, setWlEnabled] = useState(true)
  // Per-entry inline edit + details.
  const [editing, setEditing] = useState<{ list: 'bl' | 'wl'; index: number } | null>(null)
  const [editValue, setEditValue] = useState('')
  const [detailsFor, setDetailsFor] = useState<{ list: 'bl' | 'wl'; index: number } | null>(null)


  const listPatterns = (val: any): string[] => {
    if (!val) return []
    if (Array.isArray(val)) return val
    return []
  }


  /**
   * Re-read the task's stored content policy from the settings surface.
   *
   * The settings endpoint is the *only* read path used here: the bot, the
   * mobile client and this dashboard all write through it, so reading it back
   * is what guarantees the list shown is the list the server enforces. The
   * `content_policy` blob riding on the rule object is deliberately ignored —
   * it is the parent's possibly-stale copy.
   */
  const reloadContentPolicy = useCallback(async (ruleId: string) => {
    setContentLoading(true)
    setContentError('')
    try {
      const res: any = await api.getRuleSettings(ruleId)
      const s: Record<string, any> = res?.settings || {}
      setBlEntries(toEntries(s[BL_KEY], s[BL_REGEX_KEY]))
      setWlEntries(toEntries(s[WL_KEY], s[WL_REGEX_KEY]))
      // A list the store says nothing about is enabled: an absent switch must
      // never read as off, or the dashboard would disable a task's filtering
      // the first time it is opened.
      setBlEnabled(s.blacklist_off !== true)
      setWlEnabled(s.whitelist_off !== true)
    } catch (e: any) {
      setContentError(e?.message || t('ui_failed_to_load'))
    } finally {
      setContentLoading(false)
    }
  }, [t])

  /**
   * Persist the edited lists through the settings surface. Both lists and
   * both switches go in one request, so a save cannot leave the blacklist
   * updated and the whitelist half-written.
   */
  const saveContentPolicy = useCallback(
    async (bl: WordEntry[], wl: WordEntry[], blOn: boolean, wlOn: boolean) => {
      if (!rule?.id) return false
      setContentLoading(true)
      setContentError('')
      try {
        await api.updateRuleSettings(rule.id, {
          blacklist_words: bl.filter((e) => e.kind === 'word').map((e) => e.value),
          regex_block: bl.filter((e) => e.kind === 'regex').map((e) => e.value),
          whitelist_words: wl.filter((e) => e.kind === 'word').map((e) => e.value),
          regex_require: wl.filter((e) => e.kind === 'regex').map((e) => e.value),
          // The engine honors these two flags directly: switching a list off
          // parks it (the words are kept), switching it back on restores it.
          blacklist_off: !blOn,
          whitelist_off: !wlOn,
        })
        setBlEntries(bl)
        setWlEntries(wl)
        setBlEnabled(blOn)
        setWlEnabled(wlOn)
        return true
      } catch (e: any) {
        setContentError(e?.message || t('ui_failed_to_save'))
        return false
      } finally {
        setContentLoading(false)
      }
    },
    [rule?.id, t],
  )

  useEffect(() => {
    if (rule) {
      setDraft({ ...rule })
      setCurrentTab('overview')
      setNewBlWord('')
      setNewWlWord('')
      setNewBlRegex('')
      setNewWlRegex('')
      setBlRegexError('')
      setWlRegexError('')
      setEditing(null)
      setDetailsFor(null)
      // The modal was opened (or switched to another task): re-read the
      // server's copy of the lists instead of trusting the cached rule.
      reloadContentPolicy(rule.id)
    }
  }, [rule, reloadContentPolicy])


  /*
   * v30: the shared renderer for one managed list (blacklist or whitelist).
   * Every entry carries Add, Edit, Enable/Disable, Delete and View Details,
   * and each action persists through saveContentPolicy — the server is the
   * only authority, so a refresh can never resurrect a deleted entry or lose
   * a toggle.
   */
  const renderManagedList = (
    which: 'bl' | 'wl',
    title: string,
    addLabel: string,
    addPlaceholder: string,
  ) => {
    const entries = which === 'bl' ? blEntries : wlEntries
    const enabled = which === 'bl' ? blEnabled : wlEnabled
    const newWord = which === 'bl' ? newBlWord : newWlWord
    const setNewWord = which === 'bl' ? setNewBlWord : setNewWlWord
    const newRegex = which === 'bl' ? newBlRegex : newWlRegex
    const setNewRegex = which === 'bl' ? setNewBlRegex : setNewWlRegex
    const setRegexError = which === 'bl' ? setBlRegexError : setWlRegexError
    const emptyKey = which === 'bl' ? 'ui_no_words_in_blacklist' : 'ui_no_whitelist_restrictions_all_permitted'

    // Add: whitespace/comma separated, trimmed, duplicates rejected rather
    // than silently creating a second copy of an existing entry.
    const addEntries = (kind: EntryKind) => {
      const raw = (kind === 'word' ? newWord : newRegex).trim()
      if (!raw) return
      const parts = kind === 'word'
        ? raw.split(/[\n,]/).map((s) => s.trim()).filter(Boolean)
        : [raw]
      const existing = new Set(entries.map((e) => e.value))
      const dupes = parts.filter((p) => existing.has(p))
      if (dupes.length) {
        setContentError(t('ui_filter_entry_duplicate'))
        return
      }
      setContentError('')
      const next = [...entries, ...parts.map((v) => ({ value: v, kind }))]
      saveContentPolicy(
        which === 'bl' ? next : blEntries,
        which === 'wl' ? next : wlEntries,
        blEnabled, wlEnabled,
      )
      if (kind === 'word') setNewWord('')
      else setNewRegex('')
    }

    const setEntry = (idx: number, value: string) => {
      const next = entries.map((e, j) => (j === idx ? { value, kind: e.kind } : e))
      saveContentPolicy(
        which === 'bl' ? next : blEntries,
        which === 'wl' ? next : wlEntries,
        blEnabled, wlEnabled,
      )
    }

    const removeEntry = (idx: number) => {
      const next = entries.filter((_, j) => j !== idx)
      setEditing(null)
      setDetailsFor(null)
      saveContentPolicy(
        which === 'bl' ? next : blEntries,
        which === 'wl' ? next : wlEntries,
        blEnabled, wlEnabled,
      )
    }

    return (
      <div className="tom-view">
        <div className="tom-row">
          <button
            type="button"
            onClick={() => setCurrentTab('overview')}
            className="tom-back"
          >
            {t('ui_back_to_task_overview')}
          </button>
          <div className="tom-inline tom-inline--roomy">
            <button
              type="button"
              onClick={() => rule?.id && reloadContentPolicy(rule.id)}
              disabled={contentLoading}
              className="tom-back tom-back--sm"
            >
              {t('ui_refresh')}
            </button>
            <span className={'tom-count tom-count--' + which + (enabled ? '' : ' is-off')}>
              {title} {entries.length}
            </span>
          </div>
        </div>

        {/* The per-list master switch. One round trip, server-stored: the
            engine itself skips a switched-off list, so this is not paint. */}
        <div
          className={'tom-card tom-master' + (enabled ? ' is-on' : '')}
        >
          <div className="tom-label tom-label--sm">
            {enabled ? t('ui_filter_list_enabled') : t('ui_filter_list_disabled')}
            <span className="tom-sub tom-sub--block">
              {t('ui_filter_list_toggle_hint')}
            </span>
          </div>
          <button
            type="button"
            disabled={contentLoading}
            onClick={() =>
              saveContentPolicy(
                blEntries, wlEntries,
                which === 'bl' ? !blEnabled : blEnabled,
                which === 'wl' ? !wlEnabled : wlEnabled,
              )
            }
            className={'tom-pill tom-pill--master tom-pill--' + which + (enabled ? ' is-on' : '')}
          >
            {enabled ? t('ui_filter_list_disable') : t('ui_filter_list_enable')}
          </button>
        </div>

        {contentError && (
          <div className="tom-errbox">
            {contentError}
          </div>
        )}
        {contentLoading && (
          <div className="tom-sub">{t('ui_filter_list_loading')}</div>
        )}

        <div className="tom-card">
          <div className="tom-title">{addLabel}</div>
          <div className="tom-inline tom-inline--roomy">
            <input
              type="text"
              placeholder={addPlaceholder}
              value={newWord}
              onChange={(e) => setNewWord(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); addEntries('word') } }}
              className="tom-input tom-input--grow"
            />
            <button
              type="button"
              disabled={contentLoading}
              onClick={() => addEntries('word')}
              className={'btn tom-addbtn tom-addbtn--' + which}
            >
              {t('ui_add')}
            </button>
          </div>
        </div>

        <div className="tom-card">
          <div className="tom-title tom-title--b">
            {t('ui_filter_entries_title')}
          </div>
          <div className="tom-col tom-col--tight">
            {entries.map((entry, idx) => {
              const isEditing = editing?.list === which && editing.index === idx
              const isDetails = detailsFor?.list === which && detailsFor.index === idx
              return (
                <div
                  key={`${which}-${idx}`}
                  className={'tom-entry' + (enabled ? '' : ' is-off')}
                >
                  <div className="tom-wrap">
                    {isEditing ? (
                      <>
                        <input
                          type="text"
                          value={editValue}
                          autoFocus
                          onChange={(e) => setEditValue(e.target.value)}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter') {
                              e.preventDefault()
                              const v = editValue.trim()
                              if (!v) return
                              if (entries.some((e2, j) => j !== idx && e2.value === v)) {
                                setContentError(t('ui_filter_entry_duplicate'))
                                return
                              }
                              setContentError('')
                              setEditing(null)
                              setEntry(idx, v)
                            } else if (e.key === 'Escape') {
                              setEditing(null)
                            }
                          }}
                          className={'tom-input tom-input--edit tom-input--' + which + (entry.kind === 'regex' ? ' tom-input--mono' : '')}
                        />
                        <button type="button" onClick={() => setEditing(null)} className="tom-chipbtn tom-chipbtn--muted">
                          {t('ui_cancel')}
                        </button>
                        <button
                          type="button"
                          onClick={() => {
                            const v = editValue.trim()
                            if (!v) return
                            if (entries.some((e2, j) => j !== idx && e2.value === v)) {
                              setContentError(t('ui_filter_entry_duplicate'))
                              return
                            }
                            setContentError('')
                            setEditing(null)
                            setEntry(idx, v)
                          }}
                          className="tom-chipbtn tom-chipbtn--ok"
                        >
                          {t('ui_save')}
                        </button>
                      </>
                    ) : (
                      <>
                        <span
                          className={'tom-entry__value tom-entry__value--' + which + (entry.kind === 'regex' ? ' tom-mono' : '') + (enabled ? '' : ' is-off')}
                        >
                          {entry.value}
                        </span>
                        {entry.kind === 'regex' && (
                          <span className="tom-kindtag">
                            regex
                          </span>
                        )}
                        <div className="tom-inline tom-inline--end">
                          <IconButton title={t('ui_edit')} onClick={() => { setEditing({ list: which, index: idx }); setEditValue(entry.value) }}>
                            <Edit2 size={12} />
                          </IconButton>
                          <IconButton
                            title={t('ui_filter_entry_details')}
                            onClick={() => setDetailsFor(isDetails ? null : { list: which, index: idx })}
                          >
                            <Info size={12} />
                          </IconButton>
                          <IconButton title={t('ui_delete')} onClick={() => removeEntry(idx)}>
                            <Trash2 size={12} />
                          </IconButton>
                        </div>
                      </>
                    )}
                  </div>
                  {isDetails && (
                    <div className="tom-details">
                      <div><strong>{t('ui_filter_detail_kind')}:</strong> {entry.kind === 'regex' ? t('ui_filter_detail_kind_regex') : t('ui_filter_detail_kind_word')}</div>
                      <div><strong>{t('ui_filter_detail_value')}:</strong> <code>{entry.value}</code></div>
                      <div><strong>{t('ui_filter_detail_list')}:</strong> {title}</div>
                      <div><strong>{t('ui_filter_detail_state')}:</strong> {enabled ? t('ui_filter_list_enabled') : t('ui_filter_list_disabled')}</div>
                      <div><strong>{t('ui_filter_detail_task')}:</strong> {rule?.name || rule?.id}</div>
                    </div>
                  )}
                </div>
              )
            })}
            {entries.length === 0 && (
              <span className="tom-sub">{t(emptyKey)}</span>
            )}
          </div>
        </div>

        {/* Regex entries for the same list: adding one is validated client-side
            only to warn; the server is the authority on what it stores. */}
        <div className="tom-card">
          <div className="tom-title">
            {which === 'bl' ? t('ui_filter_regex_block_title') : t('ui_filter_regex_require_title')}
          </div>
          <div className="tom-inline tom-inline--roomy">
            <input
              type="text"
              placeholder={which === 'bl' ? t('ui_filter_regex_block_placeholder') : t('ui_filter_regex_require_placeholder')}
              value={newRegex}
              onChange={(e) => { setNewRegex(e.target.value); setRegexError('') }}
              onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); addEntries('regex') } }}
              className={(which === 'bl' ? blRegexError : wlRegexError) ? 'tom-input tom-input--grow tom-input--invalid' : 'tom-input tom-input--grow tom-input--mono'}
            />
            <button
              type="button"
              disabled={contentLoading}
              onClick={() => addEntries('regex')}
              className={'btn tom-addbtn tom-addbtn--' + which}
            >
              {t('ui_add')}
            </button>
          </div>
          {(which === 'bl' ? blRegexError : wlRegexError) && (
            <div className="tom-errtext">
              {which === 'bl' ? blRegexError : wlRegexError}
            </div>
          )}
        </div>
      </div>
    )
  }

  const handleToggle = (field: keyof ForwardRule) => {
    setDraft((prev) => ({
      ...prev,
      [field]: !prev[field],
    }))
  }

  const handleAddonToggle = (addonKey: string) => {
    // "Forward Every N" is a number, not a switch: the catalogue row opens the
    // interval screen, exactly as the bot's add-on row does. Flipping a boolean
    // here would set a flag the engine does not read and leave the panel
    // claiming "On" while every message still goes through.
    if (addonKey === 'every_n') {
      setCurrentTab('every_n')
      return
    }
    setDraft((prev) => {
      const currentAddons = { ...(prev.paid_addons || {}) }
      currentAddons[addonKey] = !currentAddons[addonKey]
      return {
        ...prev,
        paid_addons: currentAddons,
      }
    })
  }

  /** The draft's interval, clamped the way the server clamps it. */
  const everyN = (() => {
    const raw = Number(draft.forward_every_n ?? draft.settings?.forward_every_n ?? 1)
    if (!Number.isFinite(raw)) return 1
    return Math.min(1000, Math.max(1, Math.trunc(raw)))
  })()

  const setEveryN = (value: number) => {
    const n = Number.isFinite(value) ? Math.min(1000, Math.max(1, Math.trunc(value))) : 1
    setDraft((prev) => ({
      ...prev,
      forward_every_n: n,
      // The add-on flag mirrors the number so an older client that only reads
      // `paid_addons` still shows the feature as on — and off again at 1.
      paid_addons: { ...(prev.paid_addons || {}), every_n: n > 1 },
    }))
  }

  const handleSaveAndClose = async () => {
    setSaving(true)
    try {
      await onSave(draft)
      onClose()
    } catch (err) {
      console.error('Failed to save task settings:', err)
    } finally {
      setSaving(false)
    }
  }


  const taskName = draft.name || `af_route_lab_${(draft.id || '').slice(0, 4)}`
  const sourceName = draft.source_chat_name || draft.source_chat_id || 'AF Route Lab Source'
  const targetName = draft.target_chat_name || draft.target_chat_id || 'AF Route Lab Target'
  const isRunning = draft.is_active
  const debugOn = draft.debug_mode ?? false

  if (!isOpen || !rule) return null

  return (
    <div
      className="tom-scrim" dir={isRtl ? 'rtl' : 'ltr'}
    >
      <div
        className="tom-shell"
      >
        {/* Top Header matching reference screens */}
        <div
          className="tom-header"
        >
          <div className="tom-inline tom-inline--roomy">
            {currentTab !== 'overview' && (
              <button
                type="button"
                onClick={() => setCurrentTab('overview')}
                className="btn tom-headerbtn"
              >
                {t('ui_back')}
              </button>
            )}
            {isEditingName ? (
              <input
                type="text"
                value={draft.name || ''}
                onChange={(e) => setDraft({ ...draft, name: e.target.value })}
                onBlur={() => setIsEditingName(false)}
                autoFocus
                className="tom-input tom-rename-input"
              />
            ) : (
              <div className="tom-inline">
                <span className="tom-headtitle">
                  {taskName}
                </span>
                <button
                  type="button"
                  onClick={() => setIsEditingName(true)}
                  className="btn btn--icon tom-rename"
                >
                  <Edit2 size={14} />
                </button>
              </div>
            )}
          </div>

          <div className="tom-inline tom-inline--roomy">
            {onDelete && (
              <button
                type="button"
                onClick={() => onDelete(draft.id)}
                title={t('ui_delete_task')}
                className="btn btn--icon tom-headerbtn tom-headerbtn--danger"
              >
                <Trash2 size={18} />
              </button>
            )}
            <button
              type="button"
              onClick={onClose}
              className="btn btn--icon tom-headerbtn"
            >
              <X size={18} />
            </button>
          </div>
        </div>

        {/* Scrollable Modal Content */}
        <div className="tom-body">
          {/* Tab strip: every view one click away. Sticky at the top of the
              scrollable body; twelve tabs overflow a phone width, so the row
              scrolls horizontally and snaps. */}
          <div className="tom-tabbar" role="tablist" aria-label={t('ui_task_overview')}>
            {([
              { id: 'overview' as const, label: t('ui_task_overview'), icon: <LayoutDashboard size={13} aria-hidden="true" /> },
              { id: 'control' as const, label: t('ui_full_control_sources'), icon: <SlidersHorizontal size={13} aria-hidden="true" /> },
              { id: 'logs' as const, label: t('ui_task_logs'), icon: <ScrollText size={13} aria-hidden="true" /> },
              { id: 'advanced' as const, label: t('ui_advanced_settings'), icon: <Settings2 size={13} aria-hidden="true" /> },
              { id: 'all_addons' as const, label: t('ui_paid_addon'), icon: <Sparkles size={13} aria-hidden="true" /> },
              { id: 'replace' as const, label: t('ui_replace'), icon: <Replace size={13} aria-hidden="true" /> },
              { id: 'blacklist' as const, label: t('ui_task_overview_modal_blacklist'), icon: <Ban size={13} aria-hidden="true" /> },
              { id: 'whitelist' as const, label: t('ui_whitelist'), icon: <ListChecks size={13} aria-hidden="true" /> },
              { id: 'cleaner' as const, label: t('ui_cleaner'), icon: <Eraser size={13} aria-hidden="true" /> },
              { id: 'filters' as const, label: t('ui_filters'), icon: <Filter size={13} aria-hidden="true" /> },
              { id: 'delay' as const, label: t('ui_set_delay'), icon: <Clock size={13} aria-hidden="true" /> },
              { id: 'every_n' as const, label: t('ui_set_every_n'), icon: <Hash size={13} aria-hidden="true" /> },
            ] as const).map((tab) => (
              <button
                key={tab.id}
                type="button"
                role="tab"
                aria-selected={currentTab === tab.id}
                className={'tom-tabbar__btn' + (currentTab === tab.id ? ' is-active' : '')}
                onClick={() => setCurrentTab(tab.id)}
              >
                {tab.icon}
                <span>{tab.label}</span>
              </button>
            ))}
          </div>
          {currentTab === 'overview' && (
            <div className="tom-view tom-view--roomy">
              {/* Task Overview Card (Screens 1 & 5) */}
              <div
                className="tom-card"
              >
                <div
                  className="tom-row tom-mb-4"
                >
                  <span className="tom-overline tom-overline--loose">{t('ui_task_overview')}</span>
                  <div className="tom-inline tom-inline--roomy">
                    <span className="tom-muted">{t('ui_forwarding_status')}</span>
                    <button
                      type="button"
                      onClick={() => handleToggle('is_active')}
                      className={'tom-pill tom-pill--status' + (isRunning ? ' is-on' : '')}
                    >
                      {isRunning ? <Play size={10} fill="#fff" /> : <Pause size={10} fill="#fff" />}
                      {isRunning ? t('ui_running') : t('ui_paused')}
                    </button>
                  </div>
                </div>

                {/* Route Visualizer */}
                <div
                  className="tom-route"
                >
                  <div className="tom-flex1">
                    <div className="tom-sub tom-mb-1">{t('ui_source')}</div>
                    <div className="tom-sectitle tom-sectitle--soft">{sourceName}</div>
                    <div className="tom-tgtag">{t('ui_telegram')}</div>
                  </div>
                  <ArrowRight size={18} color="var(--color-text-muted)" className="tom-route__arrow" />
                  <div className="tom-flex1 tom-end">
                    <div className="tom-sub tom-mb-1">{t('ui_target')}</div>
                    <div className="tom-sectitle tom-sectitle--soft">{targetName}</div>
                    <div className="tom-tgtag">{t('ui_telegram')}</div>
                  </div>
                </div>

                {/* Show Forward Header row (v2.0 app update matching screenshot) */}
                <div
                  className="tom-subrow"
                >
                  <div className="tom-row">
                    <span className="tom-label tom-label--sm">{t('ui_show_forward_header')}</span>
                    <button
                      type="button"
                      onClick={() => handleToggle('show_forward_header')}
                      className={'tom-pill tom-pill--tight' + (draft.show_forward_header ? ' is-on' : '')}
                    >
                      {draft.show_forward_header ? t('ui_yes') : t('ui_no')}
                    </button>
                  </div>
                  {draft.show_forward_header && (
                    <div className="tom-warnnote">
                      {t('ui_reply_edit_replace_translate_will_not_work_when_show_header')}
                    </div>
                  )}
                </div>

                {/* Debug Mode row */}
                <div
                  className="tom-row tom-row--rule"
                >
                  <div className="tom-inline">
                    <Bug size={14} className="tom-ico tom-ico--muted" />
                    <span className="tom-muted">{t('ui_debug_mode')}</span>
                  </div>
                  <button
                    type="button"
                    onClick={() => handleToggle('debug_mode')}
                    className={'tom-pill tom-pill--tight' + (debugOn ? ' is-on' : '')}
                  >
                    {debugOn ? t('ui_on') : t('ui_off')}
                  </button>
                </div>
              </div>

              {/* Sender Type Card */}
              <div
                className="tom-card"
              >
                <div>
                  <div className="tom-sub">{t('ui_sender_type')}</div>
                  <div className="tom-rowtitle">
                    {draft.sender_type || t('ui_personal_telegram_account')}
                  </div>
                </div>
                <select
                  value={draft.sender_type || 'Personal Telegram Account'}
                  onChange={(e) => setDraft({ ...draft, sender_type: e.target.value })}
                  className="tom-select"
                >
                  <option value="Personal Telegram Account">{t('ui_personal_telegram_account')}</option>
                  <option value="Bot Account">{t('ui_bot_account')}</option>
                  <option value="MTProto Premium Userbot">{t('ui_mtproto_premium_userbot')}</option>
                </select>
              </div>

              {/* Edit Task Section: 4 Distinct Action Buttons (Screen 1 & 5) */}
              <div>
                <div className="tom-overline">
                  {t('ui_edit_task')}
                </div>
                <div className="tom-grid2 tom-mb-2">
                  {/* Green Button: Edit Source */}
                  <button
                    type="button"
                    onClick={() => {
                      const newSrc = prompt(t('ui_enter_source_chat_id_or_title'), draft.source_chat_id)
                      if (newSrc) setDraft({ ...draft, source_chat_id: newSrc, source_chat_name: newSrc })
                    }}
                    className="btn btn--primary tom-action"
                  >
                    {t('ui_edit_source')}
                  </button>

                  {/* Red Button: Edit Target */}
                  <button
                    type="button"
                    onClick={() => {
                      const newDst = prompt(t('ui_enter_target_chat_id_or_title'), draft.target_chat_id)
                      if (newDst) setDraft({ ...draft, target_chat_id: newDst, target_chat_name: newDst })
                    }}
                    className="btn btn--danger tom-action"
                  >
                    {t('ui_edit_target')}
                  </button>
                </div>

                {/* Blue Button: Set up Forwarding Topic */}
                <button
                  type="button"
                  onClick={() => setShowTopicPrompt(!showTopicPrompt)}
                  className="btn btn--primary tom-action tom-action--gap"
                >
                  <Hash size={14} />
                  {t('ui_set_up_forwarding_topic')} {draft.topic_id ? `(#${draft.topic_id})` : ''}
                </button>

                {showTopicPrompt && (
                  <div
                    className="tom-inline tom-inline--roomy tom-topic"
                  >
                    <input
                      type="number"
                      placeholder={t('ui_topic_thread_id_0_to_remove')}
                      value={draft.topic_id || ''}
                      onChange={(e) => setDraft({ ...draft, topic_id: e.target.value })}
                      className="tom-input tom-input--topic"
                    />
                    <button
                      type="button"
                      onClick={() => setShowTopicPrompt(false)}
                      className="btn btn--primary tom-topic__done"
                    >
                      {t('ui_done')}
                    </button>
                  </div>
                )}

                {/* Dark Outline Button: Advanced Settings */}
                <button
                  type="button"
                  onClick={() => setCurrentTab('advanced')}
                  className="btn tom-action"
                >
                  <SlidersHorizontal size={14} />
                  {t('ui_advanced_settings')}
                </button>

                {/* Full per-task control: multi-source + every setting the
                    server knows about, rendered from its schema. */}
                <button
                  type="button"
                  onClick={() => setCurrentTab('control')}
                  className="btn tom-action"
                >
                  <SlidersHorizontal size={14} />
                  {t('ui_full_control_sources')}
                </button>

                {/* This task's complete history: paged, searchable, exportable. */}
                <button
                  type="button"
                  onClick={() => setCurrentTab('logs')}
                  className="btn tom-action tom-action--gap"
                >
                  <ScrollText size={14} />
                  {t('ui_task_logs')}
                </button>
              </div>

              {/* v2.0 App Update — Features Section matching mobile screenshot */}
              <div
                className="tom-card"
              >
                <div
                  className="tom-row tom-mb-3"
                >
                  <span className="tom-sectitle">{t('ui_features')}</span>
                  <span className="tom-linklike">
                    {t('ui_explain')} &gt;
                  </span>
                </div>

                {/* 5 Toggle Pills matching screenshot (top half of Features) */}
                <div className="tom-wrap tom-mb-4">
                  {[
                    { key: 'sync_edits' as const, label: t('ui_edit'), active: Boolean(draft.sync_edits ?? true) },
                    { key: 'sync_deletes' as const, label: t('ui_delete'), active: Boolean(draft.sync_deletes ?? true) },
                    { key: 'sync_replies' as const, label: t('ui_reply'), active: Boolean(draft.sync_replies ?? true) },
                    { key: 'show_forward_header' as const, label: t('ui_show_forward_header'), active: Boolean(draft.show_forward_header) },
                    { key: 'duplicate_filters' as const, label: t('ui_duplicate_filters'), active: Boolean(draft.duplicate_filters ?? true) },
                  ].map((item) => (
                    <button
                      key={item.key}
                      type="button"
                      onClick={() => handleToggle(item.key)}
                      className={'tom-feat' + (item.active ? ' is-on' : '')}
                    >
                      <span className="tom-feat__mark">{item.active ? <Check size={12} aria-hidden="true" /> : <Circle size={12} aria-hidden="true" />}</span>
                      <span>{item.label}</span>
                    </button>
                  ))}
                </div>

                {/* 6 Modular Option Cards (2x3 grid) matching screenshot */}
                <div className="tom-grid2">
                  {[
                    {
                      title: t('ui_replace'),
                      icon: <Replace size={14} aria-hidden="true" />,
                      tone: 'violet',
                      status: `${t('ui_activated')} ${Object.keys((draft as any).replacements || {}).length}`,
                      onClick: () => setCurrentTab('replace'),
                    },
                    {
                      title: t('ui_task_overview_modal_blacklist'),
                      icon: <Ban size={14} aria-hidden="true" />,
                      tone: 'danger',
                      status: `${t('ui_activated')} ${(draft.blacklist_keywords || []).length + (draft.regex_block || []).length}`,
                      onClick: () => setCurrentTab('blacklist'),
                    },
                    {
                      title: t('ui_whitelist'),
                      icon: <ListChecks size={14} aria-hidden="true" />,
                      tone: 'success',
                      status: `${t('ui_activated')} ${(draft.whitelist_keywords || []).length + (draft.regex_require || []).length}`,
                      onClick: () => setCurrentTab('whitelist'),
                    },
                    {
                      title: t('ui_cleaner'),
                      icon: <Eraser size={14} aria-hidden="true" />,
                      tone: 'orange',
                      status: `${t('ui_activated')} ${draft.cleaner_enabled ? '1' : '0'}`,
                      onClick: () => setCurrentTab('cleaner'),
                    },
                    {
                      title: t('ui_filters'),
                      icon: <Filter size={14} aria-hidden="true" />,
                      tone: 'info',
                      status: `${t('ui_activated')} ${((draft.duplicate_filters ? 1 : 0) + [draft.block_text, draft.block_links, draft.block_voice, draft.block_stickers, draft.block_video, draft.block_audio].filter(Boolean).length)}`,
                      onClick: () => setCurrentTab('filters'),
                    },
                    {
                      title: t('ui_set_delay'),
                      icon: <Clock size={14} aria-hidden="true" />,
                      tone: 'warning',
                      status: `${t('ui_delay_current')} ${draft.forward_delay_seconds || draft.delay_seconds || 0}s`,
                      onClick: () => setCurrentTab('delay'),
                    },
                    {
                      title: t('ui_set_every_n'),
                      icon: <Hash size={14} aria-hidden="true" />,
                      tone: 'violet',
                      status:
                        everyN > 1
                          ? `${t('ui_every_n_current')} ${everyN}`
                          : t('ui_every_n_every_message'),
                      onClick: () => setCurrentTab('every_n'),
                    },
                  ].map((card) => (
                    <div
                      key={card.title}
                      onClick={card.onClick}
                      className="tom-opt"
                    >
                      <div className="tom-row tom-mb-1">
                        <div className="tom-inline">
                          <span className={'tom-opt__icon tom-opt__icon--' + card.tone}>{card.icon}</span>
                          <span className="tom-rowtitle tom-rowtitle--sm">{card.title} &gt;</span>
                        </div>
                      </div>
                      <div className="tom-micro">{card.status}</div>
                    </div>
                  ))}
                </div>
              </div>

              {/* Feature Groups Filter Chips */}
              <div>
                <div className="tom-row tom-mb-2">
                  <span className="tom-overline">{t('ui_feature_groups')}</span>
                  <span className="tom-sub">{t('ui_choose_customize_tools')}</span>
                </div>
                <div className="tom-scrollrow">
                  {[
                    { id: 'all', label: t('ui_all') },
                    { id: 'crypto', label: t('ui_crypto') },
                    { id: 'mmo', label: t('ui_mmo_affiliate') },
                    { id: 'content', label: t('ui_content') },
                  ].map((grp) => (
                    <button
                      key={grp.id}
                      type="button"
                      onClick={() => setActiveGroup(grp.id as FeatureGroupType)}
                      className={'tom-chipbtn' + (activeGroup === grp.id ? ' is-on' : '')}
                    >
                      {grp.label}
                    </button>
                  ))}
                </div>
              </div>

              {/* PAID ADDON Banner & 2x3 Grid (Screen 4) */}
              <div
                className="tom-card"
              >
                <div
                  className="tom-row tom-mb-3"
                >
                  <div className="tom-inline">
                    <Sparkles size={14} className="tom-ico tom-ico--warn" />
                    <span className="tom-sectitle tom-sectitle--warn">{t('ui_paid_addon')}</span>
                    <span title={t('ui_unlock_premium_features_with_credits')} className="tom-inlineflex">
                      <Info size={13} color="var(--color-text-muted)" />
                    </span>
                  </div>
                  <button
                    type="button"
                    onClick={() => setCurrentTab('all_addons')}
                    className="tom-linklike"
                  >
                    {t('ui_view_all_18')} <ChevronRight size={14} />
                  </button>
                </div>

                {/* 2x3 Grid of Addons matching reference Image 4 */}
                <div className="tom-grid2">
                  {OVERVIEW_ADDONS.map((item) => {
                    const isEnabled = draft.paid_addons?.[item.key] ?? false
                    return (
                      <div
                        key={item.key}
                        onClick={() => handleAddonToggle(item.key)}
                        className={'tom-opt tom-opt--addon' + (isEnabled ? ' is-on' : '')}
                      >
                        <div className="tom-opt__head">
                          <span className="tom-opt__eyebrow">
                            {item.locked && <Lock size={10} className="tom-lock" aria-hidden="true" />}
                            <Star size={11} aria-hidden="true" /> {t('ui_addon')}
                          </span>
                          <span
                            className={'tom-statuschip' + (isEnabled ? ' is-on' : '')}
                          >
                            {isEnabled ? t('ui_task_overview_modal_on') : t('ui_task_overview_modal_off')}
                          </span>
                        </div>
                        <div className="tom-opt__label">
                          {t(item.labelKey)}
                        </div>
                      </div>
                    )
                  })}
                </div>
              </div>
            </div>
          )}

          {/* Advanced Settings View (Screens 2 & 3) */}
          {currentTab === 'advanced' && (
            <div className="tom-view">
              {/* FILTERS (Screen 2) */}
              <div
                className="tom-card"
              >
                <div className="tom-title tom-title--flag">
                  <Shield size={14} className="tom-ico tom-ico--info" /> {t('ui_task_overview_modal_filters')}
                </div>
                <div className="tom-col">
                  <div className="tom-row">
                    <span className="tom-label">{t('ui_duplicate_filters')}</span>
                    <button
                      type="button"
                      onClick={() => handleToggle('duplicate_filters')}
                      className={'tom-pill' + (draft.duplicate_filters ? ' is-on' : '')}
                    >
                      {draft.duplicate_filters ? t('ui_on') : t('ui_off')}
                    </button>
                  </div>

                  <div className="tom-row">
                    <span className="tom-label">{t('ui_crypto_mode')}</span>
                    <button
                      type="button"
                      onClick={() => handleToggle('crypto_mode')}
                      className={'tom-pill' + (draft.crypto_mode ? ' is-on' : '')}
                    >
                      {draft.crypto_mode ? t('ui_on') : t('ui_off')}
                    </button>
                  </div>

                  <div className="tom-row">
                    <span className="tom-label">{t('ui_blacklist_whitelist')}</span>
                    <span className="tom-muted">
                      {t('ui_activated')} {(draft.domain_blocklist?.length || 0) + (draft.domain_allowlist?.length || 0)}
                    </span>
                  </div>
                </div>
              </div>

              {/* SCHEDULES (Screen 2) */}
              <div
                className="tom-card"
              >
                <div className="tom-title tom-title--flag">
                  <Clock size={14} className="tom-ico tom-ico--warn" /> {t('ui_schedules')}
                </div>
                <div className="tom-col">
                  <div className="tom-row">
                    <span className="tom-label">{t('ui_auto_posting')}</span>
                    <button
                      type="button"
                      onClick={() => handleToggle('auto_posting')}
                      className={'tom-pill' + (draft.auto_posting ? ' is-on' : '')}
                    >
                      {draft.auto_posting ? t('ui_on') : t('ui_off')}
                    </button>
                  </div>

                  <div className="tom-row">
                    <span className="tom-label">{t('ui_power_control')}</span>
                    <button
                      type="button"
                      onClick={() => handleToggle('power_control')}
                      className={'tom-pill' + (draft.power_control ? ' is-on' : '')}
                    >
                      {draft.power_control ? t('ui_on') : t('ui_off')}
                    </button>
                  </div>
                </div>
              </div>

              {/* TIME FORWARD (Screen 2) */}
              <div
                className="tom-card"
              >
                <div className="tom-title tom-title--flag">
                  <Clock size={14} className="tom-ico tom-ico--violet" /> {t('ui_time_forward')}
                </div>
                <div className="tom-grid2 tom-grid2--roomy">
                  <div>
                    <label className="tom-hint">
                      {t('ui_delay_seconds')}
                    </label>
                    <input
                      type="number"
                      value={draft.delay_seconds || 0}
                      onChange={(e) => setDraft({ ...draft, delay_seconds: parseFloat(e.target.value) || 0 })}
                      className="tom-input"
                    />
                  </div>
                  <div>
                    <label className="tom-hint">
                      {t('ui_edit_time_limit_s')}
                    </label>
                    <input
                      type="number"
                      value={draft.edit_time_limit_seconds || 0}
                      onChange={(e) => setDraft({ ...draft, edit_time_limit_seconds: parseInt(e.target.value) || 0 })}
                      className="tom-input"
                    />
                  </div>
                </div>
              </div>

              {/* PROCESSING MESSAGES (Screen 2) */}
              <div
                className="tom-card"
              >
                <div className="tom-title tom-title--b">
                  {t('ui_processing_messages')}
                </div>
                <div className="tom-col">
                  <div className="tom-row">
                    <span className="tom-label">{t('ui_process_raw_text')}</span>
                    <button
                      type="button"
                      onClick={() => handleToggle('process_raw_text')}
                      className={'tom-pill' + (draft.process_raw_text ? ' is-on' : '')}
                    >
                      {draft.process_raw_text ? t('ui_on') : t('ui_off')}
                    </button>
                  </div>
                  <div className="tom-row">
                    <span className="tom-label">{t('ui_button_to_text')}</span>
                    <button
                      type="button"
                      onClick={() => handleToggle('button_to_text')}
                      className={'tom-pill' + (draft.button_to_text ? ' is-on' : '')}
                    >
                      {draft.button_to_text ? t('ui_on') : t('ui_off')}
                    </button>
                  </div>
                </div>
              </div>

              {/* MODIFY CONTENT (Screen 3) */}
              <div
                className="tom-card"
              >
                <div className="tom-title tom-title--b">
                  {t('ui_modify_content')}
                </div>
                <div className="tom-col tom-col--roomy">
                  <div>
                    <label className="tom-hint">
                      {t('ui_task_overview_modal_header')}
                    </label>
                    <input
                      type="text"
                      placeholder={t('ui_header_empty')}
                      value={draft.custom_header || ''}
                      onChange={(e) => setDraft({ ...draft, custom_header: e.target.value })}
                      className="tom-input"
                    />
                  </div>
                  <div>
                    <label className="tom-hint">
                      {t('ui_footer')}
                    </label>
                    <input
                      type="text"
                      placeholder={t('ui_footer_empty')}
                      value={draft.custom_footer || ''}
                      onChange={(e) => setDraft({ ...draft, custom_footer: e.target.value })}
                      className="tom-input"
                    />
                  </div>
                  <div>
                    <label className="tom-hint">
                      {t('ui_watermark')}
                    </label>
                    <input
                      type="text"
                      placeholder={t('ui_empty')}
                      value={draft.watermark_text || ''}
                      onChange={(e) => setDraft({ ...draft, watermark_text: e.target.value })}
                      className="tom-input"
                    />
                  </div>
                </div>
              </div>

              {/* AI FEATURES (Screen 3) */}
              <div
                className="tom-card"
              >
                <div
                  className="tom-row tom-mb-3"
                >
                  <div className="tom-inline">
                    <Wand2 size={14} className="tom-ico tom-ico--violet" />
                    <span className="tom-title tom-title--flat">{t('ui_ai_features')}</span>
                  </div>
                  <span
                    className="badge badge--info tom-creativity"
                  >
                    {t('ui_creativity')} {draft.ai_creativity ?? 0.7} - {t('ui_max_tokens')} {draft.ai_max_tokens ?? 600}
                  </span>
                </div>

                <div className="tom-col">
                  <div className="tom-row">
                    <span className="tom-label">{t('ui_ocr_image_ai')}</span>
                    <button
                      type="button"
                      onClick={() => handleToggle('ocr_image_ai')}
                      className={'tom-pill' + (draft.ocr_image_ai ? ' is-on' : '')}
                    >
                      {draft.ocr_image_ai ? t('ui_on') : t('ui_off')}
                    </button>
                  </div>

                  <div className="tom-row">
                    <span className="tom-label">{t('ui_smart_ai_rewrite')}</span>
                    <button
                      type="button"
                      onClick={() => handleToggle('smart_ai_rewrite')}
                      className={'tom-pill' + (draft.smart_ai_rewrite ? ' is-on' : '')}
                    >
                      {draft.smart_ai_rewrite ? t('ui_on') : t('ui_off')}
                    </button>
                  </div>
                </div>
              </div>
            </div>
          )}

          {/* ALL 18 ADDONS CATALOG MODAL VIEW */}
          {currentTab === 'all_addons' && (
            <div>
              <div className="tom-desc">
                {t('ui_modular_extensions_catalogue_toggle_features_for_this_task')}
              </div>
              <div className="tom-col">
                {ADDONS.map((item) => {
                  const isEnabled = draft.paid_addons?.[item.key] ?? false
                  return (
                    <div
                      key={item.key}
                      onClick={() => handleAddonToggle(item.key)}
                      className={'tom-opt tom-opt--row' + (isEnabled ? ' is-on' : '')}
                    >
                      <div>
                        <div className="tom-inline">
                          {item.locked && <Lock size={12} className="tom-ico tom-ico--warn" />}
                          <span className="tom-rowtitle">{t(item.labelKey)}</span>
                        </div>
                        <div className="tom-sub tom-mt-1">{t(item.descKey)}</div>
                      </div>
                      <span
                        className={'tom-statuschip' + (isEnabled ? ' is-on' : '')}
                      >
                        {isEnabled ? t('ui_active') : t('ui_task_overview_modal_off')}
                      </span>
                    </div>
                  )
                })}
              </div>
            </div>
          )}

          {/* 1. CLEANER ENGINE VIEW (v2.0) */}
          {currentTab === 'cleaner' && (
            <div className="tom-view">
              <div className="tom-row">
                <button
                  type="button"
                  onClick={() => setCurrentTab('overview')}
                  className="tom-back tom-back--icon"
                >
                  {t('ui_back_to_task_overview')}
                </button>
                <button
                  type="button"
                  onClick={() => handleToggle('cleaner_enabled')}
                  className={'tom-pill tom-pill--master' + (draft.cleaner_enabled ? ' is-on' : '')}
                >
                  {draft.cleaner_enabled ? t('ui_cleaner_active') : t('ui_cleaner_inactive')}
                </button>
              </div>

              <div
                className="tom-card"
              >
                <div className="tom-title tom-title--sm">
                  {t('ui_automatic_stripping_rules')}
                </div>
                <div className="tom-sub tom-mb-3">
                  {t('ui_cleaned_content_is_stripped_before_forwarding_so_sensitive_eleme')}
                </div>

                <div className="tom-grid2">
                  {[
                    { key: 'remove_urls', label: t('ui_strip_urls') },
                    { key: 'remove_mentions', label: t('ui_strip_mentions') },
                    { key: 'remove_hashtags', label: t('ui_strip_hashtags') },
                    { key: 'remove_emojis', label: t('ui_task_overview_modal_strip_emojis') },
                    { key: 'apply_to_caption', label: t('ui_clean_captions') },
                    { key: 'case_sensitive', label: t('ui_case_sensitive') },
                    { key: 'use_regex', label: t('ui_regex_engine') },
                  ].map((opt) => {
                    const cfg = (draft.cleaner_config || {}) as any
                    const isActive = Boolean(cfg[opt.key] ?? false)
                    return (
                      <button
                        key={opt.key}
                        type="button"
                        onClick={() => {
                          const updatedCfg = { ...cfg, [opt.key]: !isActive }
                          setDraft({
                            ...draft,
                            cleaner_enabled: true,
                            cleaner_config: updatedCfg,
                          })
                        }}
                        className={'tom-cleanopt' + (isActive ? ' is-on' : '')}
                      >
                        <span>{opt.label}</span>
                        <span className="tom-cleanopt__mark">{isActive ? <Check size={12} aria-hidden="true" /> : <Circle size={12} aria-hidden="true" />}</span>
                      </button>
                    )
                  })}
                </div>
              </div>

              {/* Custom Patterns to Strip */}
              <div
                className="tom-card"
              >
                <div className="tom-title">
                  {t('ui_custom_patterns_to_strip')}
                </div>
                <div className="tom-inline tom-inline--roomy tom-mb-3">
                  <input
                    type="text"
                    placeholder={t('ui_enter_word_phrase_or_ad_text')}
                    value={newCleanerPattern}
                    onChange={(e) => setNewCleanerPattern(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' && newCleanerPattern.trim()) {
                        e.preventDefault()
                        const cfg = (draft.cleaner_config || {}) as any
                        const pats = listPatterns(cfg.patterns)
                        if (!pats.includes(newCleanerPattern.trim())) {
                          setDraft({
                            ...draft,
                            cleaner_enabled: true,
                            cleaner_config: { ...cfg, patterns: [...pats, newCleanerPattern.trim()] },
                          })
                          setNewCleanerPattern('')
                        }
                      }
                    }}
                    className="tom-input tom-input--grow"
                  />
                  <button
                    type="button"
                    onClick={() => {
                      if (!newCleanerPattern.trim()) return
                      const cfg = (draft.cleaner_config || {}) as any
                      const pats = listPatterns(cfg.patterns)
                      if (!pats.includes(newCleanerPattern.trim())) {
                        setDraft({
                          ...draft,
                          cleaner_enabled: true,
                          cleaner_config: { ...cfg, patterns: [...pats, newCleanerPattern.trim()] },
                        })
                        setNewCleanerPattern('')
                      }
                    }}
                    className="btn tom-addbtn tom-addbtn--cleaner"
                  >
                    {t('ui_add')}
                  </button>
                </div>

                <div className="tom-wrap">
                  {listPatterns((draft.cleaner_config as any)?.patterns).map((p, idx) => (
                    <span
                      key={idx}
                      className="tom-chip"
                    >
                      <span>{p}</span>
                      <button
                        type="button"
                        onClick={() => {
                          const cfg = (draft.cleaner_config || {}) as any
                          const pats = listPatterns(cfg.patterns).filter((_, i) => i !== idx)
                          setDraft({
                            ...draft,
                            cleaner_config: { ...cfg, patterns: pats },
                          })
                        }}
                        className="tom-chip__x"
                      ><X size={12} aria-hidden="true" /></button>
                    </span>
                  ))}
                  {listPatterns((draft.cleaner_config as any)?.patterns).length === 0 && (
                    <span className="tom-sub">{t('ui_no_custom_strip_patterns_added_yet')}</span>
                  )}
                </div>
              </div>
            </div>
          )}

          {/* 2. TEXT REPLACEMENTS VIEW (< >) */}
          {currentTab === 'replace' && (
            <div className="tom-view">
              <div className="tom-row">
                <button
                  type="button"
                  onClick={() => setCurrentTab('overview')}
                  className="tom-back"
                >
                  {t('ui_back_to_task_overview')}
                </button>
                <span className="tom-count tom-count--violet">
                  {t('ui_task_overview_modal_active_rules')} {Object.keys((draft as any).replacements || {}).length}
                </span>
              </div>

              <div
                className="tom-card"
              >
                <div className="tom-title">
                  {t('ui_add_text_replacement')}
                </div>
                <div className="tom-grid2 tom-grid2--add">
                  <input
                    type="text"
                    placeholder={t('ui_find_text')}
                    value={newReplaceFrom}
                    onChange={(e) => setNewReplaceFrom(e.target.value)}
                    className="tom-input"
                  />
                  <input
                    type="text"
                    placeholder={t('ui_replace_with')}
                    value={newReplaceTo}
                    onChange={(e) => setNewReplaceTo(e.target.value)}
                    className="tom-input"
                  />
                  <button
                    type="button"
                    onClick={() => {
                      if (!newReplaceFrom.trim()) return
                      const reps = { ...(draft.replacements || {}), [newReplaceFrom.trim()]: newReplaceTo }
                      setDraft({ ...draft, replacements: reps })
                      setNewReplaceFrom('')
                      setNewReplaceTo('')
                    }}
                    className="btn tom-addbtn tom-addbtn--replace"
                  >
                    {t('ui_add')}
                  </button>
                </div>
              </div>

              {/* Replacements List */}
              <div
                className="tom-card"
              >
                <div className="tom-title tom-title--b">
                  {t('ui_active_replacements')}
                </div>
                <div className="tom-col">
                  {Object.entries((draft as any).replacements || {}).map(([from, to], idx) => (
                    <div
                      key={idx}
                      className="tom-opt tom-opt--flat"
                    >
                      <div className="tom-inline tom-inline--roomy tom-reprow">
                        <code className="tom-rep-from">{from}</code>
                        <ArrowRight size={12} className="tom-rep-arrow" aria-hidden="true" />
                        <code className="tom-rep-to">{to as string}</code>
                      </div>
                      <button
                        type="button"
                        onClick={() => {
                          const reps = { ...((draft as any).replacements || {}) }
                          delete reps[from]
                          setDraft({ ...draft, replacements: reps })
                        }}
                        className="tom-iconbtn tom-iconbtn--danger"
                      >
                        <Trash2 size={13} />
                      </button>
                    </div>
                  ))}
                  {Object.keys((draft as any).replacements || {}).length === 0 && (
                    <div className="tom-sub">{t('ui_no_text_replacements_configured')}</div>
                  )}
                </div>
              </div>
            </div>
          )}

          {/* 3. BLACKLIST VIEW (≡ₓ) — v30: server-backed management UI */}
          {currentTab === 'blacklist' && renderManagedList(
            'bl',
            t('ui_blacklisted'),
            t('ui_add_blacklisted_keyword_or_domain'),
            t('ui_enter_banned_word_or_phrase'),
          )}

          {/* 4. WHITELIST VIEW (≡✓) — v30: server-backed management UI */}
          {currentTab === 'whitelist' && renderManagedList(
            'wl',
            t('ui_whitelisted'),
            t('ui_required_whitelist_words'),
            t('ui_enter_whitelisted_word_or_phrase'),
          )}

          {/* 5. FILTERS VIEW (⊶) */}
          {currentTab === 'filters' && (
            <div className="tom-view">
              <div className="tom-row">
                <button
                  type="button"
                  onClick={() => setCurrentTab('overview')}
                  className="tom-back"
                >
                  {t('ui_back_to_task_overview')}
                </button>
                <span className="tom-count tom-count--info">{t('ui_media_type_filters')}</span>
              </div>

              <div
                className="tom-card"
              >
                <div className="tom-title">
                  {t('ui_filter_message_types')}
                </div>
                <div className="tom-col">
                  {[
                    { key: 'duplicate_filters' as const, label: t('ui_duplicate_filters_sha_256_fingerprint'), active: Boolean(draft.duplicate_filters) },
                    { key: 'block_text' as const, label: t('ui_block_text_messages'), active: Boolean(draft.block_text) },
                    { key: 'block_voice' as const, label: t('ui_block_voice_messages'), active: Boolean(draft.block_voice) },
                    { key: 'block_stickers' as const, label: t('ui_task_overview_modal_block_stickers'), active: Boolean(draft.block_stickers) },
                    { key: 'block_links' as const, label: t('ui_block_links'), active: Boolean(draft.block_links) },
                    { key: 'block_video' as const, label: t('ui_block_video_messages'), active: Boolean(draft.block_video) },
                    { key: 'block_audio' as const, label: t('ui_block_audio_files'), active: Boolean(draft.block_audio) },
                  ].map((f) => (
                    <div
                      key={f.key}
                      className="tom-opt tom-opt--flat"
                    >
                      <span className="tom-rowtext">{f.label}</span>
                      <button
                        type="button"
                        onClick={() => handleToggle(f.key)}
                        className={'tom-pill tom-pill--block' + (f.active ? ' is-on' : '')}
                      >
                        {f.active ? t('ui_blocked') : t('ui_allowed')}
                      </button>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          )}

          {/* 6. SET DELAY VIEW (⏱) */}
          {currentTab === 'delay' && (
            <div className="tom-view">
              <div className="tom-row">
                <button
                  type="button"
                  onClick={() => setCurrentTab('overview')}
                  className="tom-back"
                >
                  {t('ui_back_to_task_overview')}
                </button>
                <span className="tom-count tom-count--warn">
                  {t('ui_current_delay')} {draft.forward_delay_seconds || draft.delay_seconds || 0}s
                </span>
              </div>

              <div
                className="tom-card"
              >
                <div className="tom-title tom-title--sm">
                  {t('ui_configure_forward_delay')}
                </div>
                <div className="tom-sub tom-mb-4">
                  {t('ui_adds_an_asynchronous_pause_before_forwarding_each_message_to_pre')}
                </div>

                <div className="tom-mb-4">
                  <label className="tom-overline tom-overline--block">
                    {t('ui_delay_in_seconds')} <strong>{draft.forward_delay_seconds || draft.delay_seconds || 0}s</strong>
                  </label>
                  <input
                    type="range"
                    min="0"
                    max="120"
                    value={draft.forward_delay_seconds || draft.delay_seconds || 0}
                    onChange={(e) => {
                      const v = parseFloat(e.target.value) || 0
                      setDraft({ ...draft, forward_delay_seconds: v, delay_seconds: v })
                    }}
                    className="tom-range"
                  />
                </div>

                <div className="tom-overline tom-overline--strong">
                  {t('ui_quick_presets')}
                </div>
                <div className="tom-grid4">
                  {[
                    { label: t('ui_0s_instant'), sec: 0 },
                    { label: '3s', sec: 3 },
                    { label: '5s', sec: 5 },
                    { label: '10s', sec: 10 },
                    { label: '15s', sec: 15 },
                    { label: '30s', sec: 30 },
                    { label: '60s', sec: 60 },
                    { label: '120s', sec: 120 },
                  ].map((preset) => (
                    <button
                      key={preset.sec}
                      type="button"
                      onClick={() =>
                        setDraft({
                          ...draft,
                          forward_delay_seconds: preset.sec,
                          delay_seconds: preset.sec,
                        })
                      }
                      className={'tom-preset' + ((draft.forward_delay_seconds || draft.delay_seconds || 0) === preset.sec ? ' is-on tom-preset--delay' : '')}
                    >
                      <Clock size={11} aria-hidden="true" /> {preset.label}
                    </button>
                  ))}
                </div>
              </div>
            </div>
          )}

          {/* 7. FORWARD EVERY N VIEW (🔢) */}
          {currentTab === 'every_n' && (
            <div className="tom-view">
              <div className="tom-row">
                <button
                  type="button"
                  onClick={() => setCurrentTab('overview')}
                  className="tom-back"
                >
                  {t('ui_back_to_task_overview')}
                </button>
                <span className="tom-count tom-count--violet">
                  {t('ui_every_n_current')} {everyN}
                </span>
              </div>

              <div
                className="tom-card"
              >
                <div className="tom-title tom-title--sm">
                  {t('ui_configure_every_n')}
                </div>
                <div className="tom-sub tom-mb-4">
                  {t('ui_every_n_help')}
                </div>

                <div className="tom-mb-4">
                  <label className="tom-overline tom-overline--block">
                    {t('ui_every_n_range')}
                  </label>
                  <input
                    type="number"
                    min="1"
                    max="1000"
                    step="1"
                    inputMode="numeric"
                    value={everyN}
                    onChange={(e) => setEveryN(parseInt(e.target.value, 10))}
                    className="tom-input tom-input--big"
                  />
                </div>

                <div className="tom-overline tom-overline--strong">
                  {t('ui_quick_presets')}
                </div>
                <div className="tom-grid4">
                  {[
                    { label: t('ui_every_n_every_message'), n: 1 },
                    { label: '2', n: 2 },
                    { label: '3', n: 3 },
                    { label: '5', n: 5 },
                    { label: '10', n: 10 },
                    { label: '25', n: 25 },
                    { label: '50', n: 50 },
                    { label: '100', n: 100 },
                  ].map((preset) => (
                    <button
                      key={preset.n}
                      type="button"
                      onClick={() => setEveryN(preset.n)}
                      className={'tom-preset' + (everyN === preset.n ? ' is-on tom-preset--n' : '')}
                    >
                      <Hash size={11} aria-hidden="true" /> {preset.label}
                    </button>
                  ))}
                </div>

                <div className="tom-sub tom-mt-3">
                  {t('ui_every_n_example')}
                </div>
              </div>
            </div>
          )}
        </div>

        {/* Bottom Action Footer */}
        <div
          className="tom-footer"
        >
          <button
            type="button"
            onClick={onClose}
            className="btn tom-footerbtn"
          >
            {t('ui_cancel')}
          </button>
          {currentTab === 'logs' && <TaskLogsPanel ruleId={draft.id} lang={lang} />}

          {currentTab === 'control' && (
            <TaskControlPanel
              rule={draft}
              lang={lang}
              onRulePatch={(patch) => setDraft((prev) => ({ ...prev, ...patch }))}
            />
          )}

          <button
            type="button"
            onClick={handleSaveAndClose}
            disabled={saving}
            className="btn btn--primary tom-footerbtn"
          >
            {saving ? t('ui_saving') : t('ui_save_changes')}
          </button>
        </div>
      </div>
    </div>
  )
}
