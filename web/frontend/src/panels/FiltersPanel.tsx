/**
 * Keyword and regex filters applied before forwarding.
 */
import { Edit3, Plus, Trash2 } from 'lucide-react'
import { EmptyState } from '../components/ui/Primitives'
import type { DashboardCtx } from '../App'

export default function FiltersPanel({ d }: { d: DashboardCtx }) {
  const {
    filterRules,
    setIsFilterModalOpen,
    setFilterFormData,
    t,
    handleDeleteFilter,
  } = d
  return (
    <>
              <div>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 20 }}>
                  <div>
                    <h2 style={{ fontSize: '1.2rem', fontWeight: 700 }}>
                      {t('ui_advanced_content_media_filtering')}
                    </h2>
                    <p style={{ fontSize: '0.85rem', color: '#94a3b8', marginTop: 4 }}>
                      {t('ui_manage_text_blacklists_whitelists_regex_patterns_and_media_type')}
                    </p>
                  </div>
                  <button
                    onClick={() => {
                      setFilterFormData({
                        whitelist_keywords: [],
                        blacklist_keywords: ['SPAM', 'JOIN', 'PROMO'],
                        regex_patterns: [],
                        allowed_media_types: ['PHOTO', 'VIDEO', 'DOCUMENT'],
                        blocked_media_types: ['STICKER', 'ANIMATION'],
                        drop_service_messages: true,
                        min_message_length: 5,
                        max_message_length: 4000,
                      })
                      setIsFilterModalOpen(true)
                    }}
                    className="btn btn-primary"
                  >
                    <Plus size={16} />
                    <span>{t('ui_new_filter_rule')}</span>
                  </button>
                </div>

                <div className="grid-collapse-1" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(360px, 1fr))', gap: 20 }}>
                  {(Array.isArray(filterRules) ? filterRules : []).length === 0 && (
                    <div style={{ gridColumn: '1 / -1' }}>
                      <EmptyState title={t('ui_empty_title')} description={t('ui_empty_body')} />
                    </div>
                  )}
                  {(Array.isArray(filterRules) ? filterRules : []).map((f) => (
                    <div key={f.id} className="glass-panel" style={{ padding: 20 }}>
                      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 12 }}>
                        <h3 style={{ fontSize: '1rem', fontWeight: 700 }}>{f.name}</h3>
                        <div style={{ display: 'flex', gap: 6 }}>
                          <button
                            onClick={() => {
                              setFilterFormData({ ...f })
                              setIsFilterModalOpen(true)
                            }}
                            className="btn btn-secondary"
                            style={{ padding: '6px' }}
                            aria-label={t('ui_edit')}
                            title={t('ui_edit')}
                          >
                            <Edit3 size={14} aria-hidden="true" />
                          </button>
                          <button
                            onClick={() => handleDeleteFilter(f.id)}
                            className="btn btn-danger"
                            style={{ padding: '6px' }}
                            aria-label={t('ui_delete')}
                            title={t('ui_delete')}
                          >
                            <Trash2 size={14} aria-hidden="true" />
                          </button>
                        </div>
                      </div>

                      <div style={{ fontSize: '0.8rem', display: 'flex', flexDirection: 'column', gap: 8 }}>
                        {(Array.isArray(f?.whitelist_keywords) ? f.whitelist_keywords : []).length > 0 && (
                          <div>
                            <span style={{ color: '#34d399', display: 'block', marginBottom: 2 }}>
                              {t('ui_whitelist')}
                            </span>
                            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
                              {f.whitelist_keywords.map((w, idx) => (
                                <span key={idx} style={{ background: 'rgba(16, 185, 129, 0.15)', color: '#86efac', padding: '2px 6px', borderRadius: 4 }}>
                                  {w}
                                </span>
                              ))}
                            </div>
                          </div>
                        )}

                        <div>
                          <span style={{ color: '#f87171', display: 'block', marginBottom: 2 }}>
                            {t('ui_blacklist')}
                          </span>
                          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
                            {(Array.isArray(f?.blacklist_keywords) ? f.blacklist_keywords : []).map((w, idx) => (
                              <span key={idx} style={{ background: 'rgba(239, 68, 68, 0.15)', color: '#fca5a5', padding: '2px 6px', borderRadius: 4 }}>
                                {w}
                              </span>
                            ))}
                          </div>
                        </div>

                        {(Array.isArray(f?.regex_patterns) ? f.regex_patterns : []).length > 0 && (
                          <div>
                            <span style={{ color: '#38bdf8', display: 'block', marginBottom: 2 }}>
                              {t('ui_rp_mode_regex')}
                            </span>
                            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
                              {f.regex_patterns.map((rx, idx) => (
                                <span key={idx} style={{ background: 'rgba(56, 189, 248, 0.15)', color: '#7dd3fc', padding: '2px 6px', borderRadius: 4, fontFamily: 'monospace' }}>
                                  {rx}
                                </span>
                              ))}
                            </div>
                          </div>
                        )}

                        <div>
                          <span style={{ color: '#34d399', display: 'block', marginBottom: 2 }}>
                            {t('ui_allowed_media')}
                          </span>
                          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
                            {(Array.isArray(f?.allowed_media_types) ? f.allowed_media_types : []).map((m, idx) => (
                              <span key={idx} style={{ background: 'rgba(16, 185, 129, 0.15)', color: '#6ee7b7', padding: '2px 6px', borderRadius: 4 }}>
                                {m}
                              </span>
                            ))}
                          </div>
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              </div>

    </>
  )
}
