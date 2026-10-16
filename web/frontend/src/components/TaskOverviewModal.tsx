import { useState, useEffect } from 'react'
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
} from 'lucide-react'
import type { ForwardRule } from '../types'
import { TaskControlPanel } from './TaskControlPanel'
import { TaskLogsPanel } from './TaskLogsPanel'

interface Props {
  rule: ForwardRule | null
  isOpen: boolean
  onClose: () => void
  onSave: (updated: ForwardRule) => Promise<void>
  onDelete?: (id: string) => Promise<void>
  lang?: 'fa' | 'en'
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
type FeatureGroupType = 'all' | 'crypto' | 'mmo' | 'content'

export function TaskOverviewModal({
  rule,
  isOpen,
  onClose,
  onSave,
  onDelete,
  lang = 'en',
}: Props) {
  if (!isOpen || !rule) return null

  const isRtl = lang === 'fa'
  const [currentTab, setCurrentTab] = useState<TabType>('overview')
  const [activeGroup, setActiveGroup] = useState<FeatureGroupType>('all')
  const [draft, setDraft] = useState<ForwardRule>({ ...rule })
  const [saving, setSaving] = useState(false)
  const [isEditingName, setIsEditingName] = useState(false)
  const [showTopicPrompt, setShowTopicPrompt] = useState(false)

  // Subview input states for v2.0 cards
  const [newReplaceFrom, setNewReplaceFrom] = useState('')
  const [newReplaceTo, setNewReplaceTo] = useState('')
  const [newBlWord, setNewBlWord] = useState('')
  const [newWlWord, setNewWlWord] = useState('')
  const [newCleanerPattern, setNewCleanerPattern] = useState('')

  const listPatterns = (val: any): string[] => {
    if (!val) return []
    if (Array.isArray(val)) return val
    return []
  }

  useEffect(() => {
    if (rule) {
      setDraft({ ...rule })
      setCurrentTab('overview')
    }
  }, [rule])

  const handleToggle = (field: keyof ForwardRule) => {
    setDraft((prev) => ({
      ...prev,
      [field]: !prev[field],
    }))
  }

  const handleAddonToggle = (addonKey: string) => {
    setDraft((prev) => {
      const currentAddons = { ...(prev.paid_addons || {}) }
      currentAddons[addonKey] = !currentAddons[addonKey]
      return {
        ...prev,
        paid_addons: currentAddons,
      }
    })
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

  const allAddonsCatalog = [
    { key: 'channel_comments', title: 'Channel Comments', desc: 'Forward and sync discussion replies', locked: false },
    { key: 'every_n', title: 'Forward Every N Messages', desc: 'Batch and sample messages rate-limit', locked: true },
    { key: 'kw_confirm', title: 'Keyword Confirmation', desc: 'Require approval when trigger words match', locked: true },
    { key: 'kw_reply', title: 'Keyword Reply Thread', desc: 'Auto-reply in topic threads', locked: true },
    { key: 'auto_del', title: 'Auto Delete Messages', desc: 'Self-destruct forwarded messages after TTL', locked: true },
    { key: 'webhook', title: 'Outbound Webhooks', desc: 'Dispatch HTTP webhook payload on message', locked: true },
    { key: 'crypto', title: 'Crypto Signal Mode', desc: 'Clean, format and parse trade signals', locked: true },
    { key: 'affiliate', title: 'Affiliate Kit', desc: 'Swap referral links and IDs dynamically', locked: true },
    { key: 'link_buttons', title: 'Link Buttons', desc: 'Inject custom interactive inline buttons', locked: false },
    { key: 'watermark', title: 'Watermark Protection', desc: 'Brand images and videos with logo/text', locked: true },
    { key: 'duplicate', title: 'Duplicate Filters', desc: 'Deep fingerprint media & text deduplication', locked: false },
    { key: 'cleaner', title: 'Cleaner Protocol', desc: 'Strip links, tags, phones and emojis', locked: false },
    { key: 'scheduler', title: 'Auto Post Scheduler', desc: 'Queue messages and post on schedule', locked: false },
    { key: 'power_schedule', title: 'Power On/Off Schedule', desc: 'Limit forwarding during business hours', locked: false },
    { key: 'raw_text', title: 'Process Raw Text', desc: 'Normalize formatting, entities, and styles', locked: false },
    { key: 'button_to_text', title: 'Button To Text', desc: 'Convert inline keyboard buttons to hyperlinks', locked: false },
    { key: 'ocr_ai', title: 'OCR Image AI', desc: 'Extract text from pictures via vision AI', locked: false },
    { key: 'ai_rewrite', title: 'Smart AI Rewrite', desc: 'Rephrase tone and language via LLM', locked: false },
  ]

  const taskName = draft.name || `af_route_lab_${draft.id.slice(0, 4)}`
  const sourceName = draft.source_chat_name || draft.source_chat_id || 'AF Route Lab Source'
  const targetName = draft.target_chat_name || draft.target_chat_id || 'AF Route Lab Target'
  const isRunning = draft.is_active
  const debugOn = draft.debug_mode ?? false

  return (
    <div
      style={{
        position: 'fixed',
        inset: 0,
        backgroundColor: 'rgba(0,0,0,0.75)',
        backdropFilter: 'blur(6px)',
        zIndex: 9999,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: '16px',
        direction: isRtl ? 'rtl' : 'ltr',
      }}
    >
      <div
        style={{
          width: '100%',
          maxWidth: '480px',
          maxHeight: '92vh',
          backgroundColor: '#111827',
          color: '#f9fafb',
          borderRadius: '20px',
          boxShadow: '0 25px 50px -12px rgba(0, 0, 0, 0.6)',
          display: 'flex',
          flexDirection: 'column',
          overflow: 'hidden',
          border: '1px solid #1f2937',
        }}
      >
        {/* Top Header matching reference screens */}
        <div
          style={{
            padding: '14px 18px',
            borderBottom: '1px solid #1f2937',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            backgroundColor: '#131b2e',
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
            {currentTab !== 'overview' && (
              <button
                type="button"
                onClick={() => setCurrentTab('overview')}
                style={{
                  background: '#1f2937',
                  border: 'none',
                  color: '#9ca3af',
                  borderRadius: '8px',
                  padding: '6px 10px',
                  cursor: 'pointer',
                  fontSize: '13px',
                }}
              >
                ← Back
              </button>
            )}
            {isEditingName ? (
              <input
                type="text"
                value={draft.name || ''}
                onChange={(e) => setDraft({ ...draft, name: e.target.value })}
                onBlur={() => setIsEditingName(false)}
                autoFocus
                style={{
                  background: '#1f2937',
                  border: '1px solid #3b82f6',
                  color: '#fff',
                  borderRadius: '6px',
                  padding: '4px 8px',
                  fontSize: '15px',
                  fontWeight: 600,
                }}
              />
            ) : (
              <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                <span style={{ fontSize: '16px', fontWeight: 700, color: '#f3f4f6' }}>
                  {taskName}
                </span>
                <button
                  type="button"
                  onClick={() => setIsEditingName(true)}
                  style={{ background: 'transparent', border: 'none', color: '#9ca3af', cursor: 'pointer', padding: 2 }}
                >
                  <Edit2 size={14} />
                </button>
              </div>
            )}
          </div>

          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            {onDelete && (
              <button
                type="button"
                onClick={() => onDelete(draft.id)}
                title="Delete Task"
                style={{
                  background: 'transparent',
                  border: 'none',
                  color: '#ef4444',
                  cursor: 'pointer',
                  padding: '6px',
                }}
              >
                <Trash2 size={18} />
              </button>
            )}
            <button
              type="button"
              onClick={onClose}
              style={{
                background: '#1f2937',
                border: 'none',
                color: '#9ca3af',
                borderRadius: '8px',
                padding: '6px',
                cursor: 'pointer',
              }}
            >
              <X size={18} />
            </button>
          </div>
        </div>

        {/* Scrollable Modal Content */}
        <div style={{ flex: 1, overflowY: 'auto', padding: '16px' }}>
          {currentTab === 'overview' && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
              {/* Task Overview Card (Screens 1 & 5) */}
              <div
                style={{
                  backgroundColor: '#1e293b',
                  borderRadius: '16px',
                  padding: '16px',
                  border: '1px solid #334155',
                }}
              >
                <div
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    marginBottom: '14px',
                  }}
                >
                  <span style={{ fontSize: '13px', color: '#94a3b8', fontWeight: 600 }}>Task Overview</span>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                    <span style={{ fontSize: '12px', color: '#94a3b8' }}>Forwarding Status</span>
                    <button
                      type="button"
                      onClick={() => handleToggle('is_active')}
                      style={{
                        padding: '4px 12px',
                        borderRadius: '20px',
                        border: 'none',
                        fontWeight: 700,
                        fontSize: '11px',
                        cursor: 'pointer',
                        backgroundColor: isRunning ? '#10b981' : '#475569',
                        color: '#ffffff',
                        display: 'flex',
                        alignItems: 'center',
                        gap: '4px',
                        transition: 'all 0.2s',
                      }}
                    >
                      {isRunning ? <Play size={10} fill="#fff" /> : <Pause size={10} fill="#fff" />}
                      {isRunning ? 'RUNNING' : 'PAUSED'}
                    </button>
                  </div>
                </div>

                {/* Route Visualizer */}
                <div
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    backgroundColor: '#0f172a',
                    padding: '12px 14px',
                    borderRadius: '12px',
                    marginBottom: '14px',
                  }}
                >
                  <div style={{ flex: 1 }}>
                    <div style={{ fontSize: '11px', color: '#64748b', marginBottom: '2px' }}>Source</div>
                    <div style={{ fontSize: '14px', fontWeight: 600, color: '#e2e8f0' }}>{sourceName}</div>
                    <div style={{ fontSize: '11px', color: '#38bdf8' }}>Telegram</div>
                  </div>
                  <ArrowRight size={18} color="#64748b" style={{ margin: '0 10px' }} />
                  <div style={{ flex: 1, textAlign: 'right' }}>
                    <div style={{ fontSize: '11px', color: '#64748b', marginBottom: '2px' }}>Target</div>
                    <div style={{ fontSize: '14px', fontWeight: 600, color: '#e2e8f0' }}>{targetName}</div>
                    <div style={{ fontSize: '11px', color: '#38bdf8' }}>Telegram</div>
                  </div>
                </div>

                {/* Show Forward Header row (v2.0 app update matching screenshot) */}
                <div
                  style={{
                    display: 'flex',
                    flexDirection: 'column',
                    gap: '4px',
                    padding: '8px 0',
                    borderTop: '1px solid #334155',
                  }}
                >
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                    <span style={{ fontSize: '12px', color: '#cbd5e1' }}>Show Forward Header</span>
                    <button
                      type="button"
                      onClick={() => handleToggle('show_forward_header')}
                      style={{
                        background: draft.show_forward_header ? '#22c55e' : '#334155',
                        border: 'none',
                        borderRadius: '12px',
                        padding: '2px 10px',
                        color: '#fff',
                        fontSize: '11px',
                        fontWeight: 700,
                        cursor: 'pointer',
                      }}
                    >
                      {draft.show_forward_header ? 'Yes' : 'No'}
                    </button>
                  </div>
                  {draft.show_forward_header && (
                    <div style={{ fontSize: '10px', color: '#ef4444', fontStyle: 'italic' }}>
                      *Reply, Edit, Replace, Translate will not work when show header
                    </div>
                  )}
                </div>

                {/* Debug Mode row */}
                <div
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    paddingTop: '8px',
                    borderTop: '1px solid #334155',
                  }}
                >
                  <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                    <Bug size={14} color="#94a3b8" />
                    <span style={{ fontSize: '12px', color: '#94a3b8' }}>Debug mode</span>
                  </div>
                  <button
                    type="button"
                    onClick={() => handleToggle('debug_mode')}
                    style={{
                      background: debugOn ? '#22c55e' : '#334155',
                      border: 'none',
                      borderRadius: '12px',
                      padding: '2px 10px',
                      color: '#fff',
                      fontSize: '11px',
                      fontWeight: 600,
                      cursor: 'pointer',
                    }}
                  >
                    {debugOn ? 'on' : 'off'}
                  </button>
                </div>
              </div>

              {/* Sender Type Card */}
              <div
                style={{
                  backgroundColor: '#1e293b',
                  borderRadius: '14px',
                  padding: '12px 16px',
                  border: '1px solid #334155',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'space-between',
                }}
              >
                <div>
                  <div style={{ fontSize: '11px', color: '#94a3b8' }}>Sender Type</div>
                  <div style={{ fontSize: '13px', fontWeight: 600, color: '#f1f5f9' }}>
                    {draft.sender_type || 'Personal Telegram Account'}
                  </div>
                </div>
                <select
                  value={draft.sender_type || 'Personal Telegram Account'}
                  onChange={(e) => setDraft({ ...draft, sender_type: e.target.value })}
                  style={{
                    backgroundColor: '#0f172a',
                    color: '#e2e8f0',
                    border: '1px solid #475569',
                    borderRadius: '8px',
                    padding: '6px 10px',
                    fontSize: '12px',
                  }}
                >
                  <option value="Personal Telegram Account">Personal Telegram Account</option>
                  <option value="Bot Account">Bot Account</option>
                  <option value="MTProto Premium Userbot">MTProto Premium Userbot</option>
                </select>
              </div>

              {/* Edit Task Section: 4 Distinct Action Buttons (Screen 1 & 5) */}
              <div>
                <div style={{ fontSize: '12px', color: '#94a3b8', fontWeight: 600, marginBottom: '8px' }}>
                  Edit Task
                </div>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '8px', marginBottom: '8px' }}>
                  {/* Green Button: Edit Source */}
                  <button
                    type="button"
                    onClick={() => {
                      const newSrc = prompt('Enter Source Chat ID or Title:', draft.source_chat_id)
                      if (newSrc) setDraft({ ...draft, source_chat_id: newSrc, source_chat_name: newSrc })
                    }}
                    style={{
                      backgroundColor: '#16a34a',
                      color: '#ffffff',
                      border: 'none',
                      borderRadius: '12px',
                      padding: '12px',
                      fontWeight: 600,
                      fontSize: '13px',
                      cursor: 'pointer',
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      gap: '6px',
                      boxShadow: '0 4px 6px -1px rgba(22, 163, 74, 0.3)',
                    }}
                  >
                    Edit Source
                  </button>

                  {/* Red Button: Edit Target */}
                  <button
                    type="button"
                    onClick={() => {
                      const newDst = prompt('Enter Target Chat ID or Title:', draft.target_chat_id)
                      if (newDst) setDraft({ ...draft, target_chat_id: newDst, target_chat_name: newDst })
                    }}
                    style={{
                      backgroundColor: '#dc2626',
                      color: '#ffffff',
                      border: 'none',
                      borderRadius: '12px',
                      padding: '12px',
                      fontWeight: 600,
                      fontSize: '13px',
                      cursor: 'pointer',
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      gap: '6px',
                      boxShadow: '0 4px 6px -1px rgba(220, 38, 38, 0.3)',
                    }}
                  >
                    Edit Target
                  </button>
                </div>

                {/* Blue Button: Set up Forwarding Topic */}
                <button
                  type="button"
                  onClick={() => setShowTopicPrompt(!showTopicPrompt)}
                  style={{
                    width: '100%',
                    backgroundColor: '#2563eb',
                    color: '#ffffff',
                    border: 'none',
                    borderRadius: '12px',
                    padding: '12px',
                    fontWeight: 600,
                    fontSize: '13px',
                    cursor: 'pointer',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    gap: '6px',
                    marginBottom: '8px',
                    boxShadow: '0 4px 6px -1px rgba(37, 99, 235, 0.3)',
                  }}
                >
                  <Hash size={14} />
                  Set up Forwarding Topic {draft.topic_id ? `(#${draft.topic_id})` : ''}
                </button>

                {showTopicPrompt && (
                  <div
                    style={{
                      backgroundColor: '#0f172a',
                      padding: '10px',
                      borderRadius: '10px',
                      marginBottom: '8px',
                      display: 'flex',
                      gap: '8px',
                    }}
                  >
                    <input
                      type="number"
                      placeholder="Topic Thread ID (0 to remove)"
                      value={draft.topic_id || ''}
                      onChange={(e) => setDraft({ ...draft, topic_id: e.target.value })}
                      style={{
                        flex: 1,
                        background: '#1e293b',
                        border: '1px solid #475569',
                        color: '#fff',
                        borderRadius: '6px',
                        padding: '6px 10px',
                        fontSize: '13px',
                      }}
                    />
                    <button
                      type="button"
                      onClick={() => setShowTopicPrompt(false)}
                      style={{
                        background: '#3b82f6',
                        color: '#fff',
                        border: 'none',
                        borderRadius: '6px',
                        padding: '0 12px',
                        fontSize: '12px',
                        fontWeight: 600,
                      }}
                    >
                      Done
                    </button>
                  </div>
                )}

                {/* Dark Outline Button: Advanced Settings */}
                <button
                  type="button"
                  onClick={() => setCurrentTab('advanced')}
                  style={{
                    width: '100%',
                    backgroundColor: '#1f2937',
                    color: '#f9fafb',
                    border: '1px solid #374151',
                    borderRadius: '12px',
                    padding: '12px',
                    fontWeight: 600,
                    fontSize: '13px',
                    cursor: 'pointer',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    gap: '6px',
                  }}
                >
                  <SlidersHorizontal size={14} />
                  Advanced Settings
                </button>

                {/* Full per-task control: multi-source + every setting the
                    server knows about, rendered from its schema. */}
                <button
                  type="button"
                  onClick={() => setCurrentTab('control')}
                  style={{
                    width: '100%',
                    backgroundColor: '#1f2937',
                    color: '#f9fafb',
                    border: '1px solid #374151',
                    borderRadius: '12px',
                    padding: '12px',
                    fontWeight: 600,
                    fontSize: '13px',
                    cursor: 'pointer',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    gap: '6px',
                  }}
                >
                  <SlidersHorizontal size={14} />
                  Full Control & Sources
                </button>

                {/* This task's complete history: paged, searchable, exportable. */}
                <button
                  type="button"
                  onClick={() => setCurrentTab('logs')}
                  style={{
                    width: '100%',
                    backgroundColor: '#1f2937',
                    color: '#f9fafb',
                    border: '1px solid #374151',
                    borderRadius: '12px',
                    padding: '12px',
                    fontWeight: 600,
                    fontSize: '13px',
                    cursor: 'pointer',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    gap: '6px',
                    marginTop: '8px',
                  }}
                >
                  <ScrollText size={14} />
                  Task Logs
                </button>
              </div>

              {/* v2.0 App Update — Features Section matching mobile screenshot */}
              <div
                style={{
                  backgroundColor: '#1e293b',
                  borderRadius: '16px',
                  padding: '14px',
                  border: '1px solid #334155',
                }}
              >
                <div
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    marginBottom: '12px',
                  }}
                >
                  <span style={{ fontSize: '14px', fontWeight: 700, color: '#f1f5f9' }}>Features</span>
                  <span style={{ fontSize: '11px', color: '#64748b', cursor: 'pointer', display: 'flex', alignItems: 'center', gap: '2px' }}>
                    Explain &gt;
                  </span>
                </div>

                {/* 5 Toggle Pills matching screenshot (top half of Features) */}
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: '8px', marginBottom: '14px' }}>
                  {[
                    { key: 'sync_edits' as const, label: 'Edit', active: Boolean(draft.sync_edits ?? true) },
                    { key: 'sync_deletes' as const, label: 'Delete', active: Boolean(draft.sync_deletes ?? true) },
                    { key: 'sync_replies' as const, label: 'Reply', active: Boolean(draft.sync_replies ?? true) },
                    { key: 'show_forward_header' as const, label: 'Show Forward Header', active: Boolean(draft.show_forward_header) },
                    { key: 'duplicate_filters' as const, label: 'Duplicate Filters', active: Boolean(draft.duplicate_filters ?? true) },
                  ].map((item) => (
                    <button
                      key={item.key}
                      type="button"
                      onClick={() => handleToggle(item.key)}
                      style={{
                        backgroundColor: item.active ? '#dcfce7' : '#334155',
                        color: item.active ? '#166534' : '#94a3b8',
                        border: 'none',
                        borderRadius: '20px',
                        padding: '6px 14px',
                        fontSize: '12px',
                        fontWeight: 600,
                        cursor: 'pointer',
                        display: 'flex',
                        alignItems: 'center',
                        gap: '6px',
                        transition: 'all 0.15s ease',
                      }}
                    >
                      <span>{item.active ? '✔' : '○'}</span>
                      <span>{item.label}</span>
                    </button>
                  ))}
                </div>

                {/* 6 Modular Option Cards (2x3 grid) matching screenshot */}
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '8px' }}>
                  {[
                    {
                      title: 'Replace',
                      icon: '< >',
                      iconColor: '#a855f7',
                      status: `Activated: ${Object.keys((draft as any).replacements || {}).length}`,
                      onClick: () => setCurrentTab('replace'),
                    },
                    {
                      title: 'Blacklist',
                      icon: '≡ₓ',
                      iconColor: '#ef4444',
                      status: `Activated: ${(draft.blacklist_keywords || []).length}`,
                      onClick: () => setCurrentTab('blacklist'),
                    },
                    {
                      title: 'Whitelist',
                      icon: '≡✓',
                      iconColor: '#22c55e',
                      status: `Activated: ${(draft.whitelist_keywords || []).length}`,
                      onClick: () => setCurrentTab('whitelist'),
                    },
                    {
                      title: 'Cleaner',
                      icon: '∷',
                      iconColor: '#f97316',
                      status: `Activated: ${draft.cleaner_enabled ? '1' : '0'}`,
                      onClick: () => setCurrentTab('cleaner'),
                    },
                    {
                      title: 'Filters',
                      icon: '⊶',
                      iconColor: '#3b82f6',
                      status: `Activated: ${((draft.duplicate_filters ? 1 : 0) + [draft.block_text, draft.block_links, draft.block_voice, draft.block_stickers].filter(Boolean).length)}`,
                      onClick: () => setCurrentTab('filters'),
                    },
                    {
                      title: 'Set Delay',
                      icon: '⏱',
                      iconColor: '#eab308',
                      status: `Delay Current: ${draft.forward_delay_seconds || draft.delay_seconds || 0}s`,
                      onClick: () => setCurrentTab('delay'),
                    },
                  ].map((card) => (
                    <div
                      key={card.title}
                      onClick={card.onClick}
                      style={{
                        backgroundColor: '#0f172a',
                        border: '1px solid #334155',
                        borderRadius: '10px',
                        padding: '10px 12px',
                        cursor: 'pointer',
                        transition: 'border-color 0.15s',
                      }}
                    >
                      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '4px' }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                          <span style={{ color: card.iconColor, fontWeight: 700, fontSize: '13px' }}>{card.icon}</span>
                          <span style={{ fontSize: '12px', fontWeight: 600, color: '#f1f5f9' }}>{card.title} &gt;</span>
                        </div>
                      </div>
                      <div style={{ fontSize: '10px', color: '#94a3b8' }}>{card.status}</div>
                    </div>
                  ))}
                </div>
              </div>

              {/* Feature Groups Filter Chips */}
              <div>
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '8px' }}>
                  <span style={{ fontSize: '12px', color: '#94a3b8', fontWeight: 600 }}>Feature groups</span>
                  <span style={{ fontSize: '11px', color: '#64748b' }}>Choose & customize tools</span>
                </div>
                <div style={{ display: 'flex', gap: '6px', overflowX: 'auto', paddingBottom: '4px' }}>
                  {[
                    { id: 'all', label: 'All' },
                    { id: 'crypto', label: '₿ Crypto' },
                    { id: 'mmo', label: 'MMO / Affiliate' },
                    { id: 'content', label: 'Content' },
                  ].map((grp) => (
                    <button
                      key={grp.id}
                      type="button"
                      onClick={() => setActiveGroup(grp.id as FeatureGroupType)}
                      style={{
                        padding: '6px 14px',
                        borderRadius: '20px',
                        border: 'none',
                        fontSize: '12px',
                        fontWeight: 600,
                        cursor: 'pointer',
                        whiteSpace: 'nowrap',
                        backgroundColor: activeGroup === grp.id ? '#3b82f6' : '#1e293b',
                        color: activeGroup === grp.id ? '#ffffff' : '#94a3b8',
                      }}
                    >
                      {grp.label}
                    </button>
                  ))}
                </div>
              </div>

              {/* PAID ADDON Banner & 2x3 Grid (Screen 4) */}
              <div
                style={{
                  backgroundColor: '#1e293b',
                  borderRadius: '16px',
                  padding: '14px',
                  border: '1px solid #334155',
                }}
              >
                <div
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    marginBottom: '12px',
                  }}
                >
                  <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                    <Sparkles size={14} color="#f59e0b" />
                    <span style={{ fontSize: '13px', fontWeight: 700, color: '#f59e0b' }}>PAID ADDON</span>
                    <span title="Unlock premium features with credits" style={{ display: 'inline-flex' }}>
                      <Info size={13} color="#64748b" />
                    </span>
                  </div>
                  <button
                    type="button"
                    onClick={() => setCurrentTab('all_addons')}
                    style={{
                      background: 'transparent',
                      border: 'none',
                      color: '#3b82f6',
                      fontSize: '12px',
                      fontWeight: 600,
                      cursor: 'pointer',
                      display: 'flex',
                      alignItems: 'center',
                      gap: '2px',
                    }}
                  >
                    View all (18) <ChevronRight size={14} />
                  </button>
                </div>

                {/* 2x3 Grid of Addons matching reference Image 4 */}
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '8px' }}>
                  {[
                    { key: 'channel_comments', title: 'Channel Comments', locked: false, badge: 'Addon' },
                    { key: 'every_n', title: 'Forward Every N Messages', locked: true, badge: 'Addon' },
                    { key: 'kw_confirm', title: 'Keyword Confirmation', locked: true, badge: 'Addon' },
                    { key: 'kw_reply', title: 'Keyword Reply Thread', locked: true, badge: 'Addon' },
                    { key: 'auto_del', title: 'Auto Delete Messages', locked: true, badge: 'Addon' },
                    { key: 'webhook', title: 'Outbound Webhooks', locked: true, badge: 'Addon' },
                  ].map((item) => {
                    const isEnabled = draft.paid_addons?.[item.key] ?? false
                    return (
                      <div
                        key={item.key}
                        onClick={() => handleAddonToggle(item.key)}
                        style={{
                          backgroundColor: '#0f172a',
                          borderRadius: '12px',
                          padding: '10px 12px',
                          display: 'flex',
                          flexDirection: 'column',
                          justifyContent: 'space-between',
                          minHeight: '74px',
                          cursor: 'pointer',
                          border: isEnabled ? '1px solid #3b82f6' : '1px solid #1e293b',
                          transition: 'all 0.15s',
                        }}
                      >
                        <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between' }}>
                          <span style={{ fontSize: '11px', color: '#38bdf8', fontWeight: 600 }}>
                            {item.locked && <Lock size={10} style={{ display: 'inline', marginRight: 3 }} />}
                            ★ {item.badge}
                          </span>
                          <span
                            style={{
                              fontSize: '10px',
                              padding: '2px 6px',
                              borderRadius: '8px',
                              backgroundColor: isEnabled ? '#10b981' : '#334155',
                              color: '#fff',
                              fontWeight: 700,
                            }}
                          >
                            {isEnabled ? 'ON' : 'OFF'}
                          </span>
                        </div>
                        <div style={{ fontSize: '12px', fontWeight: 600, color: '#e2e8f0', marginTop: '6px' }}>
                          {item.title}
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
            <div style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
              {/* FILTERS (Screen 2) */}
              <div
                style={{
                  backgroundColor: '#1e293b',
                  borderRadius: '14px',
                  padding: '14px',
                  border: '1px solid #334155',
                }}
              >
                <div style={{ fontSize: '13px', fontWeight: 700, color: '#f3f4f6', marginBottom: '10px', display: 'flex', alignItems: 'center', gap: '6px' }}>
                  <Shield size={14} color="#38bdf8" /> Filters (?)
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                    <span style={{ fontSize: '13px', color: '#cbd5e1' }}>Duplicate Filters</span>
                    <button
                      type="button"
                      onClick={() => handleToggle('duplicate_filters')}
                      style={{
                        background: draft.duplicate_filters ? '#22c55e' : '#475569',
                        border: 'none',
                        borderRadius: '12px',
                        padding: '3px 10px',
                        color: '#fff',
                        fontSize: '11px',
                        fontWeight: 600,
                        cursor: 'pointer',
                      }}
                    >
                      {draft.duplicate_filters ? 'on' : 'off'}
                    </button>
                  </div>

                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                    <span style={{ fontSize: '13px', color: '#cbd5e1' }}>Crypto Mode</span>
                    <button
                      type="button"
                      onClick={() => handleToggle('crypto_mode')}
                      style={{
                        background: draft.crypto_mode ? '#22c55e' : '#475569',
                        border: 'none',
                        borderRadius: '12px',
                        padding: '3px 10px',
                        color: '#fff',
                        fontSize: '11px',
                        fontWeight: 600,
                        cursor: 'pointer',
                      }}
                    >
                      {draft.crypto_mode ? 'on' : 'off'}
                    </button>
                  </div>

                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                    <span style={{ fontSize: '13px', color: '#cbd5e1' }}>Blacklist & Whitelist</span>
                    <span style={{ fontSize: '12px', color: '#94a3b8' }}>
                      Activated: {(draft.domain_blocklist?.length || 0) + (draft.domain_allowlist?.length || 0)}
                    </span>
                  </div>
                </div>
              </div>

              {/* SCHEDULES (Screen 2) */}
              <div
                style={{
                  backgroundColor: '#1e293b',
                  borderRadius: '14px',
                  padding: '14px',
                  border: '1px solid #334155',
                }}
              >
                <div style={{ fontSize: '13px', fontWeight: 700, color: '#f3f4f6', marginBottom: '10px', display: 'flex', alignItems: 'center', gap: '6px' }}>
                  <Clock size={14} color="#f59e0b" /> Schedules (?)
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                    <span style={{ fontSize: '13px', color: '#cbd5e1' }}>Auto Posting</span>
                    <button
                      type="button"
                      onClick={() => handleToggle('auto_posting')}
                      style={{
                        background: draft.auto_posting ? '#22c55e' : '#475569',
                        border: 'none',
                        borderRadius: '12px',
                        padding: '3px 10px',
                        color: '#fff',
                        fontSize: '11px',
                        fontWeight: 600,
                        cursor: 'pointer',
                      }}
                    >
                      {draft.auto_posting ? 'on' : 'off'}
                    </button>
                  </div>

                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                    <span style={{ fontSize: '13px', color: '#cbd5e1' }}>Power Control</span>
                    <button
                      type="button"
                      onClick={() => handleToggle('power_control')}
                      style={{
                        background: draft.power_control ? '#22c55e' : '#475569',
                        border: 'none',
                        borderRadius: '12px',
                        padding: '3px 10px',
                        color: '#fff',
                        fontSize: '11px',
                        fontWeight: 600,
                        cursor: 'pointer',
                      }}
                    >
                      {draft.power_control ? 'on' : 'off'}
                    </button>
                  </div>
                </div>
              </div>

              {/* TIME FORWARD (Screen 2) */}
              <div
                style={{
                  backgroundColor: '#1e293b',
                  borderRadius: '14px',
                  padding: '14px',
                  border: '1px solid #334155',
                }}
              >
                <div style={{ fontSize: '13px', fontWeight: 700, color: '#f3f4f6', marginBottom: '10px', display: 'flex', alignItems: 'center', gap: '6px' }}>
                  <Clock size={14} color="#a855f7" /> Time Forward (?)
                </div>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '10px' }}>
                  <div>
                    <label style={{ fontSize: '11px', color: '#94a3b8', display: 'block', marginBottom: '4px' }}>
                      Delay (seconds)
                    </label>
                    <input
                      type="number"
                      value={draft.delay_seconds || 0}
                      onChange={(e) => setDraft({ ...draft, delay_seconds: parseFloat(e.target.value) || 0 })}
                      style={{
                        width: '100%',
                        backgroundColor: '#0f172a',
                        border: '1px solid #475569',
                        color: '#fff',
                        borderRadius: '8px',
                        padding: '6px 10px',
                        fontSize: '12px',
                      }}
                    />
                  </div>
                  <div>
                    <label style={{ fontSize: '11px', color: '#94a3b8', display: 'block', marginBottom: '4px' }}>
                      Edit Time Limit (s)
                    </label>
                    <input
                      type="number"
                      value={draft.edit_time_limit_seconds || 0}
                      onChange={(e) => setDraft({ ...draft, edit_time_limit_seconds: parseInt(e.target.value) || 0 })}
                      style={{
                        width: '100%',
                        backgroundColor: '#0f172a',
                        border: '1px solid #475569',
                        color: '#fff',
                        borderRadius: '8px',
                        padding: '6px 10px',
                        fontSize: '12px',
                      }}
                    />
                  </div>
                </div>
              </div>

              {/* PROCESSING MESSAGES (Screen 2) */}
              <div
                style={{
                  backgroundColor: '#1e293b',
                  borderRadius: '14px',
                  padding: '14px',
                  border: '1px solid #334155',
                }}
              >
                <div style={{ fontSize: '13px', fontWeight: 700, color: '#f3f4f6', marginBottom: '10px' }}>
                  Processing Messages (?)
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                    <span style={{ fontSize: '13px', color: '#cbd5e1' }}>Process Raw Text</span>
                    <button
                      type="button"
                      onClick={() => handleToggle('process_raw_text')}
                      style={{
                        background: draft.process_raw_text ? '#22c55e' : '#475569',
                        border: 'none',
                        borderRadius: '12px',
                        padding: '3px 10px',
                        color: '#fff',
                        fontSize: '11px',
                        fontWeight: 600,
                        cursor: 'pointer',
                      }}
                    >
                      {draft.process_raw_text ? 'on' : 'off'}
                    </button>
                  </div>
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                    <span style={{ fontSize: '13px', color: '#cbd5e1' }}>Button To Text</span>
                    <button
                      type="button"
                      onClick={() => handleToggle('button_to_text')}
                      style={{
                        background: draft.button_to_text ? '#22c55e' : '#475569',
                        border: 'none',
                        borderRadius: '12px',
                        padding: '3px 10px',
                        color: '#fff',
                        fontSize: '11px',
                        fontWeight: 600,
                        cursor: 'pointer',
                      }}
                    >
                      {draft.button_to_text ? 'on' : 'off'}
                    </button>
                  </div>
                </div>
              </div>

              {/* MODIFY CONTENT (Screen 3) */}
              <div
                style={{
                  backgroundColor: '#1e293b',
                  borderRadius: '14px',
                  padding: '14px',
                  border: '1px solid #334155',
                }}
              >
                <div style={{ fontSize: '13px', fontWeight: 700, color: '#f3f4f6', marginBottom: '10px' }}>
                  Modify Content (?)
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
                  <div>
                    <label style={{ fontSize: '11px', color: '#94a3b8', display: 'block', marginBottom: '4px' }}>
                      Header
                    </label>
                    <input
                      type="text"
                      placeholder="HEADER EMPTY"
                      value={draft.custom_header || ''}
                      onChange={(e) => setDraft({ ...draft, custom_header: e.target.value })}
                      style={{
                        width: '100%',
                        backgroundColor: '#0f172a',
                        border: '1px solid #475569',
                        color: '#fff',
                        borderRadius: '8px',
                        padding: '6px 10px',
                        fontSize: '12px',
                      }}
                    />
                  </div>
                  <div>
                    <label style={{ fontSize: '11px', color: '#94a3b8', display: 'block', marginBottom: '4px' }}>
                      Footer
                    </label>
                    <input
                      type="text"
                      placeholder="FOOTER EMPTY"
                      value={draft.custom_footer || ''}
                      onChange={(e) => setDraft({ ...draft, custom_footer: e.target.value })}
                      style={{
                        width: '100%',
                        backgroundColor: '#0f172a',
                        border: '1px solid #475569',
                        color: '#fff',
                        borderRadius: '8px',
                        padding: '6px 10px',
                        fontSize: '12px',
                      }}
                    />
                  </div>
                  <div>
                    <label style={{ fontSize: '11px', color: '#94a3b8', display: 'block', marginBottom: '4px' }}>
                      Watermark
                    </label>
                    <input
                      type="text"
                      placeholder="Empty"
                      value={draft.watermark_text || ''}
                      onChange={(e) => setDraft({ ...draft, watermark_text: e.target.value })}
                      style={{
                        width: '100%',
                        backgroundColor: '#0f172a',
                        border: '1px solid #475569',
                        color: '#fff',
                        borderRadius: '8px',
                        padding: '6px 10px',
                        fontSize: '12px',
                      }}
                    />
                  </div>
                </div>
              </div>

              {/* AI FEATURES (Screen 3) */}
              <div
                style={{
                  backgroundColor: '#1e293b',
                  borderRadius: '14px',
                  padding: '14px',
                  border: '1px solid #334155',
                }}
              >
                <div
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'space-between',
                    marginBottom: '10px',
                  }}
                >
                  <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                    <Wand2 size={14} color="#818cf8" />
                    <span style={{ fontSize: '13px', fontWeight: 700, color: '#f3f4f6' }}>AI Features</span>
                  </div>
                  <span
                    style={{
                      fontSize: '11px',
                      color: '#a5b4fc',
                      backgroundColor: '#312e81',
                      padding: '2px 8px',
                      borderRadius: '10px',
                    }}
                  >
                    Creativity {draft.ai_creativity ?? 0.7} - Max Tokens {draft.ai_max_tokens ?? 600}
                  </span>
                </div>

                <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                    <span style={{ fontSize: '13px', color: '#cbd5e1' }}>OCR Image AI</span>
                    <button
                      type="button"
                      onClick={() => handleToggle('ocr_image_ai')}
                      style={{
                        background: draft.ocr_image_ai ? '#22c55e' : '#475569',
                        border: 'none',
                        borderRadius: '12px',
                        padding: '3px 10px',
                        color: '#fff',
                        fontSize: '11px',
                        fontWeight: 600,
                        cursor: 'pointer',
                      }}
                    >
                      {draft.ocr_image_ai ? 'on' : 'off'}
                    </button>
                  </div>

                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                    <span style={{ fontSize: '13px', color: '#cbd5e1' }}>Smart AI Rewrite</span>
                    <button
                      type="button"
                      onClick={() => handleToggle('smart_ai_rewrite')}
                      style={{
                        background: draft.smart_ai_rewrite ? '#22c55e' : '#475569',
                        border: 'none',
                        borderRadius: '12px',
                        padding: '3px 10px',
                        color: '#fff',
                        fontSize: '11px',
                        fontWeight: 600,
                        cursor: 'pointer',
                      }}
                    >
                      {draft.smart_ai_rewrite ? 'on' : 'off'}
                    </button>
                  </div>
                </div>
              </div>
            </div>
          )}

          {/* ALL 18 ADDONS CATALOG MODAL VIEW */}
          {currentTab === 'all_addons' && (
            <div>
              <div style={{ fontSize: '13px', color: '#94a3b8', marginBottom: '12px' }}>
                Modular Extensions Catalogue — Toggle features for this task:
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                {allAddonsCatalog.map((item) => {
                  const isEnabled = draft.paid_addons?.[item.key] ?? false
                  return (
                    <div
                      key={item.key}
                      onClick={() => handleAddonToggle(item.key)}
                      style={{
                        backgroundColor: '#1e293b',
                        padding: '12px',
                        borderRadius: '12px',
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'space-between',
                        cursor: 'pointer',
                        border: isEnabled ? '1px solid #3b82f6' : '1px solid #334155',
                      }}
                    >
                      <div>
                        <div style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
                          {item.locked && <Lock size={12} color="#f59e0b" />}
                          <span style={{ fontSize: '13px', fontWeight: 600, color: '#f1f5f9' }}>{item.title}</span>
                        </div>
                        <div style={{ fontSize: '11px', color: '#94a3b8', marginTop: '2px' }}>{item.desc}</div>
                      </div>
                      <span
                        style={{
                          fontSize: '11px',
                          padding: '3px 8px',
                          borderRadius: '12px',
                          backgroundColor: isEnabled ? '#10b981' : '#334155',
                          color: '#fff',
                          fontWeight: 700,
                        }}
                      >
                        {isEnabled ? 'ACTIVE' : 'OFF'}
                      </span>
                    </div>
                  )
                })}
              </div>
            </div>
          )}

          {/* 1. CLEANER ENGINE VIEW (v2.0) */}
          {currentTab === 'cleaner' && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                <button
                  type="button"
                  onClick={() => setCurrentTab('overview')}
                  style={{
                    background: 'none',
                    border: 'none',
                    color: '#38bdf8',
                    fontSize: '12px',
                    cursor: 'pointer',
                    display: 'flex',
                    alignItems: 'center',
                    gap: '4px',
                    padding: 0,
                  }}
                >
                  ← Back to Task Overview
                </button>
                <button
                  type="button"
                  onClick={() => handleToggle('cleaner_enabled')}
                  style={{
                    backgroundColor: draft.cleaner_enabled ? '#16a34a' : '#475569',
                    color: '#fff',
                    border: 'none',
                    borderRadius: '16px',
                    padding: '4px 12px',
                    fontSize: '11px',
                    fontWeight: 700,
                    cursor: 'pointer',
                  }}
                >
                  {draft.cleaner_enabled ? '● Cleaner Active' : '○ Cleaner Inactive'}
                </button>
              </div>

              <div
                style={{
                  backgroundColor: '#1e293b',
                  borderRadius: '14px',
                  padding: '14px',
                  border: '1px solid #334155',
                }}
              >
                <div style={{ fontSize: '13px', fontWeight: 700, color: '#f3f4f6', marginBottom: '4px' }}>
                  Automatic Stripping Rules
                </div>
                <div style={{ fontSize: '11px', color: '#94a3b8', marginBottom: '12px' }}>
                  Cleaned content is stripped before forwarding so sensitive elements never leak to targets.
                </div>

                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '8px' }}>
                  {[
                    { key: 'remove_urls', label: '🔗 Strip URLs' },
                    { key: 'remove_mentions', label: '👤 Strip Mentions (@)' },
                    { key: 'remove_hashtags', label: '#️⃣ Strip Hashtags (#)' },
                    { key: 'remove_emojis', label: '😀 Strip Emojis' },
                    { key: 'apply_to_caption', label: '📝 Clean Captions' },
                    { key: 'case_sensitive', label: '🔤 Case Sensitive' },
                    { key: 'use_regex', label: '⚙️ Regex Engine' },
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
                        style={{
                          backgroundColor: isActive ? '#065f46' : '#0f172a',
                          color: isActive ? '#6ee7b7' : '#94a3b8',
                          border: `1px solid ${isActive ? '#059669' : '#334155'}`,
                          borderRadius: '8px',
                          padding: '8px 10px',
                          fontSize: '12px',
                          fontWeight: 600,
                          cursor: 'pointer',
                          display: 'flex',
                          alignItems: 'center',
                          justifyContent: 'space-between',
                          textAlign: 'left',
                        }}
                      >
                        <span>{opt.label}</span>
                        <span>{isActive ? '✔' : '○'}</span>
                      </button>
                    )
                  })}
                </div>
              </div>

              {/* Custom Patterns to Strip */}
              <div
                style={{
                  backgroundColor: '#1e293b',
                  borderRadius: '14px',
                  padding: '14px',
                  border: '1px solid #334155',
                }}
              >
                <div style={{ fontSize: '13px', fontWeight: 700, color: '#f3f4f6', marginBottom: '8px' }}>
                  Custom Patterns to Strip
                </div>
                <div style={{ display: 'flex', gap: '8px', marginBottom: '10px' }}>
                  <input
                    type="text"
                    placeholder="Enter word, phrase, or ad text..."
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
                    style={{
                      flex: 1,
                      backgroundColor: '#0f172a',
                      border: '1px solid #475569',
                      color: '#fff',
                      borderRadius: '8px',
                      padding: '8px 10px',
                      fontSize: '12px',
                    }}
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
                    style={{
                      backgroundColor: '#f97316',
                      color: '#fff',
                      border: 'none',
                      borderRadius: '8px',
                      padding: '0 14px',
                      fontSize: '12px',
                      fontWeight: 600,
                      cursor: 'pointer',
                    }}
                  >
                    Add
                  </button>
                </div>

                <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px' }}>
                  {listPatterns((draft.cleaner_config as any)?.patterns).map((p, idx) => (
                    <span
                      key={idx}
                      style={{
                        backgroundColor: '#0f172a',
                        border: '1px solid #475569',
                        color: '#f8fafc',
                        borderRadius: '12px',
                        padding: '4px 10px',
                        fontSize: '11px',
                        display: 'flex',
                        alignItems: 'center',
                        gap: '6px',
                      }}
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
                        style={{
                          background: 'none',
                          border: 'none',
                          color: '#ef4444',
                          cursor: 'pointer',
                          padding: 0,
                          fontSize: '12px',
                          fontWeight: 700,
                        }}
                      >
                        ✕
                      </button>
                    </span>
                  ))}
                  {listPatterns((draft.cleaner_config as any)?.patterns).length === 0 && (
                    <span style={{ fontSize: '11px', color: '#64748b' }}>No custom strip patterns added yet.</span>
                  )}
                </div>
              </div>
            </div>
          )}

          {/* 2. TEXT REPLACEMENTS VIEW (< >) */}
          {currentTab === 'replace' && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                <button
                  type="button"
                  onClick={() => setCurrentTab('overview')}
                  style={{
                    background: 'none',
                    border: 'none',
                    color: '#38bdf8',
                    fontSize: '12px',
                    cursor: 'pointer',
                    padding: 0,
                  }}
                >
                  ← Back to Task Overview
                </button>
                <span style={{ fontSize: '12px', color: '#a855f7', fontWeight: 600 }}>
                  Active Rules: {Object.keys((draft as any).replacements || {}).length}
                </span>
              </div>

              <div
                style={{
                  backgroundColor: '#1e293b',
                  borderRadius: '14px',
                  padding: '14px',
                  border: '1px solid #334155',
                }}
              >
                <div style={{ fontSize: '13px', fontWeight: 700, color: '#f3f4f6', marginBottom: '8px' }}>
                  Add Text Replacement
                </div>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 80px', gap: '8px' }}>
                  <input
                    type="text"
                    placeholder="Find text..."
                    value={newReplaceFrom}
                    onChange={(e) => setNewReplaceFrom(e.target.value)}
                    style={{
                      backgroundColor: '#0f172a',
                      border: '1px solid #475569',
                      color: '#fff',
                      borderRadius: '8px',
                      padding: '8px 10px',
                      fontSize: '12px',
                    }}
                  />
                  <input
                    type="text"
                    placeholder="Replace with..."
                    value={newReplaceTo}
                    onChange={(e) => setNewReplaceTo(e.target.value)}
                    style={{
                      backgroundColor: '#0f172a',
                      border: '1px solid #475569',
                      color: '#fff',
                      borderRadius: '8px',
                      padding: '8px 10px',
                      fontSize: '12px',
                    }}
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
                    style={{
                      backgroundColor: '#a855f7',
                      color: '#fff',
                      border: 'none',
                      borderRadius: '8px',
                      fontSize: '12px',
                      fontWeight: 600,
                      cursor: 'pointer',
                    }}
                  >
                    Add
                  </button>
                </div>
              </div>

              {/* Replacements List */}
              <div
                style={{
                  backgroundColor: '#1e293b',
                  borderRadius: '14px',
                  padding: '14px',
                  border: '1px solid #334155',
                }}
              >
                <div style={{ fontSize: '13px', fontWeight: 700, color: '#f3f4f6', marginBottom: '10px' }}>
                  Active Replacements
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                  {Object.entries((draft as any).replacements || {}).map(([from, to], idx) => (
                    <div
                      key={idx}
                      style={{
                        backgroundColor: '#0f172a',
                        borderRadius: '8px',
                        padding: '8px 12px',
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'space-between',
                        border: '1px solid #334155',
                      }}
                    >
                      <div style={{ display: 'flex', alignItems: 'center', gap: '8px', fontSize: '12px' }}>
                        <code style={{ color: '#f87171' }}>{from}</code>
                        <span style={{ color: '#64748b' }}>➔</span>
                        <code style={{ color: '#4ade80' }}>{to as string}</code>
                      </div>
                      <button
                        type="button"
                        onClick={() => {
                          const reps = { ...((draft as any).replacements || {}) }
                          delete reps[from]
                          setDraft({ ...draft, replacements: reps })
                        }}
                        style={{
                          background: 'none',
                          border: 'none',
                          color: '#ef4444',
                          cursor: 'pointer',
                          padding: '2px',
                        }}
                      >
                        <Trash2 size={13} />
                      </button>
                    </div>
                  ))}
                  {Object.keys((draft as any).replacements || {}).length === 0 && (
                    <div style={{ fontSize: '11px', color: '#64748b' }}>No text replacements configured.</div>
                  )}
                </div>
              </div>
            </div>
          )}

          {/* 3. BLACKLIST VIEW (≡ₓ) */}
          {currentTab === 'blacklist' && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                <button
                  type="button"
                  onClick={() => setCurrentTab('overview')}
                  style={{
                    background: 'none',
                    border: 'none',
                    color: '#38bdf8',
                    fontSize: '12px',
                    cursor: 'pointer',
                    padding: 0,
                  }}
                >
                  ← Back to Task Overview
                </button>
                <span style={{ fontSize: '12px', color: '#ef4444', fontWeight: 600 }}>
                  Blacklisted: {(draft.blacklist_keywords || []).length}
                </span>
              </div>

              <div
                style={{
                  backgroundColor: '#1e293b',
                  borderRadius: '14px',
                  padding: '14px',
                  border: '1px solid #334155',
                }}
              >
                <div style={{ fontSize: '13px', fontWeight: 700, color: '#f3f4f6', marginBottom: '8px' }}>
                  Add Blacklisted Keyword or Domain
                </div>
                <div style={{ display: 'flex', gap: '8px' }}>
                  <input
                    type="text"
                    placeholder="Enter banned word or phrase..."
                    value={newBlWord}
                    onChange={(e) => setNewBlWord(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' && newBlWord.trim()) {
                        e.preventDefault()
                        const list = draft.blacklist_keywords || []
                        if (!list.includes(newBlWord.trim())) {
                          setDraft({ ...draft, blacklist_keywords: [...list, newBlWord.trim()] })
                          setNewBlWord('')
                        }
                      }
                    }}
                    style={{
                      flex: 1,
                      backgroundColor: '#0f172a',
                      border: '1px solid #475569',
                      color: '#fff',
                      borderRadius: '8px',
                      padding: '8px 10px',
                      fontSize: '12px',
                    }}
                  />
                  <button
                    type="button"
                    onClick={() => {
                      if (!newBlWord.trim()) return
                      const list = draft.blacklist_keywords || []
                      if (!list.includes(newBlWord.trim())) {
                        setDraft({ ...draft, blacklist_keywords: [...list, newBlWord.trim()] })
                        setNewBlWord('')
                      }
                    }}
                    style={{
                      backgroundColor: '#ef4444',
                      color: '#fff',
                      border: 'none',
                      borderRadius: '8px',
                      padding: '0 14px',
                      fontSize: '12px',
                      fontWeight: 600,
                      cursor: 'pointer',
                    }}
                  >
                    Add
                  </button>
                </div>
              </div>

              <div
                style={{
                  backgroundColor: '#1e293b',
                  borderRadius: '14px',
                  padding: '14px',
                  border: '1px solid #334155',
                }}
              >
                <div style={{ fontSize: '13px', fontWeight: 700, color: '#f3f4f6', marginBottom: '10px' }}>
                  Blocked Words
                </div>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px' }}>
                  {(draft.blacklist_keywords || []).map((w, idx) => (
                    <span
                      key={idx}
                      style={{
                        backgroundColor: '#450a0a',
                        border: '1px solid #991b1b',
                        color: '#fca5a5',
                        borderRadius: '12px',
                        padding: '4px 10px',
                        fontSize: '11px',
                        display: 'flex',
                        alignItems: 'center',
                        gap: '6px',
                      }}
                    >
                      <span>{w}</span>
                      <button
                        type="button"
                        onClick={() => {
                          const list = (draft.blacklist_keywords || []).filter((_, i) => i !== idx)
                          setDraft({ ...draft, blacklist_keywords: list })
                        }}
                        style={{
                          background: 'none',
                          border: 'none',
                          color: '#f87171',
                          cursor: 'pointer',
                          padding: 0,
                          fontSize: '12px',
                          fontWeight: 700,
                        }}
                      >
                        ✕
                      </button>
                    </span>
                  ))}
                  {(draft.blacklist_keywords || []).length === 0 && (
                    <span style={{ fontSize: '11px', color: '#64748b' }}>No words in blacklist.</span>
                  )}
                </div>
              </div>
            </div>
          )}

          {/* 4. WHITELIST VIEW (≡✓) */}
          {currentTab === 'whitelist' && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                <button
                  type="button"
                  onClick={() => setCurrentTab('overview')}
                  style={{
                    background: 'none',
                    border: 'none',
                    color: '#38bdf8',
                    fontSize: '12px',
                    cursor: 'pointer',
                    padding: 0,
                  }}
                >
                  ← Back to Task Overview
                </button>
                <span style={{ fontSize: '12px', color: '#22c55e', fontWeight: 600 }}>
                  Whitelisted: {(draft.whitelist_keywords || []).length}
                </span>
              </div>

              <div
                style={{
                  backgroundColor: '#1e293b',
                  borderRadius: '14px',
                  padding: '14px',
                  border: '1px solid #334155',
                }}
              >
                <div style={{ fontSize: '13px', fontWeight: 700, color: '#f3f4f6', marginBottom: '8px' }}>
                  Add Required Keyword or Domain
                </div>
                <div style={{ display: 'flex', gap: '8px' }}>
                  <input
                    type="text"
                    placeholder="Enter required word or phrase..."
                    value={newWlWord}
                    onChange={(e) => setNewWlWord(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' && newWlWord.trim()) {
                        e.preventDefault()
                        const list = draft.whitelist_keywords || []
                        if (!list.includes(newWlWord.trim())) {
                          setDraft({ ...draft, whitelist_keywords: [...list, newWlWord.trim()] })
                          setNewWlWord('')
                        }
                      }
                    }}
                    style={{
                      flex: 1,
                      backgroundColor: '#0f172a',
                      border: '1px solid #475569',
                      color: '#fff',
                      borderRadius: '8px',
                      padding: '8px 10px',
                      fontSize: '12px',
                    }}
                  />
                  <button
                    type="button"
                    onClick={() => {
                      if (!newWlWord.trim()) return
                      const list = draft.whitelist_keywords || []
                      if (!list.includes(newWlWord.trim())) {
                        setDraft({ ...draft, whitelist_keywords: [...list, newWlWord.trim()] })
                        setNewWlWord('')
                      }
                    }}
                    style={{
                      backgroundColor: '#22c55e',
                      color: '#fff',
                      border: 'none',
                      borderRadius: '8px',
                      padding: '0 14px',
                      fontSize: '12px',
                      fontWeight: 600,
                      cursor: 'pointer',
                    }}
                  >
                    Add
                  </button>
                </div>
              </div>

              <div
                style={{
                  backgroundColor: '#1e293b',
                  borderRadius: '14px',
                  padding: '14px',
                  border: '1px solid #334155',
                }}
              >
                <div style={{ fontSize: '13px', fontWeight: 700, color: '#f3f4f6', marginBottom: '10px' }}>
                  Required Whitelist Words
                </div>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px' }}>
                  {(draft.whitelist_keywords || []).map((w, idx) => (
                    <span
                      key={idx}
                      style={{
                        backgroundColor: '#052e16',
                        border: '1px solid #15803d',
                        color: '#86efac',
                        borderRadius: '12px',
                        padding: '4px 10px',
                        fontSize: '11px',
                        display: 'flex',
                        alignItems: 'center',
                        gap: '6px',
                      }}
                    >
                      <span>{w}</span>
                      <button
                        type="button"
                        onClick={() => {
                          const list = (draft.whitelist_keywords || []).filter((_, i) => i !== idx)
                          setDraft({ ...draft, whitelist_keywords: list })
                        }}
                        style={{
                          background: 'none',
                          border: 'none',
                          color: '#4ade80',
                          cursor: 'pointer',
                          padding: 0,
                          fontSize: '12px',
                          fontWeight: 700,
                        }}
                      >
                        ✕
                      </button>
                    </span>
                  ))}
                  {(draft.whitelist_keywords || []).length === 0 && (
                    <span style={{ fontSize: '11px', color: '#64748b' }}>No whitelist restrictions (all permitted).</span>
                  )}
                </div>
              </div>
            </div>
          )}

          {/* 5. FILTERS VIEW (⊶) */}
          {currentTab === 'filters' && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                <button
                  type="button"
                  onClick={() => setCurrentTab('overview')}
                  style={{
                    background: 'none',
                    border: 'none',
                    color: '#38bdf8',
                    fontSize: '12px',
                    cursor: 'pointer',
                    padding: 0,
                  }}
                >
                  ← Back to Task Overview
                </button>
                <span style={{ fontSize: '12px', color: '#3b82f6', fontWeight: 600 }}>Media & Type Filters</span>
              </div>

              <div
                style={{
                  backgroundColor: '#1e293b',
                  borderRadius: '14px',
                  padding: '14px',
                  border: '1px solid #334155',
                }}
              >
                <div style={{ fontSize: '13px', fontWeight: 700, color: '#f3f4f6', marginBottom: '8px' }}>
                  Filter Message Types
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                  {[
                    { key: 'duplicate_filters' as const, label: '🔀 Duplicate Filters (SHA-256 Fingerprint)', active: Boolean(draft.duplicate_filters) },
                    { key: 'block_text' as const, label: '📝 Block Text Messages', active: Boolean(draft.block_text) },
                    { key: 'block_voice' as const, label: '🎙 Block Voice Messages', active: Boolean(draft.block_voice) },
                    { key: 'block_stickers' as const, label: '🎭 Block Stickers', active: Boolean(draft.block_stickers) },
                    { key: 'block_links' as const, label: '🔗 Block Links', active: Boolean(draft.block_links) },
                    { key: 'block_video' as const, label: '🎬 Block Video Messages', active: Boolean(draft.block_video) },
                    { key: 'block_audio' as const, label: '🎵 Block Audio Files', active: Boolean(draft.block_audio) },
                  ].map((f) => (
                    <div
                      key={f.key}
                      style={{
                        backgroundColor: '#0f172a',
                        borderRadius: '8px',
                        padding: '10px 12px',
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'space-between',
                        border: '1px solid #334155',
                      }}
                    >
                      <span style={{ fontSize: '12px', color: '#e2e8f0', fontWeight: 500 }}>{f.label}</span>
                      <button
                        type="button"
                        onClick={() => handleToggle(f.key)}
                        style={{
                          backgroundColor: f.active ? '#ef4444' : '#334155',
                          color: '#fff',
                          border: 'none',
                          borderRadius: '12px',
                          padding: '3px 10px',
                          fontSize: '11px',
                          fontWeight: 700,
                          cursor: 'pointer',
                        }}
                      >
                        {f.active ? 'BLOCKED' : 'ALLOWED'}
                      </button>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          )}

          {/* 6. SET DELAY VIEW (⏱) */}
          {currentTab === 'delay' && (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                <button
                  type="button"
                  onClick={() => setCurrentTab('overview')}
                  style={{
                    background: 'none',
                    border: 'none',
                    color: '#38bdf8',
                    fontSize: '12px',
                    cursor: 'pointer',
                    padding: 0,
                  }}
                >
                  ← Back to Task Overview
                </button>
                <span style={{ fontSize: '12px', color: '#eab308', fontWeight: 600 }}>
                  Current Delay: {draft.forward_delay_seconds || draft.delay_seconds || 0}s
                </span>
              </div>

              <div
                style={{
                  backgroundColor: '#1e293b',
                  borderRadius: '14px',
                  padding: '14px',
                  border: '1px solid #334155',
                }}
              >
                <div style={{ fontSize: '13px', fontWeight: 700, color: '#f3f4f6', marginBottom: '6px' }}>
                  Configure Forward Delay
                </div>
                <div style={{ fontSize: '11px', color: '#94a3b8', marginBottom: '14px' }}>
                  Adds an asynchronous pause before forwarding each message to prevent Telegram rate limits and simulate natural human posting.
                </div>

                <div style={{ marginBottom: '16px' }}>
                  <label style={{ fontSize: '12px', color: '#cbd5e1', display: 'block', marginBottom: '6px' }}>
                    Delay in Seconds: <strong>{draft.forward_delay_seconds || draft.delay_seconds || 0}s</strong>
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
                    style={{ width: '100%', cursor: 'pointer' }}
                  />
                </div>

                <div style={{ fontSize: '12px', fontWeight: 600, color: '#cbd5e1', marginBottom: '8px' }}>
                  Quick Presets:
                </div>
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: '6px' }}>
                  {[
                    { label: '⚡ 0s (Instant)', sec: 0 },
                    { label: '⏱ 3s', sec: 3 },
                    { label: '⏱ 5s', sec: 5 },
                    { label: '⏱ 10s', sec: 10 },
                    { label: '⏱ 15s', sec: 15 },
                    { label: '⏱ 30s', sec: 30 },
                    { label: '⏱ 60s', sec: 60 },
                    { label: '⏱ 120s', sec: 120 },
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
                      style={{
                        backgroundColor:
                          (draft.forward_delay_seconds || draft.delay_seconds || 0) === preset.sec
                            ? '#eab308'
                            : '#0f172a',
                        color:
                          (draft.forward_delay_seconds || draft.delay_seconds || 0) === preset.sec
                            ? '#000000'
                            : '#f1f5f9',
                        border: '1px solid #334155',
                        borderRadius: '8px',
                        padding: '8px 4px',
                        fontSize: '11px',
                        fontWeight: 700,
                        cursor: 'pointer',
                        textAlign: 'center',
                      }}
                    >
                      {preset.label}
                    </button>
                  ))}
                </div>
              </div>
            </div>
          )}
        </div>

        {/* Bottom Action Footer */}
        <div
          style={{
            padding: '14px 18px',
            borderTop: '1px solid #1f2937',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'flex-end',
            gap: '10px',
            backgroundColor: '#131b2e',
          }}
        >
          <button
            type="button"
            onClick={onClose}
            style={{
              padding: '8px 16px',
              borderRadius: '10px',
              backgroundColor: '#1f2937',
              color: '#9ca3af',
              border: 'none',
              fontSize: '13px',
              fontWeight: 600,
              cursor: 'pointer',
            }}
          >
            Cancel
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
            style={{
              padding: '8px 20px',
              borderRadius: '10px',
              backgroundColor: '#3b82f6',
              color: '#ffffff',
              border: 'none',
              fontSize: '13px',
              fontWeight: 600,
              cursor: 'pointer',
              display: 'flex',
              alignItems: 'center',
              gap: '6px',
            }}
          >
            {saving ? 'Saving...' : 'Save Changes'}
          </button>
        </div>
      </div>
    </div>
  )
}
