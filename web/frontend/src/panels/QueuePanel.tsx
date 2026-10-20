/**
 * The delivery queue and the dead-letter queue.
 */
import { Activity, Clock, RefreshCw, Trash2 } from 'lucide-react'
import { EmptyState } from '../components/ui/Primitives'
import type { DashboardCtx } from '../App'

export default function QueuePanel({ d }: { d: DashboardCtx }) {
  const {
    queueJobs,
    dlqJobs,
    deliveryStats,
    t,
    handleRetryDLQ,
    handlePurgeDLQ,
  } = d
  return (
    <>
              <div>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 20 }}>
                  <div>
                    <h2 style={{ fontSize: '1.2rem', fontWeight: 700 }}>
                      {t('ui_delivery_jobs_dead_letter_queue_dlq')}
                    </h2>
                    <p style={{ fontSize: '0.85rem', color: '#94a3b8', marginTop: 4 }}>
                      {t('ui_track_in_flight_delivery_jobs_and_recover_failed_telegram_messag')}
                    </p>
                  </div>
                  {dlqJobs.length > 0 && (
                    <button onClick={handlePurgeDLQ} className="btn btn-danger">
                      <Trash2 size={16} />
                      <span>{t('ui_purge_all_dlq')}</span>
                    </button>
                  )}
                </div>

                {/* Delivery totals — the same snapshot the overview cards read, shown
                    here so the queue tab answers "how much moved?" at a glance. */}
                {deliveryStats && (
                  <div
                    style={{
                      display: 'grid',
                      gridTemplateColumns: 'repeat(auto-fit, minmax(120px, 1fr))',
                      gap: 12,
                      marginBottom: 20,
                    }}
                  >
                    {[
                      { label: t('ui_processed'), value: deliveryStats.processed_total ?? 0, color: '#818cf8' },
                      { label: t('ui_forwarded'), value: deliveryStats.forwarded_total ?? 0, color: '#34d399' },
                      { label: t('ui_failed'), value: deliveryStats.failed_total ?? 0, color: '#f87171' },
                      { label: t('ui_filtered'), value: deliveryStats.filtered_total ?? 0, color: '#fbbf24' },
                      { label: t('ui_in_queue'), value: deliveryStats.in_queue ?? 0, color: '#38bdf8' },
                    ].map((m) => (
                      <div key={m.label} className="glass-panel" style={{ padding: '10px 14px' }}>
                        <div style={{ fontSize: 'var(--type-xs)', color: '#94a3b8' }}>{m.label}</div>
                        <div style={{ fontSize: '1.15rem', fontWeight: 800, color: m.color }}>{m.value}</div>
                      </div>
                    ))}
                  </div>
                )}

                <div className="grid-collapse-1" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(400px, 1fr))', gap: 24 }}>
                  {/* Active Delivery Queue */}
                  <div className="glass-panel" style={{ padding: 20 }}>
                    <h3 style={{ fontSize: '1rem', fontWeight: 700, marginBottom: 12, display: 'flex', alignItems: 'center', gap: 8 }}>
                      <Clock size={16} color="#818cf8" />
                      <span>{t('ui_recent_delivery_jobs')}</span>
                    </h3>

                    {queueJobs.length === 0 ? (
                      <EmptyState title={t('ui_empty_title')} description={t('ui_no_pending_jobs_in_queue')} />
                    ) : (
                      <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                        {(Array.isArray(queueJobs) ? queueJobs : []).map((q) => (
                          <div
                            key={q.id}
                            style={{
                              background: 'rgba(15, 23, 42, 0.6)',
                              padding: '10px 14px',
                              borderRadius: 8,
                              fontSize: '0.8rem',
                              display: 'flex',
                              justifyContent: 'space-between',
                              alignItems: 'center',
                            }}
                          >
                            <div>
                              <div style={{ fontWeight: 600 }}>{t('ui_msg')} #{q.source_message_id}</div>
                              <div style={{ color: '#94a3b8', fontSize: '0.75rem' }}>{t('ui_stage')} {q.delivery_stage}</div>
                            </div>
                            <span className="badge badge-direct">{q.status}</span>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>

                  {/* Dead Letter Queue */}
                  <div className="glass-panel" style={{ padding: 20 }}>
                    <h3 style={{ fontSize: '1rem', fontWeight: 700, marginBottom: 12, display: 'flex', alignItems: 'center', gap: 8 }}>
                      <Activity size={16} color="#ef4444" />
                      <span>{t('ui_dead_letter_queue_dlq')}</span>
                      <span className="badge badge-vip">{dlqJobs.length}</span>
                    </h3>

                    {dlqJobs.length === 0 ? (
                      <EmptyState title={t('ui_empty_title')} description={t('ui_dead_letter_queue_is_empty')} />
                    ) : (
                      <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
                        {(Array.isArray(dlqJobs) ? dlqJobs : []).map((dlq) => (
                          <div
                            key={dlq.id}
                            style={{
                              background: 'rgba(15, 23, 42, 0.6)',
                              padding: '12px 14px',
                              borderRadius: 8,
                              fontSize: '0.8rem',
                              border: '1px solid rgba(239, 68, 68, 0.3)',
                            }}
                          >
                            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 6 }}>
                              <span style={{ fontWeight: 700, color: '#fca5a5' }}>{dlq.error_category}</span>
                              <button
                                onClick={() => handleRetryDLQ(dlq.id)}
                                className="btn btn-secondary"
                                style={{ padding: '2px 8px', fontSize: '0.75rem' }}
                              >
                                <RefreshCw size={12} />
                                <span>{t('ui_retry')}</span>
                              </button>
                            </div>
                            <div style={{ color: '#cbd5e1', fontSize: '0.75rem' }}>{dlq.last_error}</div>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
              </div>

    </>
  )
}
