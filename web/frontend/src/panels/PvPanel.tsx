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
                      <h2 style={{ fontSize: 'var(--type-xl)', fontWeight: 800, display: 'flex', alignItems: 'center', gap: 10 }}>
                        <MessageSquare size={22} color="var(--color-accent-violet)" />
                        <span>{t('ui_ai_pv_assistant')}</span>
                      </h2>
                      <p style={{ fontSize: 'var(--type-sm)', color: 'var(--color-text-muted)', marginTop: 4 }}>
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
                        background: pvConfig?.enabled ? 'var(--color-success-soft)' : 'var(--color-danger-soft)',
                        color: pvConfig?.enabled ? 'var(--color-success)' : 'var(--color-danger)',
                        border: pvConfig?.enabled ? '1px solid color-mix(in oklab, var(--color-success) 40%, transparent)' : '1px solid color-mix(in oklab, var(--color-danger) 40%, transparent)',
                        fontWeight: 700,
                        fontSize: 'var(--type-sm)',
                        display: 'inline-flex',
                        alignItems: 'center',
                      }}
                    >
                      <span
                        style={{
                          width: 8,
                          height: 8,
                          borderRadius: '50%',
                          background: pvConfig?.enabled ? 'var(--color-success)' : 'var(--color-danger)',
                          boxShadow: pvConfig?.enabled ? '0 0 8px var(--color-success)' : 'none',
                          display: 'inline-block',
                          marginInlineEnd: 8,
                        }}
                      />
                      <span>{pvConfig?.enabled ? (t('ui_enabled')) : (t('ui_disabled'))}</span>
                    </button>
                  </div>

                  {/* Persona Preset Buttons */}
                  <div style={{ marginBlockEnd: 'var(--space-4)' }}>
                    <label style={{ display: 'block', fontSize: 'var(--type-sm)', color: 'var(--color-text)', marginBlockEnd: 'var(--space-2)', fontWeight: 600 }}>
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
                          background: pvPreset === 'casual' ? 'color-mix(in oklab, var(--color-accent-violet) 20%, transparent)' : 'var(--color-hover-wash)',
                          color: pvPreset === 'casual' ? 'var(--color-accent-violet)' : 'var(--color-text)',
                          border: pvPreset === 'casual' ? '1px solid color-mix(in oklab, var(--color-accent-violet) 50%, transparent)' : '1px solid var(--color-border)',
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
                          background: pvPreset === 'business' ? 'var(--color-info-soft)' : 'var(--color-hover-wash)',
                          color: pvPreset === 'business' ? 'var(--color-info)' : 'var(--color-text)',
                          border: pvPreset === 'business' ? '1px solid color-mix(in oklab, var(--color-info) 50%, transparent)' : '1px solid var(--color-border)',
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
                          background: pvPreset === 'short' ? 'var(--color-warning-soft)' : 'var(--color-hover-wash)',
                          color: pvPreset === 'short' ? 'var(--color-warning)' : 'var(--color-text)',
                          border: pvPreset === 'short' ? '1px solid color-mix(in oklab, var(--color-warning) 50%, transparent)' : '1px solid var(--color-border)',
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
                  <div style={{ marginBlockEnd: 'var(--space-4)' }}>
                    <label style={{ display: 'block', fontSize: 'var(--type-sm)', color: 'var(--color-text)', marginBottom: 6, fontWeight: 600 }}>
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
                  <div style={{ marginBlockEnd: 'var(--space-4)' }}>
                    <label style={{ display: 'block', fontSize: 'var(--type-sm)', color: 'var(--color-text)', marginBottom: 6 }}>
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
                      <label style={{ display: 'block', fontSize: 'var(--type-xs)', color: 'var(--color-text)', marginBlockEnd: 'var(--space-1)' }}>
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
                      <label style={{ display: 'block', fontSize: 'var(--type-xs)', color: 'var(--color-text)', marginBlockEnd: 'var(--space-1)' }}>
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
                      <label style={{ display: 'block', fontSize: 'var(--type-xs)', color: 'var(--color-text)', marginBlockEnd: 'var(--space-1)' }}>
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
                      <label style={{ display: 'block', fontSize: 'var(--type-xs)', color: 'var(--color-text)', marginBlockEnd: 'var(--space-1)' }}>
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
                    <label style={{ display: 'flex', alignItems: 'center', gap: 8, cursor: 'pointer', fontSize: 'var(--type-sm)', color: 'var(--color-text)' }}>
                      <input
                        type="checkbox"
                        checked={pvConfig?.ignore_bots ?? true}
                        onChange={(e) => setPvConfig((prev) => prev ? { ...prev, ignore_bots: e.target.checked } : null)}
                        style={{ accentColor: 'var(--color-accent-violet)' }}
                      />
                      <span>{t('ui_ignore_bot_messages')}</span>
                    </label>

                    <button
                      type="button"
                      onClick={() => handleSavePV()}
                      disabled={pvSaving}
                      className="btn btn-primary"
                      style={{ background: 'var(--color-accent-violet)', borderColor: 'var(--color-accent-violet)' }}
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
