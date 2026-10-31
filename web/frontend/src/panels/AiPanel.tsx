/**
 * AI rewrite configuration: providers, prompts, keys and the per-rule
 * override.
 */
import { PromptEditorPanel, ProviderKeyCheck } from '../components/ShellWidgets'
import { api } from '../api'
import { Edit3, Plus, Trash2 } from 'lucide-react'
import { EmptyState } from '../components/ui/Primitives'
import type { DashboardCtx } from '../App'

export default function AiPanel({ d }: { d: DashboardCtx }) {
  const {
    lang,
    aiConfigs,
    promptTemplates,
    setPromptTemplates,
    setIsAiModalOpen,
    setAiFormData,
    t,
    showToast,
    handleDeleteAI,
  } = d
  return (
    <>
              <div>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 20 }}>
                  <div>
                    <h2 style={{ fontSize: '1.2rem', fontWeight: 700 }}>
                      {t('ui_ai_intelligence_rewriting_engines')}
                    </h2>
                    <p style={{ fontSize: 'var(--type-sm)', color: 'var(--color-text-muted)', marginTop: 4 }}>
                      {t('ui_configure_llm_models_for_auto_rewriting_signal_parsing_and_trans')}
                    </p>
                  </div>
                  <button
                    onClick={() => {
                      setAiFormData({
                        provider: 'OPENAI',
                        model: 'gpt-4o',
                        temperature: 0.7,
                        is_enabled: true,
                        target_language: 'fa',
                        system_prompt: 'You are an expert financial signal parser and translator.',
                        user_prompt_template: 'Translate and format this trade signal accurately:\n{text}',
                      })
                      setIsAiModalOpen(true)
                    }}
                    className="btn btn-primary"
                  >
                    <Plus size={16} />
                    <span>{t('ui_new_ai_engine')}</span>
                  </button>
                </div>

                <div className="grid-collapse-1" style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(360px, 1fr))', gap: 20 }}>
                  {(Array.isArray(aiConfigs) ? aiConfigs : []).length === 0 && (
                    <div style={{ gridColumn: '1 / -1' }}>
                      <EmptyState title={t('ui_empty_title')} description={t('ui_empty_body')} />
                    </div>
                  )}
                  {(Array.isArray(aiConfigs) ? aiConfigs : []).map((ai) => (
                    <div key={ai.id} className="glass-panel" style={{ padding: 20 }}>
                      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 12 }}>
                        <div>
                          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                            <h3 style={{ fontSize: '1rem', fontWeight: 700 }}>{ai.name}</h3>
                            <span className="badge badge-vip">{ai.provider}</span>
                          </div>
                          <div style={{ fontSize: '0.8rem', color: 'var(--color-accent-violet)', marginTop: 2, fontFamily: 'monospace' }}>
                            {ai.model}
                          </div>
                        </div>

                        <div style={{ display: 'flex', gap: 6 }}>
                          <button
                            onClick={() => {
                              setAiFormData({ ...ai })
                              setIsAiModalOpen(true)
                            }}
                            className="btn btn-secondary"
                            style={{ padding: '6px' }}
                            aria-label={t('ui_edit')}
                            title={t('ui_edit')}
                          >
                            <Edit3 size={14} aria-hidden="true" />
                          </button>
                          <button
                            onClick={() => handleDeleteAI(ai.id)}
                            className="btn btn-danger"
                            style={{ padding: '6px' }}
                            aria-label={t('ui_delete')}
                            title={t('ui_delete')}
                          >
                            <Trash2 size={14} aria-hidden="true" />
                          </button>
                        </div>
                      </div>

                      <div style={{ fontSize: '0.8rem', color: 'var(--color-text)', display: 'flex', flexDirection: 'column', gap: 6 }}>
                        <div>
                          <span style={{ color: 'var(--color-text-muted)' }}>{t('ui_api_key')}</span>
                          <code>{ai.api_key_masked || (t('ui_none_environment'))}</code>
                        </div>
                        <div style={{ overflowWrap: 'anywhere' }}>
                          <span style={{ color: 'var(--color-text-muted)' }}>
                            {t('ui_endpoint')}
                          </span>
                          <code dir="ltr">{ai.base_url || (t('ui_provider_default'))}</code>
                        </div>
                        <ProviderKeyCheck aiConfigId={ai.id} lang={lang} compact />
                        <div>
                          <span style={{ color: 'var(--color-text-muted)' }}>{t('ui_temperature')}</span>
                          <span>{ai.temperature}</span> | <span style={{ color: 'var(--color-text-muted)' }}>{t('ui_lang')}</span>
                          <span>{ai.target_language}</span>
                        </div>
                        {ai.system_prompt && (
                          <div style={{ background: 'var(--color-surface-inset)', padding: 8, borderRadius: 6, marginTop: 4 }}>
                            <span style={{ color: 'var(--color-text-muted)', fontSize: 'var(--type-xs)', display: 'block' }}>{t('ui_system_prompt')}</span>
                            <span style={{ fontSize: 'var(--type-xs)' }}>{ai.system_prompt}</span>
                          </div>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              </div>


              <PromptEditorPanel
                prompts={Array.isArray(promptTemplates) ? promptTemplates : []}
                aiConfigs={Array.isArray(aiConfigs) ? aiConfigs : []}
                onSaved={async () => setPromptTemplates(await api.getPromptTemplates().catch(() => []))}
                onToast={showToast}
                lang={lang}
              />

    </>
  )
}
