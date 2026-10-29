/**
 * Smart-forwarding rules: the list, the per-rule live counters and the
 * brand-hop summary. A chunk of its own, so the landing tab never downloads the
 * rule editor's markup, icons or branding-hop helpers.
 *
 * Two things changed in the overhaul:
 *
 * - "New rule" opens the editor on a clean draft (the editor owns its draft
 *   since the modal was extracted) — this panel used to seed it with fake demo
 *   data the user had to notice and overwrite.
 * - The card is `React.memo`'d and receives only stable inputs (the handlers
 *   are memoized in the shell), so a shell re-render does not re-render every
 *   card in the list. Deletion is confirmed by a real `ConfirmDialog`, not
 *   `window.confirm`.
 */
import { memo, useCallback, useState } from 'react'
import {
  ArrowDown,
  ArrowRightLeft,
  Bot,
  ChevronLeft,
  ChevronRight,
  Clock,
  Copy,
  Edit3,
  Filter,
  PlayCircle,
  Plus,
  Send,
  Shield,
  SlidersHorizontal,
  Sparkles,
  Trash2,
} from 'lucide-react'
import { DiagnosticsPanel } from '../components/DiagnosticsPanel'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { EmptyState } from '../components/ui/Primitives'
import { normalizeEndpoint } from '../endpoint'
import { extraSourceCount } from '../components/ShellWidgets'
import type { DashboardCtx } from '../App'
import type { ForwardRule, RuleLiveStat } from '../types'

interface RuleCardProps {
  rule: ForwardRule
  liveStat?: RuleLiveStat
  isRtl: boolean
  t: (key: string, params?: Record<string, string | number>) => string
  onToggle: (id: string) => void
  onPause: (id: string) => void
  onResume: (id: string) => void
  onEdit: (rule: ForwardRule) => void
  onDuplicate: (id: string) => void
  onDelete: (rule: ForwardRule) => void
  onOverview: (rule: ForwardRule) => void
  onQuickRoute: (id: string, path: 'route1_direct' | 'route2_vip_hop' | 'route3_native') => void
}

/**
 * One rule card. Memoized: the list is the heaviest render in the panel, and a
 * parent re-render (a toast, a poll tick) must not re-render every card.
 */
const RuleCard = memo(function RuleCard({
  rule,
  liveStat,
  isRtl,
  t,
  onToggle,
  onPause,
  onResume,
  onEdit,
  onDuplicate,
  onDelete,
  onOverview,
  onQuickRoute,
}: RuleCardProps) {
  const isVipHop = rule.use_intermediate && rule.message_category === 'VIP_ONLY'
  const isDirectCopy = !rule.use_intermediate && rule.forward_mode !== 'DIRECT_FORWARD'
  const isNative = !rule.use_intermediate && rule.forward_mode === 'DIRECT_FORWARD'
  // One normalizer for the loop badge, the same one the editor and the server use.
  const isLoop =
    !!rule.source_chat_id &&
    !!rule.target_chat_id &&
    normalizeEndpoint(rule.source_chat_id) === normalizeEndpoint(rule.target_chat_id)

  return (
    <div
      className="glass-panel rule-card"
      style={{
        padding: '16px',
        position: 'relative',
        border: isVipHop ? '1px solid rgba(245, 158, 11, 0.4)' : '1px solid var(--border-color)',
        boxShadow: isVipHop ? '0 0 20px rgba(245, 158, 11, 0.08)' : 'none',
      }}
    >
      {/* Top Bar of Card */}
      <div className="rule-card-head" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 12, gap: 8, flexWrap: 'wrap' }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
          <div className="rule-badges" style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
            {isVipHop && (
              <span className="badge badge-vip" style={{ fontSize: 'var(--type-xs)', padding: '3px 8px' }}>
                <Sparkles size={11} />
                {t('ui_route_2_vip_hop')}
              </span>
            )}
            {isDirectCopy && (
              <span className="badge badge-direct" style={{ fontSize: 'var(--type-xs)', padding: '3px 8px' }}>
                <Copy size={11} />
                {t('ui_route_1_direct_copy')}
              </span>
            )}
            {isNative && (
              <span className="badge badge-native" style={{ fontSize: 'var(--type-xs)', padding: '3px 8px' }}>
                <Send size={11} />
                {t('ui_route_3_native')}
              </span>
            )}
            {isLoop && (
              <span
                className="badge"
                style={{
                  background: 'rgba(239, 68, 68, 0.2)',
                  color: 'var(--color-danger-text)',
                  border: '1px solid rgba(239, 68, 68, 0.5)',
                  fontSize: 'var(--type-xs)',
                }}
              >
                <Shield size={11} color="var(--color-danger)" />
                {t('ui_loop')}
              </span>
            )}
            <span className="badge badge-gray" style={{ fontSize: 'var(--type-xs)', padding: '2px 6px' }}>
              P:{rule.priority}
            </span>
          </div>
          <div
            title={rule.id}
            onClick={() => navigator.clipboard?.writeText(rule.id).catch(() => {})}
            style={{
              fontSize: 'var(--type-xs)',
              color: 'var(--color-text-muted)',
              cursor: 'pointer',
              display: 'inline-flex',
              alignItems: 'center',
              gap: 4,
              padding: '1px 4px',
              borderRadius: 4,
            }}
          >
            <Copy size={10} /> <span className="font-mono-ltr">{rule.id.substring(0, 8)}…</span>
          </div>
        </div>

        <div className="rule-card-actions" style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap', justifyContent: 'flex-end' }}>
          <button
            onClick={() => onToggle(rule.id)}
            className="btn"
            style={{
              padding: '4px 8px',
              fontSize: 'var(--type-xs)',
              background: rule.is_active ? 'rgba(16, 185, 129, 0.15)' : 'rgba(239, 68, 68, 0.15)',
              color: rule.is_active ? 'var(--color-success)' : 'var(--color-danger-text)',
              border: rule.is_active ? '1px solid rgba(16, 185, 129, 0.3)' : '1px solid rgba(239, 68, 68, 0.3)',
            }}
          >
            {rule.is_active ? (t('ui_active')) : (t('ui_paused'))}
          </button>

          {rule.is_paused ? (
            <button
              onClick={() => onResume(rule.id)}
              className="btn btn-secondary icon-btn"
              style={{ padding: '4px 8px', fontSize: 'var(--type-xs)', color: 'var(--color-success)' }}
              title={t('ui_resume')}
            >
              <PlayCircle size={12} />
              <span>{t('ui_resume')}</span>
            </button>
          ) : (
            <button
              onClick={() => onPause(rule.id)}
              className="btn btn-secondary icon-btn"
              style={{ padding: '4px 8px', fontSize: 'var(--type-xs)', color: 'var(--color-warning)' }}
              title={t('ui_pause')}
            >
              <Clock size={12} />
              <span>{t('ui_pause')}</span>
            </button>
          )}

          <button
            onClick={() => onOverview(rule)}
            className="btn btn-secondary icon-btn"
            style={{ padding: '5px', background: 'rgba(59, 130, 246, 0.2)', color: '#60a5fa', borderColor: 'rgba(59, 130, 246, 0.4)' }}
            title={t('ui_task_overview_advanced_settings')}
          >
            <SlidersHorizontal size={13} />
          </button>

          <button
            onClick={() => onEdit(rule)}
            className="btn btn-secondary icon-btn"
            style={{ padding: '5px' }}
            title={t('ui_edit')}
          >
            <Edit3 size={13} />
          </button>

          <button
            onClick={() => onDuplicate(rule.id)}
            className="btn btn-secondary icon-btn"
            style={{ padding: '5px' }}
            title={t('ui_duplicate_task')}
          >
            <Copy size={13} />
          </button>

          <button
            onClick={() => onDelete(rule)}
            className="btn btn-danger icon-btn"
            style={{ padding: '5px' }}
            title={t('ui_delete')}
          >
            <Trash2 size={13} />
          </button>
        </div>
      </div>

      {/* Channel Flow Diagram — Mobile (Vertical Stack) */}
      <div className="channel-flow-mobile" style={{ background: 'rgba(15, 23, 42, 0.6)', borderRadius: 10, padding: '12px 14px', marginBottom: 14, border: '1px solid rgba(255, 255, 255, 0.05)', overflow: 'hidden' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8, minWidth: 0 }}>
          <div style={{ minWidth: 0, flex: 1 }}>
            <div style={{ color: 'var(--color-text-muted)', fontSize: 'var(--type-xs)' }}>
              {t('ui_source_a')}
              {extraSourceCount(rule) > 0 && (
                <span className="badge" style={{ fontSize: 'var(--type-xs)', marginInlineStart: 6, background: 'rgba(59, 130, 246, 0.18)', color: '#93c5fd' }}>
                  +{extraSourceCount(rule)}
                </span>
              )}
            </div>
            <div style={{ fontWeight: 600, color: '#f8fafc', fontSize: '0.82rem', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
              {rule.source_chat_name || rule.source_chat_id}
            </div>
          </div>
          <span className="font-mono-ltr channel-id-mono" style={{ fontSize: 'var(--type-xs)', color: 'var(--color-text-muted)', flexShrink: 0, marginInlineStart: 8, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', maxWidth: '46%' }}>
            {rule.source_chat_id}
          </span>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 8, padding: '2px 0' }}>
          <div style={{ flex: 1, height: 1, background: 'rgba(255,255,255,0.08)' }} />
          <span className="badge badge-direct" style={{ fontSize: 'var(--type-xs)', padding: '2px 8px' }}>
            <ArrowDown size={11} /> {rule.use_intermediate ? (t('ui_hop_c')) : (t('ui_direct'))}
          </span>
          <div style={{ flex: 1, height: 1, background: 'rgba(255,255,255,0.08)' }} />
        </div>

        {rule.use_intermediate && (
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8, minWidth: 0, background: 'rgba(245, 158, 11, 0.08)', padding: '6px 10px', borderRadius: 6 }}>
            <div style={{ minWidth: 0, flex: 1 }}>
              <div style={{ color: 'var(--color-warning)', fontSize: 'var(--type-xs)' }}>{t('ui_hop_c')}</div>
              <div style={{ fontWeight: 600, color: 'var(--color-warning)', fontSize: '0.8rem', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                {rule.intermediate_channel_name || rule.intermediate_channel_id}
              </div>
            </div>
            <span className="font-mono-ltr channel-id-mono" style={{ fontSize: 'var(--type-xs)', color: '#b45309', flexShrink: 0, marginInlineStart: 8, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', maxWidth: '46%' }}>
              {rule.intermediate_channel_id}
            </span>
          </div>
        )}

        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 8, minWidth: 0 }}>
          <div style={{ minWidth: 0, flex: 1 }}>
            <div style={{ color: 'var(--color-text-muted)', fontSize: 'var(--type-xs)' }}>{t('ui_target_b')}</div>
            <div style={{ fontWeight: 600, color: '#f8fafc', fontSize: '0.82rem', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
              {rule.target_chat_name || rule.target_chat_id}
            </div>
          </div>
          <span className="font-mono-ltr channel-id-mono" style={{ fontSize: 'var(--type-xs)', color: 'var(--color-text-muted)', flexShrink: 0, marginInlineStart: 8, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', maxWidth: '46%' }}>
            {rule.target_chat_id}
          </span>
        </div>
      </div>

      {/* Channel Flow Diagram — Desktop (Horizontal Row) */}
      <div
        className="channel-flow-desktop"
        style={{
          background: 'rgba(15, 23, 42, 0.6)',
          borderRadius: 10,
          padding: '12px 14px',
          marginBottom: 14,
          border: '1px solid rgba(255, 255, 255, 0.05)',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: 8,
          fontSize: '0.82rem',
        }}
      >
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ color: 'var(--color-text-muted)', fontSize: 'var(--type-xs)' }}>{t('ui_source_a')}</div>
          <div style={{ fontWeight: 600, color: '#f8fafc', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
            {rule.source_chat_name || rule.source_chat_id}
          </div>
          <div style={{ fontSize: 'var(--type-xs)', color: 'var(--color-text-muted)' }}>
            <span className="font-mono-ltr">{rule.source_chat_id}</span>
          </div>
        </div>

        {rule.use_intermediate ? (
          <>
            <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', color: 'var(--color-warning)', flexShrink: 0 }}>
              <span style={{ fontSize: 'var(--type-xs)' }}>{t('ui_hop')}</span>
              <ArrowRightLeft size={14} />
            </div>
            <div style={{ flex: 1, minWidth: 0, textAlign: 'center' }}>
              <div style={{ color: 'var(--color-warning)', fontSize: 'var(--type-xs)' }}>{t('ui_brand_c')}</div>
              <div style={{ fontWeight: 600, color: 'var(--color-warning)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                {rule.intermediate_channel_name || rule.intermediate_channel_id || t('ui_channel_c')}
              </div>
              <div style={{ fontSize: 'var(--type-xs)', color: '#b45309' }}>
                <span className="font-mono-ltr">{rule.intermediate_channel_id}</span>
              </div>
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', color: 'var(--color-success)', flexShrink: 0 }}>
              <span style={{ fontSize: 'var(--type-xs)' }}>{t('ui_native')}</span>
              {isRtl ? <ChevronLeft size={14} /> : <ChevronRight size={14} />}
            </div>
          </>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', color: '#3b82f6', padding: '0 8px', flexShrink: 0 }}>
            <span style={{ fontSize: 'var(--type-xs)' }}>{t('ui_direct')}</span>
            {isRtl ? <ChevronLeft size={14} /> : <ChevronRight size={14} />}
          </div>
        )}

        <div style={{ flex: 1, minWidth: 0, textAlign: isRtl ? 'left' : 'right' }}>
          <div style={{ color: 'var(--color-text-muted)', fontSize: 'var(--type-xs)' }}>{t('ui_target_b')}</div>
          <div style={{ fontWeight: 600, color: '#f8fafc', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
            {rule.target_chat_name || rule.target_chat_id}
          </div>
          <div style={{ fontSize: 'var(--type-xs)', color: 'var(--color-text-muted)' }}>
            <span className="font-mono-ltr">{rule.target_chat_id}</span>
          </div>
        </div>
      </div>

      {/* Criteria & Details */}
      <div style={{ fontSize: '0.78rem', color: 'var(--color-text-subtle)', marginBottom: 12 }}>
        {rule.custom_header && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 }}>
            <span style={{ color: 'var(--color-text-muted)' }}>{t('ui_header')}</span>
            <code style={{ background: 'rgba(255,255,255,0.06)', padding: '2px 6px', borderRadius: 4, color: 'var(--color-warning)' }}>
              {rule.custom_header}
            </code>
          </div>
        )}
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 6 }}>
          <span className="badge badge-gray" style={{ fontSize: 'var(--type-xs)', padding: '2px 6px' }}>
            {rule.link_policy}
          </span>
          {rule.filter_rule_id && (
            <span className="badge badge-direct" style={{ fontSize: 'var(--type-xs)', padding: '2px 6px' }}>
              <Filter size={9} /> {t('ui_filter')}
            </span>
          )}
          {rule.ai_config_id && (
            <span className="badge badge-vip" style={{ fontSize: 'var(--type-xs)', padding: '2px 6px' }}>
              <Bot size={9} /> AI
            </span>
          )}
          {rule.fallback_enabled && (
            <span className="badge badge-gray" style={{ fontSize: 'var(--type-xs)', padding: '2px 6px', color: '#a7f3d0' }}>
              <Shield size={9} /> {t('ui_fallback')} {rule.fallback_mode}
            </span>
          )}
        </div>
      </div>

      {/* Live per-rule counters from the delivery pipeline */}
      {liveStat && (
        <div
          className="rule-stats"
          style={{
            display: 'flex',
            gap: 0,
            marginBottom: 12,
            borderRadius: 8,
            overflow: 'hidden',
            border: '1px solid rgba(255, 255, 255, 0.06)',
            fontSize: 'var(--type-xs)',
          }}
        >
          <div className="rule-stat" style={{ flex: 1, padding: '7px 6px', background: 'rgba(16, 185, 129, 0.10)', textAlign: 'center' }}>
            <div style={{ color: 'var(--color-text-muted)', fontSize: 'var(--type-xs)', fontWeight: 600 }}>{t('ui_fwd')}</div>
            <div style={{ fontWeight: 800, color: 'var(--color-success)', lineHeight: 1.35 }}>{liveStat.forwarded ?? 0}</div>
          </div>
          <div className="rule-stat" style={{ flex: 1, padding: '7px 6px', background: 'rgba(59, 130, 246, 0.10)', textAlign: 'center' }}>
            <div style={{ color: 'var(--color-text-muted)', fontSize: 'var(--type-xs)', fontWeight: 600 }}>{t('ui_filt')}</div>
            <div style={{ fontWeight: 800, color: '#60a5fa', lineHeight: 1.35 }}>{liveStat.filtered ?? 0}</div>
          </div>
          <div className="rule-stat" style={{ flex: 1, padding: '7px 6px', background: liveStat.errors > 0 ? 'rgba(239, 68, 68, 0.18)' : 'rgba(239, 68, 68, 0.07)', textAlign: 'center' }}>
            <div style={{ color: 'var(--color-text-muted)', fontSize: 'var(--type-xs)', fontWeight: 600 }}>{t('ui_err')}</div>
            <div style={{ fontWeight: 800, color: liveStat.errors > 0 ? 'var(--color-danger-text)' : 'var(--color-text-muted)', lineHeight: 1.35 }}>{liveStat.errors ?? 0}</div>
          </div>
          <div
            className="rule-stat-last"
            style={{
              flex: 1.3,
              padding: '7px 6px',
              background: 'rgba(99, 102, 241, 0.06)',
              textAlign: 'center',
            }}
          >
            <span style={{ color: 'var(--color-text-muted)', fontSize: 'var(--type-xs)' }}>{t('ui_last')}</span>
            <span style={{ fontWeight: 700, color: '#818cf8', fontSize: 'var(--type-xs)' }}>
              {liveStat.last_forward_ts
                ? new Date(liveStat.last_forward_ts * 1000).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
                : '—'}
            </span>
          </div>
        </div>
      )}
      {liveStat?.last_error && (
        <div
          dir="ltr"
          style={{
            textAlign: 'left',
            marginBottom: 12,
            padding: '6px 10px',
            borderRadius: 6,
            background: 'rgba(239, 68, 68, 0.07)',
            border: '1px solid rgba(239, 68, 68, 0.2)',
            color: 'var(--color-danger-text)',
            fontSize: 'var(--type-xs)',
            fontFamily: "'JetBrains Mono', Consolas, monospace",
            wordBreak: 'break-word',
          }}
        >
          ⚠️ {liveStat.last_error.slice(0, 160)}
        </div>
      )}

      {/* 1-Click Route Switch Buttons */}
      <div
        style={{
          borderTop: '1px solid rgba(255, 255, 255, 0.06)',
          paddingTop: 10,
        }}
      >
        <div className="route-switch-grid">
          <button
            onClick={() => onQuickRoute(rule.id, 'route1_direct')}
            className={`route-switch-btn btn ${isDirectCopy ? 'btn-primary' : 'btn-secondary'}`}
          >
            <Copy size={11} />
            <span>{t('ui_route_1_direct')}</span>
          </button>

          <button
            onClick={() => onQuickRoute(rule.id, 'route2_vip_hop')}
            className={`route-switch-btn btn ${isVipHop ? 'btn-vip' : 'btn-secondary'}`}
          >
            <Sparkles size={11} />
            <span>{t('ui_route_2_vip')}</span>
          </button>

          <button
            onClick={() => onQuickRoute(rule.id, 'route3_native')}
            className={`route-switch-btn btn ${isNative ? 'btn-primary' : 'btn-secondary'}`}
          >
            <Send size={11} />
            <span>{t('ui_route_3_native')}</span>
          </button>
        </div>
      </div>
    </div>
  )
})

export default function RulesPanel({ d }: { d: DashboardCtx }) {
  const {
    lang,
    rules,
    ruleLiveStats,
    diagnostics,
    diagLoading,
    refreshDiagnostics,
    openRuleModal,
    setTaskOverviewRule,
    setIsTaskOverviewOpen,
    isRtl,
    t,
    handleToggleRule,
    handleDeleteRule,
    handleDuplicateRule,
    handleQuickRouteSwitch,
    handlePauseRule,
    handleResumeRule,
  } = d

  // Deletion is a real dialog: it names the task and blocks the main flow
  // until answered, instead of the unstyled `window.confirm`.
  const [deleteTarget, setDeleteTarget] = useState<ForwardRule | null>(null)
  const [deleting, setDeleting] = useState(false)
  const confirmDelete = async () => {
    if (!deleteTarget) return
    setDeleting(true)
    try {
      await handleDeleteRule(deleteTarget.id)
      setDeleteTarget(null)
    } finally {
      setDeleting(false)
    }
  }

  // Stable per-card callbacks so the memoized cards only re-render when their
  // own inputs (rule, live stat, language) change.
  const onOverview = useCallback(
    (rule: ForwardRule) => {
      setTaskOverviewRule(rule)
      setIsTaskOverviewOpen(true)
    },
    [setTaskOverviewRule, setIsTaskOverviewOpen],
  )
  const onEdit = useCallback((rule: ForwardRule) => openRuleModal(rule), [openRuleModal])
  const onDelete = useCallback((rule: ForwardRule) => setDeleteTarget(rule), [])
  const onToggle = useCallback((id: string) => void handleToggleRule(id), [handleToggleRule])
  const onPause = useCallback((id: string) => void handlePauseRule(id), [handlePauseRule])
  const onResume = useCallback((id: string) => void handleResumeRule(id), [handleResumeRule])
  const onDuplicate = useCallback((id: string) => void handleDuplicateRule(id), [handleDuplicateRule])
  const onQuickRoute = useCallback(
    (id: string, path: 'route1_direct' | 'route2_vip_hop' | 'route3_native') =>
      void handleQuickRouteSwitch(id, path),
    [handleQuickRouteSwitch],
  )

  return (
    <>
              <div>
                <DiagnosticsPanel
                  report={diagnostics}
                  loading={diagLoading}
                  onRefresh={refreshDiagnostics}
                  lang={lang}
                />
                <div className="hero-container" style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 20 }}>
                  <div>
                    <h2 style={{ fontSize: '1.2rem', fontWeight: 800, margin: 0 }}>
                      {t('ui_smart_forwarding_routing_rules')}
                    </h2>
                    <p className="text-balance" style={{ fontSize: '0.82rem', color: '#94a3b8', marginTop: 4, margin: 0 }}>
                      {t('ui_configure_triple_routes_branding_hops_bypass_policies_and_channe')}
                    </p>
                  </div>
                  {/* A new task starts from the editor's clean default — this
                      button no longer seeds fake demo data. */}
                  <button
                    onClick={() => openRuleModal(null)}
                    className="hero-add-btn btn btn-primary"
                  >
                    <Plus size={16} />
                    <span>{t('ui_new_rule')}</span>
                  </button>
                </div>

                <div className="rules-grid" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(360px, 1fr))', gap: 16 }}>
                  {(Array.isArray(rules) ? rules : []).length === 0 && (
                    <div style={{ gridColumn: '1 / -1' }}>
                      <EmptyState title={t('ui_empty_title')} description={t('ui_empty_body')} />
                    </div>
                  )}
                  {(Array.isArray(rules) ? rules : []).map((rule) => (
                    <RuleCard
                      key={rule.id}
                      rule={rule}
                      liveStat={(ruleLiveStats || []).find((st) => st.rule_id === rule.id)}
                      isRtl={isRtl}
                      t={t}
                      onToggle={onToggle}
                      onPause={onPause}
                      onResume={onResume}
                      onEdit={onEdit}
                      onDuplicate={onDuplicate}
                      onDelete={onDelete}
                      onOverview={onOverview}
                      onQuickRoute={onQuickRoute}
                    />
                  ))}
                </div>
              </div>

      <ConfirmDialog
        open={!!deleteTarget}
        lang={lang}
        title={t('ui_confirm_delete_title')}
        body={deleteTarget ? t('ui_are_you_sure_you_want_to_delete_this_rule') : ''}
        confirmLabel={t('ui_delete')}
        cancelLabel={t('ui_cancel')}
        busy={deleting}
        onConfirm={() => void confirmDelete()}
        onCancel={() => setDeleteTarget(null)}
      />
    </>
  )
}
