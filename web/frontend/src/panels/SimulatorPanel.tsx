/**
 * The message simulator: build a fake message, run it through the matcher,
 * read the decision it would produce. Lazy — a reader who never simulates never
 * pays for it.
 */
import { Activity, Lock, PlayCircle, Shield, Zap } from 'lucide-react'
import type { DashboardCtx } from '../App'

export default function SimulatorPanel({ d }: { d: DashboardCtx }) {
  const {
    rules,
    simText,
    setSimText,
    simOriginChatId,
    setSimOriginChatId,
    simOriginTitle,
    setSimOriginTitle,
    simOriginUser,
    setSimOriginUser,
    simHasMedia,
    setSimHasMedia,
    simIsProtected,
    setSimIsProtected,
    simSelectedRuleId,
    setSimSelectedRuleId,
    simResult,
    simulating,
    isRtl,
    t,
    handleRunSimulation,
  } = d
  return (
    <>
              <div className="grid-collapse-1" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(400px, 1fr))', gap: 24 }}>
                {/* Controls Panel */}
                <div className="glass-panel" style={{ padding: 24 }}>
                  <h2 style={{ fontSize: '1.2rem', fontWeight: 700, marginBottom: 8, display: 'flex', alignItems: 'center', gap: 8 }}>
                    <PlayCircle size={20} color="#fbbf24" />
                    <span>{t('ui_live_smart_simulator')}</span>
                  </h2>
                  <p style={{ fontSize: '0.85rem', color: '#94a3b8', marginBottom: 20 }}>
                    {t('ui_test_routing_engine_decisions_vip_origin_criteria_and_protected')}
                  </p>

                  <div style={{ marginBottom: 16 }}>
                    <label style={{ display: 'block', fontSize: '0.8rem', color: '#cbd5e1', marginBottom: 6 }}>
                      {t('ui_select_target_rule')}
                    </label>
                    <select
                      value={simSelectedRuleId}
                      onChange={(e) => setSimSelectedRuleId(e.target.value)}
                      className="input-field"
                    >
                      {(Array.isArray(rules) ? rules : []).map((r) => (
                        <option key={r.id} value={r.id}>
                          {r.source_chat_name || r.source_chat_id} ➔ {r.target_chat_name || r.target_chat_id} (
                          {r.use_intermediate ? t('ui_vip_hop_via_c') : r.forward_mode})
                        </option>
                      ))}
                    </select>
                  </div>

                  <div style={{ marginBottom: 16 }}>
                    <label style={{ display: 'block', fontSize: '0.8rem', color: '#cbd5e1', marginBottom: 6 }}>
                      {t('ui_simulated_message_text')}
                    </label>
                    <textarea
                      rows={4}
                      value={simText}
                      onChange={(e) => setSimText(e.target.value)}
                      className="input-field"
                      style={{ fontFamily: 'inherit', resize: 'vertical' }}
                    />
                  </div>

                  <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginBottom: 16 }}>
                    <div>
                      <label style={{ display: 'block', fontSize: '0.8rem', color: '#cbd5e1', marginBottom: 6 }}>
                        {t('ui_forward_origin_chat_id')}
                      </label>
                      <input
                        type="text"
                        value={simOriginChatId}
                        onChange={(e) => setSimOriginChatId(e.target.value)}
                        className="input-field"
                        placeholder="-1001111111111"
                      />
                    </div>

                    <div>
                      <label style={{ display: 'block', fontSize: '0.8rem', color: '#cbd5e1', marginBottom: 6 }}>
                        {t('ui_forward_origin_title')}
                      </label>
                      <input
                        type="text"
                        value={simOriginTitle}
                        onChange={(e) => setSimOriginTitle(e.target.value)}
                        className="input-field"
                        placeholder={t('ui_forex_vip_channel')}
                      />
                    </div>
                  </div>

                  <div style={{ marginBottom: 16 }}>
                    <label style={{ display: 'block', fontSize: '0.8rem', color: '#cbd5e1', marginBottom: 6 }}>
                      {t('ui_forward_origin_username')}
                    </label>
                    <input
                      type="text"
                      value={simOriginUser}
                      onChange={(e) => setSimOriginUser(e.target.value)}
                      className="input-field"
                      placeholder="vip_forex_signals"
                    />
                  </div>

                  <div style={{ display: 'flex', gap: 16, marginBottom: 20 }}>
                    <label style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer', fontSize: '0.85rem' }}>
                      <input
                        type="checkbox"
                        checked={simIsProtected}
                        onChange={(e) => setSimIsProtected(e.target.checked)}
                      />
                      <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                        <Lock size={14} color="#f87171" />
                        {t('ui_protected_content_channel')}
                      </span>
                    </label>

                    <label style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer', fontSize: '0.85rem' }}>
                      <input
                        type="checkbox"
                        checked={simHasMedia}
                        onChange={(e) => setSimHasMedia(e.target.checked)}
                      />
                      <span>{t('ui_has_media')}</span>
                    </label>
                  </div>

                  <button
                    onClick={handleRunSimulation}
                    disabled={simulating}
                    className="btn btn-vip"
                    style={{ width: '100%', padding: '12px' }}
                  >
                    <Zap size={18} />
                    <span>{simulating ? (t('ui_simulating')) : (t('ui_run_decision_engine'))}</span>
                  </button>
                </div>

                {/* Results Panel */}
                <div className="glass-panel" style={{ padding: 24 }}>
                  <h3 style={{ fontSize: '1.1rem', fontWeight: 700, marginBottom: 14 }}>
                    {t('ui_engine_decision_workflow_trace')}
                  </h3>

                  {simResult ? (
                    <div>
                      <div
                        style={{
                          display: 'flex',
                          alignItems: 'center',
                          justifyContent: 'space-between',
                          padding: '12px 16px',
                          borderRadius: 12,
                          background: simResult.matched
                            ? simResult.is_vip
                              ? 'rgba(245, 158, 11, 0.15)'
                              : 'rgba(59, 130, 246, 0.15)'
                            : 'rgba(239, 68, 68, 0.15)',
                          border: `1px solid ${
                            simResult.matched
                              ? simResult.is_vip
                                ? 'rgba(245, 158, 11, 0.4)'
                                : 'rgba(59, 130, 246, 0.4)'
                              : 'rgba(239, 68, 68, 0.4)'
                          }`,
                          marginBottom: 16,
                        }}
                      >
                        <div>
                          <div style={{ fontSize: '0.75rem', opacity: 0.8 }}>{t('ui_decision')}</div>
                          <div style={{ fontSize: '1.1rem', fontWeight: 800 }}>
                            {isRtl ? simResult.route_label_fa : simResult.route_label_en}
                          </div>
                        </div>
                        <span
                          className={`badge ${
                            simResult.matched ? (simResult.is_vip ? 'badge-vip' : 'badge-direct') : 'badge-gray'
                          }`}
                        >
                          {simResult.detected_category}
                        </span>
                      </div>

                      <div style={{ marginBottom: 16 }}>
                        <div style={{ fontSize: '0.8rem', color: '#94a3b8', marginBottom: 4 }}>
                          {t('ui_decision_reason')}
                        </div>
                        <div style={{ background: 'rgba(15, 23, 42, 0.6)', padding: '10px 14px', borderRadius: 8, fontSize: '0.85rem' }}>
                          {simResult.reason}
                        </div>
                      </div>

                      {simResult.protected_content_handled && (
                        <div
                          style={{
                            background: 'rgba(245, 158, 11, 0.12)',
                            border: '1px solid rgba(245, 158, 11, 0.3)',
                            borderRadius: 8,
                            padding: '10px 14px',
                            marginBottom: 16,
                            fontSize: '0.85rem',
                            display: 'flex',
                            alignItems: 'center',
                            gap: 8,
                            color: '#fbbf24',
                          }}
                        >
                          <Shield size={16} />
                          <span>
                            {t('ui_bypass_activated_protected_content_will_be_re_uploaded_directly')}
                          </span>
                        </div>
                      )}

                      <div style={{ marginBottom: 16 }}>
                        <div style={{ fontSize: '0.8rem', color: '#94a3b8', marginBottom: 6 }}>
                          {t('ui_pipeline_execution_steps')}
                        </div>
                        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                          {(Array.isArray(simResult?.action_steps) ? simResult.action_steps : []).map((st, idx) => (
                            <div
                              key={idx}
                              style={{
                                background: 'rgba(15, 23, 42, 0.5)',
                                padding: '8px 12px',
                                borderRadius: 6,
                                fontSize: '0.8rem',
                                borderLeft: isRtl ? 'none' : '3px solid #818cf8',
                                borderRight: isRtl ? '3px solid #818cf8' : 'none',
                              }}
                            >
                              {st}
                            </div>
                          ))}
                        </div>
                      </div>

                      {simResult.preview_message && (
                        <div>
                          <div style={{ fontSize: '0.8rem', color: '#94a3b8', marginBottom: 6 }}>
                            {t('ui_output_message_preview')}
                          </div>
                          <pre
                            style={{
                              background: '#090d16',
                              padding: 14,
                              borderRadius: 8,
                              fontSize: '0.8rem',
                              whiteSpace: 'pre-wrap',
                              border: '1px solid rgba(255, 255, 255, 0.08)',
                              color: '#f8fafc',
                              fontFamily: 'inherit',
                            }}
                          >
                            {simResult.preview_message}
                          </pre>
                        </div>
                      )}
                    </div>
                  ) : (
                    <div style={{ textAlign: 'center', padding: '60px 20px', color: 'var(--color-text-muted)' }}>
                      <Activity size={36} style={{ margin: '0 auto 12px', opacity: 0.5 }} />
                      <p>{t('ui_no_simulation_run_yet_click_run_to_evaluate')}</p>
                    </div>
                  )}
                </div>
              </div>

    </>
  )
}
