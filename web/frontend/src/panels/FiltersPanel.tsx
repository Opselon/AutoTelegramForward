/**
 * Keyword and regex filters applied before forwarding.
 *
 * The overhaul: cards used to show three fields and delete via
 * `window.confirm`. Now every configured field is on the card (whitelist,
 * blacklist, both regex lists, media gates, length bounds, service messages),
 * the enable/disable switch is a real optimistic toggle with rollback on
 * failure (`api.toggleFilter`), deletion is a ConfirmDialog, "new filter"
 * opens the editor on an empty draft, and the card is memoized so a parent
 * re-render does not repaint the whole list.
 */
import { memo, useCallback, useState, type ReactNode } from 'react'
import { Edit3, Plus, Trash2 } from 'lucide-react'
import { api } from '../api'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { Badge, Button, EmptyState } from '../components/ui/Primitives'
import type { DashboardCtx } from '../App'
import type { FilterRule } from '../types'

/** One label/value row, only rendered when the value exists. */
function FilterRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <span className="filter-card__row-label">{label}</span>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>{children}</div>
    </div>
  )
}

function Chip({ tone, children }: { tone: 'success' | 'danger' | 'info' | 'neutral'; children: ReactNode }) {
  return (
    <Badge tone={tone}>
      {children}
    </Badge>
  )
}

interface FilterCardProps {
  filter: FilterRule
  t: (key: string, params?: Record<string, string | number>) => string
  onEdit: (filter: FilterRule) => void
  onDelete: (filter: FilterRule) => void
  onToggle: (filter: FilterRule) => void
  toggling: boolean
}

const FilterCard = memo(function FilterCard({
  filter,
  t,
  onEdit,
  onDelete,
  onToggle,
  toggling,
}: FilterCardProps) {
  const enabled = filter.is_enabled !== false
  const whitelist = Array.isArray(filter.whitelist_keywords) ? filter.whitelist_keywords : []
  const blacklist = Array.isArray(filter.blacklist_keywords) ? filter.blacklist_keywords : []
  const regexBlock = [...(Array.isArray(filter.regex_block) ? filter.regex_block : []), ...(Array.isArray(filter.regex_patterns) ? filter.regex_patterns : [])]
  const regexRequire = Array.isArray(filter.regex_require) ? filter.regex_require : []
  const allowed = Array.isArray(filter.allowed_media_types) ? filter.allowed_media_types : []
  const blocked = Array.isArray(filter.blocked_media_types) ? filter.blocked_media_types : []
  const hasLengthBounds = (filter.min_message_length ?? 0) > 0 || (filter.max_message_length ?? 0) > 0

  return (
    <div className="glass-panel filter-card" style={{ padding: 20, opacity: enabled ? 1 : 0.65 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 12, gap: 8 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', minWidth: 0 }}>
          <h3 style={{ fontSize: '1rem', fontWeight: 700 }}>{filter.name}</h3>
          <Badge tone={enabled ? 'success' : 'neutral'}>{enabled ? t('ui_filter_list_enabled') : t('ui_filter_list_disabled')}</Badge>
        </div>
        <div style={{ display: 'flex', gap: 6, flexShrink: 0 }}>
          <button
            onClick={() => onToggle(filter)}
            disabled={toggling}
            className="btn btn-secondary btn--sm"
            style={{ minHeight: 36, minWidth: 36, padding: '0 10px' }}
            aria-pressed={enabled}
            title={enabled ? t('ui_filter_list_disable') : t('ui_filter_list_enable')}
          >
            {enabled ? t('ui_filter_list_disable') : t('ui_filter_list_enable')}
          </button>
          <button
            onClick={() => onEdit(filter)}
            className="btn btn-secondary icon-btn"
            style={{ padding: '6px', minHeight: 36, minWidth: 36 }}
            aria-label={t('ui_edit')}
            title={t('ui_edit')}
          >
            <Edit3 size={14} aria-hidden="true" />
          </button>
          <button
            onClick={() => onDelete(filter)}
            className="btn btn-danger icon-btn"
            style={{ padding: '6px', minHeight: 36, minWidth: 36 }}
            aria-label={t('ui_delete')}
            title={t('ui_delete')}
          >
            <Trash2 size={14} aria-hidden="true" />
          </button>
        </div>
      </div>

      <div style={{ fontSize: '0.8rem', display: 'flex', flexDirection: 'column', gap: 8 }}>
        {whitelist.length > 0 && (
          <FilterRow label={t('ui_whitelist')}>
            {whitelist.map((w, idx) => (
              <Chip key={`${w}-${idx}`} tone="success">{w}</Chip>
            ))}
          </FilterRow>
        )}

        <FilterRow label={t('ui_blacklist')}>
          {blacklist.length > 0
            ? blacklist.map((w, idx) => <Chip key={`${w}-${idx}`} tone="danger">{w}</Chip>)
            : <span className="field__hint">{t('ui_comma_or_newline_separated')}</span>}
        </FilterRow>

        {regexBlock.length > 0 && (
          <FilterRow label={t('ui_regex_block_list')}>
            {regexBlock.map((rx, idx) => (
              <Chip key={`${rx}-${idx}`} tone="info">{rx}</Chip>
            ))}
          </FilterRow>
        )}

        {regexRequire.length > 0 && (
          <FilterRow label={t('ui_regex_require_list')}>
            {regexRequire.map((rx, idx) => (
              <Chip key={`${rx}-${idx}`} tone="info">{rx}</Chip>
            ))}
          </FilterRow>
        )}

        {(allowed.length > 0 || blocked.length > 0) && (
          <FilterRow label={t('ui_allowed_media')}>
            {allowed.map((m, idx) => <Chip key={`a-${m}-${idx}`} tone="success">{m}</Chip>)}
            {blocked.map((m, idx) => <Chip key={`b-${m}-${idx}`} tone="danger">{m}</Chip>)}
          </FilterRow>
        )}

        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
          {filter.drop_service_messages && (
            <Badge tone="neutral">{t('ui_drop_service_messages')}</Badge>
          )}
          {hasLengthBounds && (
            <Badge tone="neutral">
              {t('ui_min_message_length')} {filter.min_message_length ?? 0} · {t('ui_max_message_length')} {filter.max_message_length ?? 0}
            </Badge>
          )}
        </div>
      </div>
    </div>
  )
})

export default function FiltersPanel({ d }: { d: DashboardCtx }) {
  const {
    filterRules,
    lang,
    openFilterModal,
    t,
    showToast,
    setFilterRules,
    handleDeleteFilter,
  } = d

  const [deleteTarget, setDeleteTarget] = useState<FilterRule | null>(null)
  const [deleting, setDeleting] = useState(false)
  const [togglingId, setTogglingId] = useState<string | null>(null)

  // Optimistic enable/disable: flip locally first, roll back if the server
  // refuses, so the switch never lies but also never waits on the network.
  const toggleFilter = useCallback(
    async (filter: FilterRule) => {
      const next = filter.is_enabled === false
      setTogglingId(filter.id)
      setFilterRules((prev) => prev.map((f) => (f.id === filter.id ? { ...f, is_enabled: next } : f)))
      try {
        const saved = await api.toggleFilter(filter.id)
        setFilterRules((prev) => prev.map((f) => (f.id === filter.id ? saved : f)))
      } catch (e: any) {
        setFilterRules((prev) => prev.map((f) => (f.id === filter.id ? { ...f, is_enabled: filter.is_enabled } : f)))
        showToast(e?.message || t('ui_error'))
      } finally {
        setTogglingId(null)
      }
    },
    [setFilterRules, showToast, t],
  )

  const confirmDelete = async () => {
    if (!deleteTarget) return
    setDeleting(true)
    try {
      await handleDeleteFilter(deleteTarget.id)
      setDeleteTarget(null)
    } finally {
      setDeleting(false)
    }
  }

  const onEdit = useCallback((filter: FilterRule) => openFilterModal(filter), [openFilterModal])
  const onDelete = useCallback((filter: FilterRule) => setDeleteTarget(filter), [])
  const onToggle = useCallback((filter: FilterRule) => void toggleFilter(filter), [toggleFilter])

  return (
    <>
              <div>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 20 }}>
                  <div>
                    <h2 style={{ fontSize: '1.2rem', fontWeight: 700 }}>
                      {t('ui_advanced_content_media_filtering')}
                    </h2>
                    <p style={{ fontSize: 'var(--type-sm)', color: 'var(--color-text-muted)', marginTop: 4 }}>
                      {t('ui_manage_text_blacklists_whitelists_regex_patterns_and_media_type')}
                    </p>
                  </div>
                  {/* New filters start empty: no seeded SPAM keywords to notice. */}
                  <button
                    onClick={() => openFilterModal(null)}
                    className="btn btn-primary"
                  >
                    <Plus size={16} />
                    <span>{t('ui_new_filter_rule')}</span>
                  </button>
                </div>

                <div className="grid-collapse-1" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(360px, 1fr))', gap: 20 }}>
                  {(Array.isArray(filterRules) ? filterRules : []).length === 0 && (
                    <div style={{ gridColumn: '1 / -1' }}>
                      <EmptyState
                        title={t('ui_empty_title')}
                        description={t('ui_empty_body')}
                        action={
                          <Button variant="primary" onClick={() => openFilterModal(null)}>
                            <Plus size={16} aria-hidden="true" />
                            <span>{t('ui_create_first_filter')}</span>
                          </Button>
                        }
                      />
                    </div>
                  )}
                  {(Array.isArray(filterRules) ? filterRules : []).map((f) => (
                    <FilterCard
                      key={f.id}
                      filter={f}
                      t={t}
                      onEdit={onEdit}
                      onDelete={onDelete}
                      onToggle={onToggle}
                      toggling={togglingId === f.id}
                    />
                  ))}
                </div>
              </div>

      <ConfirmDialog
        open={!!deleteTarget}
        lang={lang}
        title={t('ui_confirm_delete_title')}
        body={deleteTarget ? t('ui_delete_this_filter_rule') : ''}
        confirmLabel={t('ui_delete')}
        cancelLabel={t('ui_cancel')}
        busy={deleting}
        onConfirm={() => void confirmDelete()}
        onCancel={() => setDeleteTarget(null)}
      />
    </>
  )
}
