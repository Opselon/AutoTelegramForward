/**
 * AI PV auto-responder: presets, rules and the saved config.
 */
import { Check, MessageSquare } from 'lucide-react'
import type { DashboardCtx } from '../App'

export default function PvPanel({ d }: { d: DashboardCtx }) {
  const {
    aiConfigs,
    pvConfig,
    setPvConfig,
    pvSaving,
    pvPreset,
    setPvPreset,
    isRtl,
    t,
    handleSavePV,
  } = d
  return (
    <>
              <div className="grid-collapse-1" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(360px, 1fr))', gap: 24 }}>
                {/* Main Controls & Prompt */}
                <div className="glass-panel" style={{ padding: 24 }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
                    <div>
                      <h2 style={{ fontSize: '1.25rem', fontWeight: 800, display: 'flex', alignItems: 'center', gap: 10 }}>
                        <MessageSquare size={22} color="#ec4899" />
                        <span>{t('ui_ai_pv_assistant')}</span>
                      </h2>
                      <p style={{ fontSize: '0.85rem', color: '#94a3b8', marginTop: 4 }}>
                        {t('ui_human_like_conversational_auto_responder_for_telegram_private_me')}
                      </p>
                    </div>

                    {/* Instant On/Off Toggle Button */}
                    <button
                      onClick={() => handleSavePV({ enabled: !pvConfig?.enabled })}
                      disabled={pvSaving}
                      className="btn"
                      style={{
                        padding: '8px 16px',
                        background: pvConfig?.enabled ? 'rgba(16, 185, 129, 0.2)' : 'rgba(239, 68, 68, 0.2)',
                        color: pvConfig?.enabled ? '#34d399' : '#f87171',
                        border: pvConfig?.enabled ? '1px solid rgba(16, 185, 129, 0.4)' : '1px solid rgba(239, 68, 68, 0.4)',
                        fontWeight: 700,
                        fontSize: '0.85rem',
                        display: 'inline-flex',
                        alignItems: 'center',
                      }}
                    >
                      <span
                        style={{
                          width: 8,
                          height: 8,
                          borderRadius: '50%',
                          background: pvConfig?.enabled ? '#10b981' : '#ef4444',
                          boxShadow: pvConfig?.enabled ? '0 0 8px #10b981' : 'none',
                          display: 'inline-block',
                          marginLeft: isRtl ? 8 : 0,
                          marginRight: isRtl ? 0 : 8,
                        }}
                      />
                      <span>{pvConfig?.enabled ? (t('ui_enabled')) : (t('ui_disabled'))}</span>
                    </button>
                  </div>

                  {/* Persona Preset Buttons */}
                  <div style={{ marginBottom: 16 }}>
                    <label style={{ display: 'block', fontSize: '0.8rem', color: '#cbd5e1', marginBottom: 8, fontWeight: 600 }}>
                      {t('ui_persona_tone_presets')}
                    </label>
                    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(130px, 1fr))', gap: 8 }}>
                      <button
                        type="button"
                        onClick={() => {
                          setPvPreset('casual')
                          setPvConfig((prev) => prev ? { ...prev, persona_prompt: t('ui_pv_preset_casual_prompt') } : null)
                        }}
                        className="btn"
                        style={{
                          background: pvPreset === 'casual' ? 'rgba(236, 72, 153, 0.2)' : 'rgba(255, 255, 255, 0.05)',
                          color: pvPreset === 'casual' ? '#f472b6' : '#cbd5e1',
                          border: pvPreset === 'casual' ? '1px solid rgba(236, 72, 153, 0.5)' : '1px solid rgba(255, 255, 255, 0.08)',
                          fontSize: '0.78rem',
                          padding: '8px 10px',
                          textAlign: 'center',
                        }}
                      >
                        {t('ui_pv_preset_casual_label')}
                      </button>
                      <button
                        type="button"
                        onClick={() => {
                          setPvPreset('business')
                          setPvConfig((prev) => prev ? { ...prev, persona_prompt: t('ui_pv_preset_business_prompt') } : null)
                        }}
                        className="btn"
                        style={{
                          background: pvPreset === 'business' ? 'rgba(59, 130, 246, 0.2)' : 'rgba(255, 255, 255, 0.05)',
                          color: pvPreset === 'business' ? '#60a5fa' : '#cbd5e1',
                          border: pvPreset === 'business' ? '1px solid rgba(59, 130, 246, 0.5)' : '1px solid rgba(255, 255, 255, 0.08)',
                          fontSize: '0.78rem',
                          padding: '8px 10px',
                          textAlign: 'center',
                        }}
                      >
                        {t('ui_pv_preset_business_label')}
                      </button>
                      <button
                        type="button"
                        onClick={() => {
                          setPvPreset('short')
                          setPvConfig((prev) => prev ? { ...prev, persona_prompt: t('ui_pv_preset_short_prompt') } : null)
                        }}
                        className="btn"
                        style={{
                          background: pvPreset === 'short' ? 'rgba(245, 158, 11, 0.2)' : 'rgba(255, 255, 255, 0.05)',
                          color: pvPreset === 'short' ? '#fbbf24' : '#cbd5e1',
                          border: pvPreset === 'short' ? '1px solid rgba(245, 158, 11, 0.5)' : '1px solid rgba(255, 255, 255, 0.08)',
                          fontSize: '0.78rem',
                          padding: '8px 10px',
                          textAlign: 'center',
                        }}
                      >
                        {t('ui_pv_preset_short_label')}
                      </button>
                    </div>
                  </div>

                  {/* Persona Prompt Textarea */}
                  <div style={{ marginBottom: 16 }}>
                    <label style={{ display: 'block', fontSize: '0.8rem', color: '#cbd5e1', marginBottom: 6, fontWeight: 600 }}>
                      {t('ui_persona_prompt_instructions')}
                    </label>
                    <textarea
                      rows={9}
                      value={pvConfig?.persona_prompt || ''}
                      onChange={(e) => {
                        setPvPreset('custom')
                        setPvConfig((prev) => prev ? { ...prev, persona_prompt: e.target.value } : null)
                      }}
                      className="input-field"
                      style={{ width: '100%', fontFamily: 'inherit', fontSize: '0.825rem', lineHeight: 1.6 }}
                      placeholder={t('ui_ai_behavior_prompt_for_private_chats')}
                    />
                  </div>

                  {/* Connect to AI Config */}
                  <div style={{ marginBottom: 16 }}>
                    <label style={{ display: 'block', fontSize: '0.8rem', color: '#cbd5e1', marginBottom: 6 }}>
                      {t('ui_linked_ai_model_configuration')}
                    </label>
                    <select
                      value={pvConfig?.ai_config_id || ''}
                      onChange={(e) => setPvConfig((prev) => prev ? { ...prev, ai_config_id: e.target.value || undefined } : null)}
                      className="input-field"
                      style={{ width: '100%' }}
                    >
                      <option value="">{t('ui_system_default')}</option>
                      {aiConfigs.map((ai) => (
                        <option key={ai.id} value={ai.id}>
                          {ai.name} ({ai.provider} - {ai.model})
                        </option>
                      ))}
                    </select>
                  </div>

                  {/* Tuning Settings Grid */}
                  <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(130px, 1fr))', gap: 12, marginBottom: 18 }}>
                    <div>
                      <label style={{ display: 'block', fontSize: '0.75rem', color: '#cbd5e1', marginBottom: 4 }}>
                        {t('ui_min_typing_delay_s')}
                      </label>
                      <input
                        type="number"
                        step="0.5"
                        min="0.5"
                        max="10"
                        value={pvConfig?.typing_delay_min ?? 2.0}
                        onChange={(e) => setPvConfig((prev) => prev ? { ...prev, typing_delay_min: parseFloat(e.target.value) || 1 } : null)}
                        className="input-field"
                        style={{ width: '100%' }}
                      />
                    </div>
                    <div>
                      <label style={{ display: 'block', fontSize: '0.75rem', color: '#cbd5e1', marginBottom: 4 }}>
                        {t('ui_max_typing_delay_s')}
                      </label>
                      <input
                        type="number"
                        step="0.5"
                        min="1"
                        max="15"
                        value={pvConfig?.typing_delay_max ?? 4.5}
                        onChange={(e) => setPvConfig((prev) => prev ? { ...prev, typing_delay_max: parseFloat(e.target.value) || 2 } : null)}
                        className="input-field"
                        style={{ width: '100%' }}
                      />
                    </div>
                    <div>
                      <label style={{ display: 'block', fontSize: '0.75rem', color: '#cbd5e1', marginBottom: 4 }}>
                        {t('ui_cooldown_s')}
                      </label>
                      <input
                        type="number"
                        min="5"
                        max="120"
                        value={pvConfig?.cooldown_seconds ?? 15}
                        onChange={(e) => setPvConfig((prev) => prev ? { ...prev, cooldown_seconds: parseInt(e.target.value) || 10 } : null)}
                        className="input-field"
                        style={{ width: '100%' }}
                      />
                    </div>
                    <div>
                      <label style={{ display: 'block', fontSize: '0.75rem', color: '#cbd5e1', marginBottom: 4 }}>
                        {t('ui_chat_history_limit')}
                      </label>
                      <input
                        type="number"
                        min="1"
                        max="20"
                        value={pvConfig?.history_limit ?? 4}
                        onChange={(e) => setPvConfig((prev) => prev ? { ...prev, history_limit: parseInt(e.target.value) || 4 } : null)}
                        className="input-field"
                        style={{ width: '100%' }}
                      />
                    </div>
                  </div>

                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                    <label style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer', fontSize: '0.8rem', color: '#cbd5e1' }}>
                      <input
                        type="checkbox"
                        checked={pvConfig?.ignore_bots ?? true}
                        onChange={(e) => setPvConfig((prev) => prev ? { ...prev, ignore_bots: e.target.checked } : null)}
                        style={{ accentColor: '#ec4899' }}
                      />
                      <span>{t('ui_ignore_bot_messages')}</span>
                    </label>

                    <button
                      type="button"
                      onClick={() => handleSavePV()}
                      disabled={pvSaving}
                      className="btn btn-primary"
                      style={{ background: '#ec4899', borderColor: '#db2777' }}
                    >
                      <Check size={16} />
                      <span>{pvSaving ? (t('ui_saving')) : (t('ui_save_pv_config'))}</span>
                    </button>
                  </div>
                </div>

              </div>

    </>
  )
}
