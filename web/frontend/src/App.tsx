import React, { useState, useEffect } from 'react'
import {
  Send,
  Radio,
  Sliders,
  PlayCircle,
  Users,
  BarChart3,
  CheckCircle2,
  AlertTriangle,
  RotateCcw,
  Plus,
  Trash2,
  Edit3,
  Layers,
  Shield,
  Zap,
  Globe,
  ChevronRight,
  Sparkles,
} from 'lucide-react'
import { api } from './api'
import type {
  ForwardRule,
  SaveRuleRequest,
  Session,
  SimulateResponse,
  StatsResponse,
  LogItem,
} from './types'

export function App() {
  const [lang, setLang] = useState<'fa' | 'en'>('fa')
  const [activeTab, setActiveTab] = useState<'rules' | 'simulator' | 'sessions' | 'metrics'>('rules')
  const [rules, setRules] = useState<ForwardRule[]>([])
  const [sessions, setSessions] = useState<Session[]>([])
  const [stats, setStats] = useState<StatsResponse | null>(null)
  const [logs, setLogs] = useState<LogItem[]>([])
  const [loading, setLoading] = useState(false)
  const [toast, setToast] = useState<string | null>(null)

  // Edit / Create Rule Modal
  const [editingRule, setEditingRule] = useState<Partial<SaveRuleRequest> | null>(null)
  const [isModalOpen, setIsModalOpen] = useState(false)

  // Simulator State
  const [simText, setSimText] = useState('💎 VIP SIGNAL: BUY XAUUSD @ 2655 | TP1: 2680 | SL: 2640')
  const [simOriginChatId, setSimOriginChatId] = useState('-1001111111111')
  const [simOriginTitle, setSimOriginTitle] = useState('Forex VIP Channel')
  const [simOriginUser, setSimOriginUser] = useState('vip_forex_signals')
  const [simHasMedia, setSimHasMedia] = useState(false)
  const [simIsProtected, setSimIsProtected] = useState(false)
  const [simSelectedRuleId, setSimSelectedRuleId] = useState<string>('')
  const [simResult, setSimResult] = useState<SimulateResponse | null>(null)
  const [simulating, setSimulating] = useState(false)

  const isRtl = lang === 'fa'

  const showToast = (msg: string) => {
    setToast(msg)
    setTimeout(() => setToast(null), 4000)
  }

  const loadData = async () => {
    setLoading(true)
    try {
      const [r, s, st, l] = await Promise.all([
        api.getRules().catch(() => []),
        api.getSessions().catch(() => []),
        api.getStats().catch(() => null),
        api.getLogs().catch(() => []),
      ])
      setRules(r)
      setSessions(s)
      setStats(st)
      setLogs(l)
      if (r.length > 0 && !simSelectedRuleId) {
        setSimSelectedRuleId(r[0].id)
      }
    } catch (e: any) {
      showToast(e.message || 'Error loading data')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    loadData()
    const timer = setInterval(() => {
      api.getStats().then(setStats).catch(() => {})
    }, 10000)
    return () => clearInterval(timer)
  }, [])

  const handleToggleRule = async (id: string) => {
    try {
      const updated = await api.toggleRule(id)
      setRules((prev) => prev.map((r) => (r.id === id ? updated : r)))
      showToast(isRtl ? 'وضعیت قانون با موفقیت تغییر کرد' : 'Rule status updated')
    } catch (e: any) {
      showToast(e.message)
    }
  }

  const handleDeleteRule = async (id: string) => {
    if (!confirm(isRtl ? 'آیا از حذف این قانون اطمینان دارید؟' : 'Are you sure you want to delete this rule?')) return
    try {
      await api.deleteRule(id)
      setRules((prev) => prev.filter((r) => r.id !== id))
      showToast(isRtl ? 'قانون با موفقیت حذف شد' : 'Rule deleted successfully')
    } catch (e: any) {
      showToast(e.message)
    }
  }

  const handleQuickRouteSwitch = async (ruleId: string, pathType: 'route1_direct' | 'route2_vip_hop' | 'route3_native') => {
    try {
      const updated = await api.quickSetRoutePath(ruleId, {
        path_type: pathType,
      })
      setRules((prev) => prev.map((r) => (r.id === ruleId ? updated : r)))
      const label = pathType === 'route2_vip_hop' ? 'Route 2 (A ➔ C ➔ B)' : pathType === 'route3_native' ? 'Route 3 (Native)' : 'Route 1 (A ➔ B Direct)'
      showToast(isRtl ? `مسیر با موفقیت به ${label} تغییر یافت` : `Switched to ${label}`)
    } catch (e: any) {
      showToast(e.message)
    }
  }

  const handleSaveRule = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!editingRule || !editingRule.source_chat_id || !editingRule.target_chat_id) {
      showToast(isRtl ? 'لطفاً شناسه کانال مبدأ و مقصد را وارد کنید' : 'Please provide source and target chat IDs')
      return
    }
    try {
      const saved = await api.saveRule(editingRule as SaveRuleRequest)
      setRules((prev) => {
        const idx = prev.findIndex((r) => r.id === saved.id)
        if (idx >= 0) {
          const clone = [...prev]
          clone[idx] = saved
          return clone
        }
        return [saved, ...prev]
      })
      setIsModalOpen(false)
      setEditingRule(null)
      showToast(isRtl ? 'تنظیمات قانون با موفقیت ذخیره شد' : 'Rule saved successfully')
    } catch (e: any) {
      showToast(e.message)
    }
  }

  const handleRunSimulation = async () => {
    setSimulating(true)
    try {
      const res = await api.simulate({
        rule_id: simSelectedRuleId || undefined,
        test_text: simText,
        forward_origin_chat_id: simOriginChatId || undefined,
        forward_origin_title: simOriginTitle || undefined,
        forward_origin_username: simOriginUser || undefined,
        has_media: simHasMedia,
        is_protected: simIsProtected,
      })
      setSimResult(res)
    } catch (e: any) {
      showToast(e.message || 'Simulation error')
    } finally {
      setSimulating(false)
    }
  }

  return (
    <div dir={isRtl ? 'rtl' : 'ltr'} style={{ padding: '24px 20px', minHeight: '100vh', display: 'flex', flexDirection: 'column', gap: '20px' }}>
      {/* Toast Notification */}
      {toast && (
        <div style={{
          position: 'fixed',
          top: '24px',
          left: '50%',
          transform: 'translateX(-50%)',
          zIndex: 9999,
          background: 'rgba(15, 23, 42, 0.95)',
          border: '1px solid rgba(99, 102, 241, 0.4)',
          color: '#ffffff',
          padding: '12px 24px',
          borderRadius: '12px',
          boxShadow: '0 8px 32px rgba(0,0,0,0.5)',
          display: 'flex',
          alignItems: 'center',
          gap: '10px',
        }}>
          <Sparkles size={18} color="#818cf8" />
          <span>{toast}</span>
        </div>
      )}

      {/* Top Navigation Bar */}
      <header className="glass-panel" style={{ padding: '16px 24px', display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: '16px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '14px' }}>
          <div style={{
            width: '44px',
            height: '44px',
            borderRadius: '12px',
            background: 'linear-gradient(135deg, #6366f1 0%, #a855f7 100%)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            boxShadow: '0 4px 16px rgba(99, 102, 241, 0.4)',
          }}>
            <Send size={22} color="#ffffff" />
          </div>
          <div>
            <h1 style={{ fontSize: '1.25rem', fontWeight: 800, color: '#f8fafc', margin: 0 }}>
              {isRtl ? 'سیستم هوشمند فوروارد و مسیریابی تلگرام' : 'AutoTelegramForward Pro'}
            </h1>
            <p style={{ fontSize: '0.8rem', color: '#94a3b8', margin: 0 }}>
              {isRtl ? 'میکروسرویس Rust + Axum / فرانت‌اند React + TypeScript' : 'Rust + Axum Microservice / React + TS'}
            </p>
          </div>
        </div>

        {/* Status Indicators */}
        <div style={{ display: 'flex', alignItems: 'center', gap: '12px', flexWrap: 'wrap' }}>
          <div className="badge badge-gray" style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
            <span style={{ width: '8px', height: '8px', borderRadius: '50%', background: stats?.atf_core_online ? '#10b981' : '#ef4444' }} />
            <span>Core gRPC (6001): {stats?.atf_core_online ? (isRtl ? 'آنلاین' : 'Online') : (isRtl ? 'آفلاین' : 'Offline')}</span>
          </div>

          <div className="badge badge-gray" style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
            <span style={{ width: '8px', height: '8px', borderRadius: '50%', background: '#10b981' }} />
            <span>Web Axum: {isRtl ? 'فعال (۸۰۸۸)' : 'Active (:8088)'}</span>
          </div>

          {/* Language Switcher */}
          <button
            className="btn btn-secondary"
            onClick={() => setLang(lang === 'fa' ? 'en' : 'fa')}
            style={{ padding: '6px 12px', fontSize: '0.8rem' }}
          >
            <Globe size={14} />
            <span>{lang === 'fa' ? 'English' : 'فارسی'}</span>
          </button>

          <button className="btn btn-secondary" onClick={loadData} style={{ padding: '6px 12px' }}>
            <RotateCcw size={14} className={loading ? 'animate-spin' : ''} />
          </button>
        </div>
      </header>

      {/* Main Tabs Navigation */}
      <nav style={{ display: 'flex', gap: '10px', flexWrap: 'wrap' }}>
        <button
          className={`btn ${activeTab === 'rules' ? 'btn-primary' : 'btn-secondary'}`}
          onClick={() => setActiveTab('rules')}
          style={{ flex: 1, minWidth: '160px', padding: '12px' }}
        >
          <Sliders size={18} />
          <span>{isRtl ? 'مدیریت قوانین و روتینگ هوشمند' : 'Smart Routing & Rules'}</span>
          <span style={{ background: 'rgba(255,255,255,0.2)', padding: '2px 8px', borderRadius: '99px', fontSize: '0.75rem' }}>
            {rules.length}
          </span>
        </button>

        <button
          className={`btn ${activeTab === 'simulator' ? 'btn-primary' : 'btn-secondary'}`}
          onClick={() => setActiveTab('simulator')}
          style={{ flex: 1, minWidth: '160px', padding: '12px' }}
        >
          <PlayCircle size={18} />
          <span>{isRtl ? 'آزمایشگاه و شبیه‌ساز زنده (Dry Run)' : 'Live Routing Simulator'}</span>
          <span className="badge badge-vip" style={{ fontSize: '0.7rem' }}>VIP Lab</span>
        </button>

        <button
          className={`btn ${activeTab === 'sessions' ? 'btn-primary' : 'btn-secondary'}`}
          onClick={() => setActiveTab('sessions')}
          style={{ flex: 1, minWidth: '140px', padding: '12px' }}
        >
          <Users size={18} />
          <span>{isRtl ? 'کلاینت‌ها و نشست‌ها' : 'Userbot Sessions'}</span>
          <span style={{ background: 'rgba(255,255,255,0.2)', padding: '2px 8px', borderRadius: '99px', fontSize: '0.75rem' }}>
            {sessions.length}
          </span>
        </button>

        <button
          className={`btn ${activeTab === 'metrics' ? 'btn-primary' : 'btn-secondary'}`}
          onClick={() => setActiveTab('metrics')}
          style={{ flex: 1, minWidth: '140px', padding: '12px' }}
        >
          <BarChart3 size={18} />
          <span>{isRtl ? 'آمار، صف و لاگ خطاها' : 'Metrics & Forensics'}</span>
        </button>
      </nav>

      {/* TAB 1: RULES MANAGEMENT */}
      {activeTab === 'rules' && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '20px' }}>
          {/* Quick Stats Grid */}
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: '16px' }}>
            <div className="glass-panel" style={{ padding: '18px 20px' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <span style={{ color: '#94a3b8', fontSize: '0.85rem' }}>{isRtl ? 'کل قوانین فوروارد' : 'Total Rules'}</span>
                <Sliders size={20} color="#818cf8" />
              </div>
              <div style={{ fontSize: '1.75rem', fontWeight: 800, marginTop: '8px' }}>{rules.length}</div>
            </div>

            <div className="glass-panel" style={{ padding: '18px 20px' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <span style={{ color: '#94a3b8', fontSize: '0.85rem' }}>{isRtl ? 'مسیر ۲: فوروارد VIP از واسط C' : 'Route 2: VIP Hop (A->C->B)'}</span>
                <Radio size={20} color="#fbbf24" />
              </div>
              <div style={{ fontSize: '1.75rem', fontWeight: 800, color: '#fbbf24', marginTop: '8px' }}>
                {rules.filter((r) => r.use_intermediate && r.intermediate_channel_id).length}
              </div>
            </div>

            <div className="glass-panel" style={{ padding: '18px 20px' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <span style={{ color: '#94a3b8', fontSize: '0.85rem' }}>{isRtl ? 'مسیر ۱: ارسال مستقیم و تمیز' : 'Route 1: Direct Clean (A->B)'}</span>
                <Zap size={20} color="#60a5fa" />
              </div>
              <div style={{ fontSize: '1.75rem', fontWeight: 800, color: '#60a5fa', marginTop: '8px' }}>
                {rules.filter((r) => !r.use_intermediate && r.forward_mode !== 'DIRECT_FORWARD').length}
              </div>
            </div>

            <div className="glass-panel" style={{ padding: '18px 20px' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <span style={{ color: '#94a3b8', fontSize: '0.85rem' }}>{isRtl ? 'مسیر ۳: فوروارد رسمی نیتیو' : 'Route 3: Native Forward'}</span>
                <Send size={20} color="#34d399" />
              </div>
              <div style={{ fontSize: '1.75rem', fontWeight: 800, color: '#34d399', marginTop: '8px' }}>
                {rules.filter((r) => !r.use_intermediate && r.forward_mode === 'DIRECT_FORWARD').length}
              </div>
            </div>
          </div>

          {/* Action Row */}
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: '12px' }}>
            <h2 style={{ fontSize: '1.15rem', fontWeight: 700, margin: 0 }}>
              {isRtl ? 'لیست قوانین فعال مسیریابی' : 'Active Routing Rules'}
            </h2>
            <button
              className="btn btn-primary"
              onClick={() => {
                setEditingRule({
                  source_chat_id: '',
                  source_chat_name: '',
                  target_chat_id: '',
                  target_chat_name: '',
                  routing_type: 'CHANNEL_TO_CHANNEL',
                  forward_mode: 'COPY_MESSAGE',
                  message_category: 'ALL',
                  use_intermediate: false,
                  intermediate_channel_id: '',
                  intermediate_channel_name: '',
                  priority: 10,
                  fallback_mode: 'COPY_MESSAGE',
                  fallback_enabled: true,
                  split_long_caption: true,
                  detection_criteria: {
                    match_mode: 'ANY',
                    text_contains: ['VIP', 'SIGNAL'],
                    regex_pattern: '',
                  },
                })
                setIsModalOpen(true)
              }}
            >
              <Plus size={18} />
              <span>{isRtl ? 'تعریف قانون هوشمند جدید' : 'Create New Rule'}</span>
            </button>
          </div>

          {/* Rules List */}
          {rules.length === 0 ? (
            <div className="glass-panel" style={{ padding: '40px', textAlign: 'center', color: '#94a3b8' }}>
              <p>{isRtl ? 'هیچ قانونی یافت نشد. برای شروع دکمه «تعریف قانون جدید» را بزنید.' : 'No forward rules found. Click Create New Rule to begin.'}</p>
            </div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
              {rules.map((rule) => {
                const isVipHop = rule.use_intermediate && rule.intermediate_channel_id && (rule.message_category === 'VIP' || rule.message_category === 'VIP_ONLY')
                const isNative = !rule.use_intermediate && rule.forward_mode === 'DIRECT_FORWARD'
                const criteria = rule.detection_criteria || {}
                const keywords = criteria.text_contains || criteria.keywords || []
                const regexPattern = criteria.regex_pattern || criteria.text_regex || ''
                const matchMode = criteria.match_mode || 'ANY'

                return (
                  <div
                    key={rule.id}
                    className="glass-panel"
                    style={{
                      padding: '20px 24px',
                      display: 'flex',
                      flexDirection: 'column',
                      gap: '16px',
                      borderLeft: isRtl ? undefined : isVipHop ? '4px solid #f59e0b' : isNative ? '4px solid #10b981' : '4px solid #3b82f6',
                      borderRight: isRtl ? (isVipHop ? '4px solid #f59e0b' : isNative ? '4px solid #10b981' : '4px solid #3b82f6') : undefined,
                    }}
                  >
                    {/* Top Row: Route Visualization and Badges */}
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', flexWrap: 'wrap', gap: '12px' }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '12px', flexWrap: 'wrap' }}>
                        {/* Source A */}
                        <div style={{ background: 'rgba(255,255,255,0.05)', padding: '6px 14px', borderRadius: '8px', border: '1px solid rgba(255,255,255,0.1)' }}>
                          <div style={{ fontSize: '0.75rem', color: '#94a3b8' }}>{isRtl ? 'کانال مبدأ A' : 'Source A'}</div>
                          <div style={{ fontWeight: 700, fontSize: '0.95rem' }}>{rule.source_chat_name || rule.source_chat_id}</div>
                        </div>

                        <ChevronRight size={18} color="#94a3b8" />

                        {/* Intermediate C if enabled */}
                        {isVipHop && (
                          <>
                            <div style={{ background: 'rgba(245, 158, 11, 0.15)', padding: '6px 14px', borderRadius: '8px', border: '1px solid rgba(245, 158, 11, 0.4)' }}>
                              <div style={{ fontSize: '0.75rem', color: '#fbbf24', display: 'flex', alignItems: 'center', gap: '4px' }}>
                                <Sparkles size={12} />
                                <span>{isRtl ? 'واسط برندینگ C' : 'Intermediate C'}</span>
                              </div>
                              <div style={{ fontWeight: 700, fontSize: '0.95rem', color: '#fbbf24' }}>
                                {rule.intermediate_channel_name || rule.intermediate_channel_id}
                              </div>
                            </div>
                            <ChevronRight size={18} color="#94a3b8" />
                          </>
                        )}

                        {/* Destination B */}
                        <div style={{ background: 'rgba(255,255,255,0.05)', padding: '6px 14px', borderRadius: '8px', border: '1px solid rgba(255,255,255,0.1)' }}>
                          <div style={{ fontSize: '0.75rem', color: '#94a3b8' }}>{isRtl ? 'مقصد نهایی B' : 'Target B'}</div>
                          <div style={{ fontWeight: 700, fontSize: '0.95rem' }}>{rule.target_chat_name || rule.target_chat_id}</div>
                        </div>
                      </div>

                      {/* Route Mode Badge */}
                      <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                        {isVipHop ? (
                          <span className="badge badge-vip">
                            <Sparkles size={14} />
                            <span>{isRtl ? 'مسیر ۲: واسط VIP (A ➔ C ➔ B)' : 'Route 2: VIP Hop'}</span>
                          </span>
                        ) : isNative ? (
                          <span className="badge badge-native">
                            <Send size={14} />
                            <span>{isRtl ? 'مسیر ۳: فوروارد نیتیو (A ➔ B)' : 'Route 3: Native Forward'}</span>
                          </span>
                        ) : (
                          <span className="badge badge-direct">
                            <Zap size={14} />
                            <span>{isRtl ? 'مسیر ۱: مستقیم و تمیز (A ➔ B)' : 'Route 1: Direct Clean'}</span>
                          </span>
                        )}

                        <span className={`badge ${rule.is_active ? 'badge-native' : 'badge-danger'}`}>
                          {rule.is_active ? (isRtl ? 'فعال' : 'Active') : (isRtl ? 'غیرفعال' : 'Inactive')}
                        </span>
                      </div>
                    </div>

                    {/* Middle Row: One-Click Quick Route Switcher */}
                    <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap', background: 'rgba(0,0,0,0.25)', padding: '10px 14px', borderRadius: '8px' }}>
                      <span style={{ fontSize: '0.8rem', color: '#94a3b8', fontWeight: 600 }}>
                        {isRtl ? 'تغییر آنی مسیر:' : 'Switch Route:'}
                      </span>
                      <button
                        className={`btn ${!rule.use_intermediate && !isNative ? 'btn-primary' : 'btn-secondary'}`}
                        style={{ padding: '4px 12px', fontSize: '0.8rem' }}
                        onClick={() => handleQuickRouteSwitch(rule.id, 'route1_direct')}
                      >
                        {isRtl ? '📋 مسیر ۱ (مستقیم A ➔ B)' : 'Route 1 (Direct)'}
                      </button>
                      <button
                        className={`btn ${isVipHop ? 'btn-vip' : 'btn-secondary'}`}
                        style={{ padding: '4px 12px', fontSize: '0.8rem' }}
                        onClick={() => handleQuickRouteSwitch(rule.id, 'route2_vip_hop')}
                      >
                        {isRtl ? '💎 مسیر ۲ (واسط VIP A ➔ C ➔ B)' : 'Route 2 (VIP Hop)'}
                      </button>
                      <button
                        className={`btn ${isNative ? 'btn-primary' : 'btn-secondary'}`}
                        style={{ padding: '4px 12px', fontSize: '0.8rem', background: isNative ? '#10b981' : undefined }}
                        onClick={() => handleQuickRouteSwitch(rule.id, 'route3_native')}
                      >
                        {isRtl ? '↗️ مسیر ۳ (فوروارد رسمی)' : 'Route 3 (Native)'}
                      </button>
                    </div>

                    {/* Details Row: VIP conditions, regex, priority */}
                    <div style={{ display: 'flex', alignItems: 'center', gap: '12px', flexWrap: 'wrap', fontSize: '0.85rem', color: '#cbd5e1' }}>
                      <div>
                        <span style={{ color: '#94a3b8' }}>{isRtl ? 'منطق شروط:' : 'Match Mode:'} </span>
                        <code>{matchMode}</code>
                      </div>

                      {keywords.length > 0 && (
                        <div>
                          <span style={{ color: '#94a3b8' }}>{isRtl ? 'کلیدواژه‌ها:' : 'Keywords:'} </span>
                          {keywords.map((k: string, i: number) => (
                            <span key={i} className="badge badge-gray" style={{ margin: '0 3px', fontSize: '0.75rem' }}>
                              {k}
                            </span>
                          ))}
                        </div>
                      )}

                      {regexPattern && (
                        <div>
                          <span style={{ color: '#94a3b8' }}>Regex: </span>
                          <code>{regexPattern}</code>
                        </div>
                      )}

                      <div>
                        <span style={{ color: '#94a3b8' }}>{isRtl ? 'اولویت:' : 'Priority:'} </span>
                        <strong>{rule.priority}</strong>
                      </div>

                      <div style={{ display: 'flex', alignItems: 'center', gap: '4px', color: '#34d399' }}>
                        <Shield size={14} />
                        <span>{isRtl ? 'Clean Copy & Re-upload در صورت قفل بودن کانال' : 'Clean Copy & Re-upload Protected Bypass'}</span>
                      </div>
                    </div>

                    {/* Bottom Action Buttons */}
                    <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '10px', marginTop: '6px' }}>
                      <button
                        className="btn btn-secondary"
                        style={{ padding: '6px 14px' }}
                        onClick={() => {
                          setSimSelectedRuleId(rule.id)
                          setActiveTab('simulator')
                        }}
                      >
                        <PlayCircle size={15} color="#818cf8" />
                        <span>{isRtl ? 'تست در شبیه‌ساز' : 'Test in Simulator'}</span>
                      </button>

                      <button
                        className="btn btn-secondary"
                        style={{ padding: '6px 14px' }}
                        onClick={() => handleToggleRule(rule.id)}
                      >
                        <span>{rule.is_active ? (isRtl ? 'غیرفعال‌سازی' : 'Deactivate') : (isRtl ? 'فعال‌سازی' : 'Activate')}</span>
                      </button>

                      <button
                        className="btn btn-secondary"
                        style={{ padding: '6px 14px' }}
                        onClick={() => {
                          setEditingRule({ ...rule })
                          setIsModalOpen(true)
                        }}
                      >
                        <Edit3 size={15} />
                        <span>{isRtl ? 'ویرایش کامل' : 'Edit Rule'}</span>
                      </button>

                      <button
                        className="btn btn-danger"
                        style={{ padding: '6px 14px' }}
                        onClick={() => handleDeleteRule(rule.id)}
                      >
                        <Trash2 size={15} />
                      </button>
                    </div>
                  </div>
                )
              })}
            </div>
          )}
        </div>
      )}

      {/* TAB 2: LIVE SIMULATOR & LAB */}
      {activeTab === 'simulator' && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(360px, 1fr))', gap: '24px' }}>
          {/* Controls Panel */}
          <div className="glass-panel" style={{ padding: '24px', display: 'flex', flexDirection: 'column', gap: '18px' }}>
            <h2 style={{ fontSize: '1.2rem', fontWeight: 700, margin: 0, display: 'flex', alignItems: 'center', gap: '10px' }}>
              <PlayCircle size={22} color="#6366f1" />
              <span>{isRtl ? 'آزمایشگاه و شبیه‌ساز زنده روتینگ' : 'Live Routing Simulator & Lab'}</span>
            </h2>
            <p style={{ fontSize: '0.85rem', color: '#94a3b8', margin: 0 }}>
              {isRtl
                ? 'پیام نمونه را وارد کنید تا موتور روتینگ بدون ارسال واقعی به تلگرام، مسیر پیام را شبیه‌سازی و بررسی کند.'
                : 'Test incoming messages to inspect decision rules, VIP provenance checks, and path routing before sending.'}
            </p>

            {/* Target Rule Selector */}
            <div>
              <label style={{ display: 'block', fontSize: '0.85rem', color: '#cbd5e1', marginBottom: '6px' }}>
                {isRtl ? 'قانون مورد ارزیابی:' : 'Target Rule to Evaluate:'}
              </label>
              <select
                className="input-field"
                value={simSelectedRuleId}
                onChange={(e) => setSimSelectedRuleId(e.target.value)}
              >
                {rules.map((r) => (
                  <option key={r.id} value={r.id}>
                    {r.source_chat_name || r.source_chat_id} ➔ {r.target_chat_name || r.target_chat_id} ({r.use_intermediate ? 'VIP Hop C' : r.forward_mode})
                  </option>
                ))}
              </select>
            </div>

            {/* Simulated Message Text */}
            <div>
              <label style={{ display: 'block', fontSize: '0.85rem', color: '#cbd5e1', marginBottom: '6px' }}>
                {isRtl ? 'متن پیام ورودی (Text):' : 'Incoming Message Text:'}
              </label>
              <textarea
                className="input-field"
                rows={4}
                value={simText}
                onChange={(e) => setSimText(e.target.value)}
                placeholder="Message content..."
              />
            </div>

            {/* Forward Origin Metadata Simulation */}
            <div style={{ background: 'rgba(0,0,0,0.3)', padding: '14px', borderRadius: '10px', display: 'flex', flexDirection: 'column', gap: '10px' }}>
              <div style={{ fontSize: '0.85rem', fontWeight: 700, color: '#f8fafc' }}>
                {isRtl ? 'متادیتای فوروارد تلگرام (Forward Provenance):' : 'Telegram Forward Metadata:'}
              </div>

              <div>
                <label style={{ fontSize: '0.75rem', color: '#94a3b8' }}>
                  {isRtl ? 'شناسه کانال مبدأ فوروارد (from_chat_id):' : 'Forward Origin Chat ID:'}
                </label>
                <input
                  type="text"
                  className="input-field"
                  value={simOriginChatId}
                  onChange={(e) => setSimOriginChatId(e.target.value)}
                  placeholder="-100..."
                />
              </div>

              <div>
                <label style={{ fontSize: '0.75rem', color: '#94a3b8' }}>
                  {isRtl ? 'عنوان کانال فورواردکننده:' : 'Forward Origin Title:'}
                </label>
                <input
                  type="text"
                  className="input-field"
                  value={simOriginTitle}
                  onChange={(e) => setSimOriginTitle(e.target.value)}
                  placeholder="Channel Title..."
                />
              </div>

              <div>
                <label style={{ fontSize: '0.75rem', color: '#94a3b8' }}>
                  {isRtl ? 'یوزرنیم منشأ فوروارد (@username):' : 'Forward Origin Username:'}
                </label>
                <input
                  type="text"
                  className="input-field"
                  value={simOriginUser}
                  onChange={(e) => setSimOriginUser(e.target.value)}
                  placeholder="username..."
                />
              </div>
            </div>

            {/* Special Conditions Toggles */}
            <div style={{ display: 'flex', gap: '18px', flexWrap: 'wrap' }}>
              <label style={{ display: 'flex', alignItems: 'center', gap: '8px', cursor: 'pointer', fontSize: '0.85rem' }}>
                <input
                  type="checkbox"
                  checked={simHasMedia}
                  onChange={(e) => setSimHasMedia(e.target.checked)}
                />
                <span>{isRtl ? 'شامل مدیا (تصویر/ویدئو)' : 'Has Media Attached'}</span>
              </label>

              <label style={{ display: 'flex', alignItems: 'center', gap: '8px', cursor: 'pointer', fontSize: '0.85rem' }}>
                <input
                  type="checkbox"
                  checked={simIsProtected}
                  onChange={(e) => setSimIsProtected(e.target.checked)}
                />
                <span>{isRtl ? 'کانال دارای قفل فوروارد (Protected Content)' : 'Protected / Restricted Channel'}</span>
              </label>
            </div>

            {/* Simulation Trigger Button */}
            <button
              className="btn btn-primary"
              style={{ padding: '12px', fontSize: '0.95rem' }}
              onClick={handleRunSimulation}
              disabled={simulating}
            >
              <PlayCircle size={18} />
              <span>{simulating ? (isRtl ? 'در حال بررسی...' : 'Evaluating...') : (isRtl ? 'اجرای شبیه‌سازی زنده' : 'Run Simulation')}</span>
            </button>
          </div>

          {/* Results Panel */}
          <div className="glass-panel" style={{ padding: '24px', display: 'flex', flexDirection: 'column', gap: '20px' }}>
            <h2 style={{ fontSize: '1.2rem', fontWeight: 700, margin: 0 }}>
              {isRtl ? 'تحلیل و نتایج شبیه‌سازی' : 'Simulation Analysis & Route Decision'}
            </h2>

            {!simResult ? (
              <div style={{ padding: '60px 20px', textAlign: 'center', color: '#94a3b8' }}>
                <Layers size={40} style={{ margin: '0 auto 12px', opacity: 0.5 }} />
                <p>{isRtl ? 'دکمه «اجرای شبیه‌سازی زنده» را بزنید تا خروجی موتور اینجا نمایش داده شود.' : 'Click "Run Simulation" to inspect decision pipeline.'}</p>
              </div>
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '18px' }}>
                {/* Status and Decision Badge */}
                <div style={{
                  padding: '16px',
                  borderRadius: '12px',
                  background: simResult.matched ? 'rgba(16, 185, 129, 0.1)' : 'rgba(239, 68, 68, 0.1)',
                  border: `1px solid ${simResult.matched ? 'rgba(16, 185, 129, 0.3)' : 'rgba(239, 68, 68, 0.3)'}`,
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'space-between',
                  flexWrap: 'wrap',
                  gap: '12px',
                }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                    {simResult.matched ? (
                      <CheckCircle2 size={24} color="#10b981" />
                    ) : (
                      <AlertTriangle size={24} color="#ef4444" />
                    )}
                    <div>
                      <div style={{ fontWeight: 800, fontSize: '1rem', color: simResult.matched ? '#34d399' : '#f87171' }}>
                        {simResult.matched ? (isRtl ? 'تطبیق موفق با قانون' : 'Rule Matched Successfully') : (isRtl ? 'عدم تطبیق با قانون' : 'Rule Mismatch')}
                      </div>
                      <div style={{ fontSize: '0.8rem', color: '#cbd5e1' }}>{simResult.reason}</div>
                    </div>
                  </div>

                  <div className={`badge ${simResult.decision === 'BRANDING_VIA_C' ? 'badge-vip' : simResult.decision === 'DIRECT_FORWARD' ? 'badge-native' : 'badge-direct'}`} style={{ fontSize: '0.85rem', padding: '6px 14px' }}>
                    {isRtl ? simResult.route_label_fa : simResult.route_label_en}
                  </div>
                </div>

                {/* Steps Breakdown */}
                <div>
                  <h3 style={{ fontSize: '0.95rem', fontWeight: 700, marginBottom: '8px' }}>
                    {isRtl ? 'مراحل اجرای خط لوله (Pipeline Steps):' : 'Pipeline Execution Steps:'}
                  </h3>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                    {simResult.action_steps.map((step, idx) => (
                      <div key={idx} style={{ background: 'rgba(255,255,255,0.04)', padding: '10px 14px', borderRadius: '8px', fontSize: '0.85rem' }}>
                        {step}
                      </div>
                    ))}
                  </div>
                </div>

                {/* Protected Bypass Status */}
                {simResult.protected_content_handled && (
                  <div style={{ background: 'rgba(16, 185, 129, 0.15)', border: '1px solid rgba(16, 185, 129, 0.3)', padding: '12px', borderRadius: '8px', display: 'flex', alignItems: 'center', gap: '10px' }}>
                    <Shield size={18} color="#34d399" />
                    <span style={{ fontSize: '0.85rem', color: '#a7f3d0' }}>
                      {isRtl ? 'محدودیت فوروارد و قفل کانال با متد Clean Re-upload دور زده شد.' : 'Protected content restriction resolved via Clean Re-upload.'}
                    </span>
                  </div>
                )}

                {/* Live Message Preview */}
                <div>
                  <h3 style={{ fontSize: '0.95rem', fontWeight: 700, marginBottom: '8px' }}>
                    {isRtl ? 'پیش‌نمایش پیام تحویل‌شده در مقصد B:' : 'Delivered Message Preview at Target B:'}
                  </h3>
                  <div style={{
                    background: '#070a13',
                    border: '1px solid rgba(255,255,255,0.1)',
                    borderRadius: '10px',
                    padding: '16px',
                    fontFamily: 'monospace',
                    fontSize: '0.85rem',
                    whiteSpace: 'pre-wrap',
                    color: '#e2e8f0',
                  }}>
                    {simResult.preview_message}
                  </div>
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      {/* TAB 3: SESSIONS */}
      {activeTab === 'sessions' && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '20px' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <h2 style={{ fontSize: '1.2rem', fontWeight: 700, margin: 0 }}>
              {isRtl ? 'نشست‌های کلاینت تلگرام (Userbot Sessions)' : 'Telegram Client Sessions'}
            </h2>
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))', gap: '16px' }}>
            {sessions.map((s) => (
              <div key={s.id} className="glass-panel" style={{ padding: '20px', display: 'flex', flexDirection: 'column', gap: '12px' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                  <div style={{ fontWeight: 700, fontSize: '1.05rem' }}>{s.first_name || s.phone_number}</div>
                  <span className={`badge ${s.is_active ? 'badge-native' : 'badge-danger'}`}>
                    {s.is_active ? (isRtl ? 'متصل' : 'Active') : (isRtl ? 'قطع' : 'Inactive')}
                  </span>
                </div>
                <div style={{ fontSize: '0.85rem', color: '#94a3b8' }}>
                  <div>{isRtl ? 'شماره:' : 'Phone:'} <code>{s.phone_number}</code></div>
                  <div>{isRtl ? 'شناسه کاربر:' : 'User ID:'} <code>{s.user_id}</code></div>
                  <div>{isRtl ? 'نام کاربری:' : 'Username:'} <code>@{s.username || 'none'}</code></div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* TAB 4: METRICS & FORENSICS */}
      {activeTab === 'metrics' && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '20px' }}>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: '16px' }}>
            <div className="glass-panel" style={{ padding: '18px 20px' }}>
              <span style={{ color: '#94a3b8', fontSize: '0.85rem' }}>{isRtl ? 'پیام‌های فورواردشده (۲۴ ساعت)' : 'Forwarded 24h'}</span>
              <div style={{ fontSize: '1.75rem', fontWeight: 800, color: '#34d399', marginTop: '8px' }}>
                {stats?.total_forwarded_24h || 0}
              </div>
            </div>

            <div className="glass-panel" style={{ padding: '18px 20px' }}>
              <span style={{ color: '#94a3b8', fontSize: '0.85rem' }}>{isRtl ? 'خطاهای پردازش (۲۴ ساعت)' : 'Errors 24h'}</span>
              <div style={{ fontSize: '1.75rem', fontWeight: 800, color: '#f87171', marginTop: '8px' }}>
                {stats?.total_errors_24h || 0}
              </div>
            </div>

            <div className="glass-panel" style={{ padding: '18px 20px' }}>
              <span style={{ color: '#94a3b8', fontSize: '0.85rem' }}>{isRtl ? 'صف تحویل در انتظار (Pending)' : 'Queue Jobs Pending'}</span>
              <div style={{ fontSize: '1.75rem', fontWeight: 800, color: '#60a5fa', marginTop: '8px' }}>
                {stats?.queue_jobs_pending || 0}
              </div>
            </div>

            <div className="glass-panel" style={{ padding: '18px 20px' }}>
              <span style={{ color: '#94a3b8', fontSize: '0.85rem' }}>{isRtl ? 'صف شکست‌خورده (Failed / DLQ)' : 'Queue Jobs Failed'}</span>
              <div style={{ fontSize: '1.75rem', fontWeight: 800, color: '#fbbf24', marginTop: '8px' }}>
                {stats?.queue_jobs_failed || 0}
              </div>
            </div>
          </div>

          {/* Forensics Log Table */}
          <div className="glass-panel" style={{ padding: '20px', display: 'flex', flexDirection: 'column', gap: '14px' }}>
            <h3 style={{ fontSize: '1rem', fontWeight: 700, margin: 0 }}>
              {isRtl ? 'گزارش آخرین خطاها و وقایع سیستم (Error Forensics)' : 'Recent Error Forensics Log'}
            </h3>

            {logs.length === 0 ? (
              <p style={{ color: '#94a3b8', fontSize: '0.85rem' }}>{isRtl ? 'هیچ خطایی ثبت نشده است. سیستم کاملاً پایدار است.' : 'No error entries found. System running smooth.'}</p>
            ) : (
              <div style={{ overflowX: 'auto' }}>
                <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.85rem' }}>
                  <thead>
                    <tr style={{ borderBottom: '1px solid rgba(255,255,255,0.1)', color: '#94a3b8' }}>
                      <th style={{ padding: '8px', textAlign: isRtl ? 'right' : 'left' }}>Time</th>
                      <th style={{ padding: '8px', textAlign: isRtl ? 'right' : 'left' }}>Severity</th>
                      <th style={{ padding: '8px', textAlign: isRtl ? 'right' : 'left' }}>Category</th>
                      <th style={{ padding: '8px', textAlign: isRtl ? 'right' : 'left' }}>Error Name</th>
                      <th style={{ padding: '8px', textAlign: isRtl ? 'right' : 'left' }}>Detail</th>
                    </tr>
                  </thead>
                  <tbody>
                    {logs.map((item) => (
                      <tr key={item.id} style={{ borderBottom: '1px solid rgba(255,255,255,0.05)' }}>
                        <td style={{ padding: '8px' }}>{new Date(item.ts * 1000).toLocaleTimeString()}</td>
                        <td style={{ padding: '8px' }}>
                          <span className={`badge ${item.severity === 'fatal' || item.severity === 'error' ? 'badge-danger' : 'badge-gray'}`}>
                            {item.severity}
                          </span>
                        </td>
                        <td style={{ padding: '8px' }}><code>{item.category}</code></td>
                        <td style={{ padding: '8px', color: '#f87171' }}>{item.error_name}</td>
                        <td style={{ padding: '8px', color: '#cbd5e1' }}>{item.detail}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      )}

      {/* RULE EDIT / CREATE MODAL */}
      {isModalOpen && editingRule && (
        <div style={{
          position: 'fixed',
          top: 0,
          left: 0,
          right: 0,
          bottom: 0,
          background: 'rgba(0,0,0,0.75)',
          backdropFilter: 'blur(8px)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          padding: '20px',
          zIndex: 10000,
        }}>
          <div className="glass-panel" style={{
            background: '#0f172a',
            width: '100%',
            maxWidth: '680px',
            maxHeight: '90vh',
            overflowY: 'auto',
            padding: '28px',
            borderRadius: '16px',
            display: 'flex',
            flexDirection: 'column',
            gap: '18px',
          }}>
            <h2 style={{ fontSize: '1.25rem', fontWeight: 800, margin: 0 }}>
              {editingRule.id ? (isRtl ? 'ویرایش قانون هوشمند' : 'Edit Forward Rule') : (isRtl ? 'تعریف قانون جدید' : 'Create Forward Rule')}
            </h2>

            <form onSubmit={handleSaveRule} style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
              {/* Source & Target Chat IDs */}
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px' }}>
                <div>
                  <label style={{ display: 'block', fontSize: '0.8rem', color: '#94a3b8', marginBottom: '4px' }}>
                    {isRtl ? 'شناسه کانال مبدأ A:' : 'Source Chat ID A:'}
                  </label>
                  <input
                    type="text"
                    required
                    className="input-field"
                    value={editingRule.source_chat_id || ''}
                    onChange={(e) => setEditingRule({ ...editingRule, source_chat_id: e.target.value })}
                    placeholder="-100..."
                  />
                </div>

                <div>
                  <label style={{ display: 'block', fontSize: '0.8rem', color: '#94a3b8', marginBottom: '4px' }}>
                    {isRtl ? 'نام کانال مبدأ:' : 'Source Name:'}
                  </label>
                  <input
                    type="text"
                    className="input-field"
                    value={editingRule.source_chat_name || ''}
                    onChange={(e) => setEditingRule({ ...editingRule, source_chat_name: e.target.value })}
                  />
                </div>
              </div>

              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px' }}>
                <div>
                  <label style={{ display: 'block', fontSize: '0.8rem', color: '#94a3b8', marginBottom: '4px' }}>
                    {isRtl ? 'شناسه کانال مقصد B:' : 'Target Chat ID B:'}
                  </label>
                  <input
                    type="text"
                    required
                    className="input-field"
                    value={editingRule.target_chat_id || ''}
                    onChange={(e) => setEditingRule({ ...editingRule, target_chat_id: e.target.value })}
                    placeholder="-100..."
                  />
                </div>

                <div>
                  <label style={{ display: 'block', fontSize: '0.8rem', color: '#94a3b8', marginBottom: '4px' }}>
                    {isRtl ? 'نام کانال مقصد:' : 'Target Name:'}
                  </label>
                  <input
                    type="text"
                    className="input-field"
                    value={editingRule.target_chat_name || ''}
                    onChange={(e) => setEditingRule({ ...editingRule, target_chat_name: e.target.value })}
                  />
                </div>
              </div>

              {/* Route Mode Choice */}
              <div>
                <label style={{ display: 'block', fontSize: '0.85rem', fontWeight: 700, color: '#f8fafc', marginBottom: '8px' }}>
                  {isRtl ? 'انتخاب مسیر اصلی پیام:' : 'Select Routing Path:'}
                </label>
                <div style={{ display: 'flex', gap: '10px', flexWrap: 'wrap' }}>
                  <button
                    type="button"
                    className={`btn ${!editingRule.use_intermediate && editingRule.forward_mode !== 'DIRECT_FORWARD' ? 'btn-primary' : 'btn-secondary'}`}
                    onClick={() => setEditingRule({ ...editingRule, use_intermediate: false, forward_mode: 'COPY_MESSAGE', message_category: 'ALL' })}
                  >
                    {isRtl ? '📋 مسیر ۱: مستقیم A ➔ B' : 'Route 1: Direct Clean'}
                  </button>

                  <button
                    type="button"
                    className={`btn ${editingRule.use_intermediate ? 'btn-vip' : 'btn-secondary'}`}
                    onClick={() => setEditingRule({ ...editingRule, use_intermediate: true, forward_mode: 'CUSTOM_HEADER_COPY', message_category: 'VIP_ONLY' })}
                  >
                    {isRtl ? '💎 مسیر ۲: واسط VIP (A ➔ C ➔ B)' : 'Route 2: VIP Hop (A->C->B)'}
                  </button>

                  <button
                    type="button"
                    className={`btn ${editingRule.forward_mode === 'DIRECT_FORWARD' && !editingRule.use_intermediate ? 'btn-primary' : 'btn-secondary'}`}
                    onClick={() => setEditingRule({ ...editingRule, use_intermediate: false, forward_mode: 'DIRECT_FORWARD', message_category: 'ALL' })}
                  >
                    {isRtl ? '↗️ مسیر ۳: نیتیو A ➔ B' : 'Route 3: Native Forward'}
                  </button>
                </div>
              </div>

              {/* Intermediate Channel C settings */}
              {editingRule.use_intermediate && (
                <div style={{ background: 'rgba(245, 158, 11, 0.1)', border: '1px solid rgba(245, 158, 11, 0.3)', padding: '14px', borderRadius: '10px', display: 'flex', flexDirection: 'column', gap: '10px' }}>
                  <div style={{ fontWeight: 700, color: '#fbbf24', fontSize: '0.9rem' }}>
                    {isRtl ? 'تنظیمات کانال واسط C (هوپ برندینگ):' : 'Intermediate Channel C Configuration:'}
                  </div>
                  <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '10px' }}>
                    <div>
                      <label style={{ fontSize: '0.75rem', color: '#94a3b8' }}>{isRtl ? 'شناسه کانال واسط C:' : 'Intermediate Chat ID:'}</label>
                      <input
                        type="text"
                        className="input-field"
                        value={editingRule.intermediate_channel_id || ''}
                        onChange={(e) => setEditingRule({ ...editingRule, intermediate_channel_id: e.target.value })}
                        placeholder="-100..."
                      />
                    </div>
                    <div>
                      <label style={{ fontSize: '0.75rem', color: '#94a3b8' }}>{isRtl ? 'نام کانال واسط:' : 'Intermediate Title:'}</label>
                      <input
                        type="text"
                        className="input-field"
                        value={editingRule.intermediate_channel_name || ''}
                        onChange={(e) => setEditingRule({ ...editingRule, intermediate_channel_name: e.target.value })}
                      />
                    </div>
                  </div>
                </div>
              )}

              {/* VIP Criteria */}
              <div style={{ background: 'rgba(0,0,0,0.3)', padding: '14px', borderRadius: '10px', display: 'flex', flexDirection: 'column', gap: '10px' }}>
                <div style={{ fontWeight: 700, fontSize: '0.9rem' }}>
                  {isRtl ? 'شروط احراز VIP (Detection Criteria):' : 'VIP Detection Criteria:'}
                </div>

                <div style={{ display: 'flex', gap: '12px', alignItems: 'center' }}>
                  <label style={{ fontSize: '0.8rem', color: '#94a3b8' }}>{isRtl ? 'منطق ترکیب شروط:' : 'Match Mode:'}</label>
                  <select
                    className="input-field"
                    style={{ width: 'auto' }}
                    value={editingRule.detection_criteria?.match_mode || 'ANY'}
                    onChange={(e) => {
                      const cur = editingRule.detection_criteria || {}
                      setEditingRule({ ...editingRule, detection_criteria: { ...cur, match_mode: e.target.value as any } })
                    }}
                  >
                    <option value="ANY">{isRtl ? 'ANY (حداقل یک شرط)' : 'ANY (Match any condition)'}</option>
                    <option value="ALL">{isRtl ? 'ALL (الزام تحقق تمام شروط)' : 'ALL (Match all conditions)'}</option>
                  </select>
                </div>

                <div>
                  <label style={{ fontSize: '0.75rem', color: '#94a3b8' }}>
                    {isRtl ? 'کلیدواژه‌های متنی (جدا با کاما):' : 'Text Keywords (comma-separated):'}
                  </label>
                  <input
                    type="text"
                    className="input-field"
                    value={(editingRule.detection_criteria?.text_contains || []).join(', ')}
                    onChange={(e) => {
                      const kws = e.target.value.split(',').map((x) => x.trim()).filter(Boolean)
                      const cur = editingRule.detection_criteria || {}
                      setEditingRule({ ...editingRule, detection_criteria: { ...cur, text_contains: kws, keywords: kws } })
                    }}
                    placeholder="VIP, SIGNAL, GOLD, تحلیل"
                  />
                </div>

                <div>
                  <label style={{ fontSize: '0.75rem', color: '#94a3b8' }}>
                    {isRtl ? 'الگوی Regex متن:' : 'Text Regex Pattern:'}
                  </label>
                  <input
                    type="text"
                    className="input-field"
                    value={editingRule.detection_criteria?.regex_pattern || ''}
                    onChange={(e) => {
                      const cur = editingRule.detection_criteria || {}
                      setEditingRule({ ...editingRule, detection_criteria: { ...cur, regex_pattern: e.target.value, text_regex: e.target.value } })
                    }}
                    placeholder="(?i)TP\\d+\\s+HIT"
                  />
                </div>
              </div>

              {/* Custom Header */}
              <div>
                <label style={{ fontSize: '0.8rem', color: '#94a3b8' }}>
                  {isRtl ? 'هدر اختصاصی کپی (Custom Header):' : 'Custom Header:'}
                </label>
                <input
                  type="text"
                  className="input-field"
                  value={editingRule.custom_header || ''}
                  onChange={(e) => setEditingRule({ ...editingRule, custom_header: e.target.value })}
                  placeholder="💎 برند اختصاصی ما"
                />
              </div>

              {/* Priority & Split Caption */}
              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '12px', alignItems: 'center' }}>
                <div>
                  <label style={{ fontSize: '0.8rem', color: '#94a3b8' }}>{isRtl ? 'اولویت بررسی (Priority):' : 'Priority:'}</label>
                  <input
                    type="number"
                    className="input-field"
                    value={editingRule.priority ?? 10}
                    onChange={(e) => setEditingRule({ ...editingRule, priority: parseInt(e.target.value) || 10 })}
                  />
                </div>

                <div style={{ display: 'flex', alignItems: 'center', gap: '8px', paddingTop: '18px' }}>
                  <input
                    type="checkbox"
                    id="split_cap"
                    checked={editingRule.split_long_caption ?? true}
                    onChange={(e) => setEditingRule({ ...editingRule, split_long_caption: e.target.checked })}
                  />
                  <label htmlFor="split_cap" style={{ fontSize: '0.8rem', cursor: 'pointer' }}>
                    {isRtl ? 'تفکیک کپشن طولانی (>1024)' : 'Split long captions (>1024)'}
                  </label>
                </div>
              </div>

              {/* Modal Actions */}
              <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '12px', marginTop: '14px' }}>
                <button
                  type="button"
                  className="btn btn-secondary"
                  onClick={() => {
                    setIsModalOpen(false)
                    setEditingRule(null)
                  }}
                >
                  {isRtl ? 'انصراف' : 'Cancel'}
                </button>
                <button type="submit" className="btn btn-primary">
                  {isRtl ? 'ذخیره قانون' : 'Save Rule'}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}
    </div>
  )
}
export default App
