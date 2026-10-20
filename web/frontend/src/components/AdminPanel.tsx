import { useEffect, useState } from 'react'
import {
  ShieldAlert,
  Users,
  Send,
  Lock,
  Server,
  Activity,
  CheckCircle2,
  RefreshCw,
  Plus,
  Trash2,
  Radio,
  Sliders,
} from 'lucide-react'
import { api } from '../api'
import { makeT, type UiLang } from '../locales/engine'

interface Props {
  lang: UiLang
}

export function AdminPanel({ lang }: Props) {
  const isRtl = lang === 'fa'
  const t = makeT(lang)
  const [activeSection, setActiveSection] = useState<'overview' | 'users' | 'broadcast' | 'forcesub' | 'admins' | 'settings'>('overview')
  const [overview, setOverview] = useState<any>(null)
  const [users, setUsers] = useState<any[]>([])
  const [totalUsers, setTotalUsers] = useState(0)
  const [admins, setAdmins] = useState<any[]>([])
  const [newAdminId, setNewAdminId] = useState('')
  const [bcastText, setBcastText] = useState('')
  const [bcastTarget] = useState('all')
  const [bcastSending, setBcastSending] = useState(false)
  const [bcastResult, setBcastResult] = useState<any>(null)
  const [fsEnabled, setFsEnabled] = useState(false)
  const [fsChannel, setFsChannel] = useState('')
  const [webUrl, setWebUrl] = useState('')
  const [regOpen, setRegOpen] = useState(true)
  const [saveStatus, setSaveStatus] = useState<string | null>(null)
  const [extensions, setExtensions] = useState<any[]>([])

  const loadOverview = async () => {
    try {
      const data = await api.adminGetOverview()
      setOverview(data)
    } catch (e: any) {
      console.error(e)
    }
  }

  const loadUsers = async () => {
    try {
      const data = await api.adminListUsers(50, 0)
      setUsers(data?.users || [])
      setTotalUsers(data?.total || 0)
    } catch (e: any) {
      console.error(e)
    }
  }

  const loadSettings = async () => {
    try {
      const data = await api.adminGetSettings()
      setFsEnabled(!!data?.force_sub_enabled)
      setFsChannel(data?.force_sub_channel || '')
      setWebUrl(data?.web_url || '')
      setRegOpen(data?.registration_open ?? true)
    } catch (e: any) {
      console.error(e)
    }
  }

  const loadAdmins = async () => {
    try {
      const data = await api.adminListAdmins()
      setAdmins(data?.admins || [])
    } catch (e: any) {
      console.error(e)
    }
  }

  const loadExtensions = async () => {
    try {
      const data = await api.adminListExtensions()
      setExtensions(data?.extensions || [])
    } catch (e: any) {
      console.error(e)
    }
  }

  useEffect(() => {
    loadOverview()
    loadUsers()
    loadSettings()
    loadAdmins()
    loadExtensions()
  }, [])

  const handleToggleUserStatus = async (uid: number, currentStatus: string) => {
    const next = currentStatus === 'active' ? 'suspended' : 'active'
    try {
      await api.adminSetUserStatus(uid, next)
      loadUsers()
      loadOverview()
    } catch (e: any) {
      alert(e?.message || t('ui_error_updating_user_status'))
    }
  }

  const handleSendBroadcast = async () => {
    if (!bcastText.trim()) return
    setBcastSending(true)
    setBcastResult(null)
    try {
      const res = await api.adminBroadcast(bcastText, bcastTarget)
      setBcastResult(res)
      setBcastText('')
    } catch (e: any) {
      alert(e?.message || t('ui_broadcast_error'))
    } finally {
      setBcastSending(false)
    }
  }

  const handleSaveSettings = async () => {
    setSaveStatus('saving')
    try {
      await api.adminSaveSettings({
        force_sub_enabled: fsEnabled,
        force_sub_channel: fsChannel,
        web_url: webUrl,
        registration_open: regOpen,
      })
      setSaveStatus('saved')
      setTimeout(() => setSaveStatus(null), 3000)
    } catch (e: any) {
      alert(e?.message || t('ui_save_error'))
      setSaveStatus(null)
    }
  }

  const handleAddAdmin = async () => {
    const num = parseInt(newAdminId, 10)
    if (!num || num <= 0) return alert(t('ui_invalid_user_id'))
    try {
      await api.adminAddAdmin(num)
      setNewAdminId('')
      loadAdmins()
    } catch (e: any) {
      alert(e?.message || t('ui_add_admin_error'))
    }
  }

  const handleRemoveAdmin = async (uid: number) => {
    if (!confirm(t('ui_remove_admin_access_for_uid_2', { uid_2: uid }))) return
    try {
      await api.adminRemoveAdmin(uid)
      loadAdmins()
    } catch (e: any) {
      alert(e?.message || t('ui_remove_admin_error'))
    }
  }

  return (
    <div style={{ padding: '8px 0', direction: isRtl ? 'rtl' : 'ltr' }}>
      {/* Header bar */}
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          marginBottom: 20,
          background: 'linear-gradient(135deg, rgba(239, 68, 68, 0.15), rgba(15, 23, 42, 0.8))',
          padding: '16px 20px',
          borderRadius: 16,
          border: '1px solid rgba(239, 68, 68, 0.3)',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <div
            style={{
              padding: 10,
              borderRadius: 12,
              background: 'rgba(239, 68, 68, 0.2)',
              color: '#f87171',
            }}
          >
            <ShieldAlert size={28} />
          </div>
          <div>
            <h2 style={{ margin: 0, fontSize: '1.25rem', fontWeight: 700, color: '#f87171' }}>
              {t('ui_master_administrative_control_plane')}
            </h2>
            <p style={{ margin: 0, fontSize: '0.82rem', color: '#94a3b8' }}>
              {t('ui_real_time_multi_tenant_governance_background_broadcasting_force')}
            </p>
          </div>
        </div>

        <button
          onClick={() => {
            loadOverview()
            loadUsers()
            loadSettings()
            loadAdmins()
          }}
          className="btn"
          style={{ background: 'rgba(255,255,255,0.06)', color: '#cbd5e1' }}
        >
          <RefreshCw size={15} />
          <span>{t('ui_refresh')}</span>
        </button>
      </div>

      {/* Sub navigation */}
      <div
        style={{
          display: 'flex',
          gap: 8,
          marginBottom: 20,
          borderBottom: '1px solid rgba(255, 255, 255, 0.08)',
          paddingBottom: 10,
          overflowX: 'auto',
        }}
      >
        <button
          onClick={() => setActiveSection('overview')}
          className="btn"
          style={{
            background: activeSection === 'overview' ? 'rgba(239, 68, 68, 0.2)' : 'transparent',
            color: activeSection === 'overview' ? '#f87171' : '#94a3b8',
            border: activeSection === 'overview' ? '1px solid rgba(239, 68, 68, 0.4)' : '1px solid transparent',
          }}
        >
          <Activity size={16} />
          <span>{t('ui_system_overview')}</span>
        </button>

        <button
          onClick={() => setActiveSection('users')}
          className="btn"
          style={{
            background: activeSection === 'users' ? 'rgba(239, 68, 68, 0.2)' : 'transparent',
            color: activeSection === 'users' ? '#f87171' : '#94a3b8',
            border: activeSection === 'users' ? '1px solid rgba(239, 68, 68, 0.4)' : '1px solid transparent',
          }}
        >
          <Users size={16} />
          <span>{t('ui_user_governance')}</span>
          <span style={{ fontSize: '0.75rem', opacity: 0.7 }}>({totalUsers})</span>
        </button>

        <button
          onClick={() => setActiveSection('broadcast')}
          className="btn"
          style={{
            background: activeSection === 'broadcast' ? 'rgba(239, 68, 68, 0.2)' : 'transparent',
            color: activeSection === 'broadcast' ? '#f87171' : '#94a3b8',
            border: activeSection === 'broadcast' ? '1px solid rgba(239, 68, 68, 0.4)' : '1px solid transparent',
          }}
        >
          <Send size={16} />
          <span>{t('ui_broadcast_queue')}</span>
        </button>

        <button
          onClick={() => setActiveSection('forcesub')}
          className="btn"
          style={{
            background: activeSection === 'forcesub' ? 'rgba(239, 68, 68, 0.2)' : 'transparent',
            color: activeSection === 'forcesub' ? '#f87171' : '#94a3b8',
            border: activeSection === 'forcesub' ? '1px solid rgba(239, 68, 68, 0.4)' : '1px solid transparent',
          }}
        >
          <Lock size={16} />
          <span>{t('ui_force_subscription')}</span>
        </button>

        <button
          onClick={() => setActiveSection('admins')}
          className="btn"
          style={{
            background: activeSection === 'admins' ? 'rgba(239, 68, 68, 0.2)' : 'transparent',
            color: activeSection === 'admins' ? '#f87171' : '#94a3b8',
            border: activeSection === 'admins' ? '1px solid rgba(239, 68, 68, 0.4)' : '1px solid transparent',
          }}
        >
          <ShieldAlert size={16} />
          <span>{t('ui_administrators')}</span>
        </button>

        <button
          onClick={() => setActiveSection('settings')}
          className="btn"
          style={{
            background: activeSection === 'settings' ? 'rgba(239, 68, 68, 0.2)' : 'transparent',
            color: activeSection === 'settings' ? '#f87171' : '#94a3b8',
            border: activeSection === 'settings' ? '1px solid rgba(239, 68, 68, 0.4)' : '1px solid transparent',
          }}
        >
          <Sliders size={16} />
          <span>{t('ui_settings_extensions')}</span>
        </button>
      </div>

      {/* SECTION: Overview */}
      {activeSection === 'overview' && (
        <div>
          <div
            style={{
              display: 'grid',
              gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))',
              gap: 16,
              marginBottom: 24,
            }}
          >
            <div style={{ background: 'rgba(15, 23, 42, 0.7)', padding: 18, borderRadius: 14, border: '1px solid rgba(255,255,255,0.08)' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', color: '#94a3b8', fontSize: '0.85rem' }}>
                <span>{t('ui_total_registered_users')}</span>
                <Users size={18} color="#60a5fa" />
              </div>
              <div style={{ fontSize: '1.8rem', fontWeight: 800, marginTop: 8, color: '#f8fafc' }}>
                {overview?.total_users ?? totalUsers}
              </div>
            </div>

            <div style={{ background: 'rgba(15, 23, 42, 0.7)', padding: 18, borderRadius: 14, border: '1px solid rgba(255,255,255,0.08)' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', color: '#94a3b8', fontSize: '0.85rem' }}>
                <span>{t('ui_active_rules')}</span>
                <Radio size={18} color="#34d399" />
              </div>
              <div style={{ fontSize: '1.8rem', fontWeight: 800, marginTop: 8, color: '#34d399' }}>
                {overview?.active_rules ?? 0}
              </div>
            </div>

            <div style={{ background: 'rgba(15, 23, 42, 0.7)', padding: 18, borderRadius: 14, border: '1px solid rgba(255,255,255,0.08)' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', color: '#94a3b8', fontSize: '0.85rem' }}>
                <span>{t('ui_pending_in_queue')}</span>
                <Activity size={18} color="#fbbf24" />
              </div>
              <div style={{ fontSize: '1.8rem', fontWeight: 800, marginTop: 8, color: '#fbbf24' }}>
                {overview?.queue_pending ?? 0}
              </div>
            </div>

            <div style={{ background: 'rgba(15, 23, 42, 0.7)', padding: 18, borderRadius: 14, border: '1px solid rgba(255,255,255,0.08)' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', color: '#94a3b8', fontSize: '0.85rem' }}>
                <span>{t('ui_core_daemon_status')}</span>
                <Server size={18} color="#4ade80" />
              </div>
              <div style={{ fontSize: '1.2rem', fontWeight: 700, marginTop: 12, color: '#4ade80', display: 'flex', alignItems: 'center', gap: 6 }}>
                <CheckCircle2 size={18} />
                <span>{overview?.core_online ? t('ui_online_grpc_50051') : t('ui_standby')}</span>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* SECTION: Users */}
      {activeSection === 'users' && (
        <div style={{ background: 'rgba(15, 23, 42, 0.7)', padding: 20, borderRadius: 14, border: '1px solid rgba(255,255,255,0.08)' }}>
          <h3 style={{ margin: '0 0 16px', fontSize: '1.05rem', color: '#f8fafc' }}>
            {t('ui_registered_tenants_lifecycle_control')}
          </h3>

          <div style={{ overflowX: 'auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', textAlign: isRtl ? 'right' : 'left', fontSize: '0.88rem' }}>
              <thead>
                <tr style={{ borderBottom: '1px solid rgba(255,255,255,0.1)', color: '#94a3b8' }}>
                  <th style={{ padding: '10px 12px' }}>{t('ui_admin_user_id')}</th>
                  <th style={{ padding: '10px 12px' }}>{t('ui_username')}</th>
                  <th style={{ padding: '10px 12px' }}>{t('ui_plan')}</th>
                  <th style={{ padding: '10px 12px' }}>{t('ui_status')}</th>
                  <th style={{ padding: '10px 12px' }}>{t('ui_actions')}</th>
                </tr>
              </thead>
              <tbody>
                {users.map((u) => {
                  const isSuspended = u.status === 'suspended'
                  return (
                    <tr key={u.user_id} style={{ borderBottom: '1px solid rgba(255,255,255,0.05)' }}>
                      <td style={{ padding: '12px', fontFamily: 'monospace', color: '#e2e8f0' }}>
                        {u.user_id} {u.is_admin ? <span className="badge badge-vip">{t('ui_role_admin')}</span> : null}
                      </td>
                      <td style={{ padding: '12px', color: '#cbd5e1' }}>{u.username ? `@${u.username}` : '—'}</td>
                      <td style={{ padding: '12px', color: '#94a3b8', textTransform: 'uppercase' }}>{u.plan}</td>
                      <td style={{ padding: '12px' }}>
                        <span
                          style={{
                            display: 'inline-block',
                            padding: '2px 8px',
                            borderRadius: 6,
                            fontSize: '0.78rem',
                            fontWeight: 600,
                            background: isSuspended ? 'rgba(239,68,68,0.2)' : 'rgba(34,197,94,0.2)',
                            color: isSuspended ? '#f87171' : '#4ade80',
                          }}
                        >
                          {isSuspended ? (t('ui_suspended')) : (t('ui_active'))}
                        </span>
                      </td>
                      <td style={{ padding: '12px' }}>
                        {!u.is_admin && (
                          <button
                            onClick={() => handleToggleUserStatus(u.user_id, u.status)}
                            className="btn"
                            style={{
                              padding: '4px 10px',
                              fontSize: '0.78rem',
                              background: isSuspended ? 'rgba(34,197,94,0.2)' : 'rgba(239,68,68,0.2)',
                              color: isSuspended ? '#4ade80' : '#f87171',
                              border: isSuspended ? '1px solid rgba(34,197,94,0.4)' : '1px solid rgba(239,68,68,0.4)',
                            }}
                          >
                            {isSuspended ? (t('ui_activate')) : (t('ui_suspend'))}
                          </button>
                        )}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* SECTION: Broadcast */}
      {activeSection === 'broadcast' && (
        <div style={{ background: 'rgba(15, 23, 42, 0.7)', padding: 20, borderRadius: 14, border: '1px solid rgba(255,255,255,0.08)' }}>
          <h3 style={{ margin: '0 0 12px', fontSize: '1.05rem', color: '#f8fafc' }}>
            {t('ui_asynchronous_broadcast_dispatcher')}
          </h3>
          <p style={{ margin: '0 0 16px', fontSize: '0.84rem', color: '#94a3b8' }}>
            {t('ui_messages_are_asynchronously_queued_and_dispatched_at_25_msgs_sec')}
          </p>

          <div style={{ marginBottom: 16 }}>
            <label style={{ display: 'block', fontSize: '0.85rem', color: '#cbd5e1', marginBottom: 6 }}>
              {t('ui_broadcast_message_body_markdown_supported')}
            </label>
            <textarea
              rows={5}
              value={bcastText}
              onChange={(e) => setBcastText(e.target.value)}
              placeholder={t('ui_type_broadcast_announcement_here')}
              style={{
                width: '100%',
                padding: 12,
                borderRadius: 8,
                background: 'rgba(30, 41, 59, 0.8)',
                border: '1px solid rgba(255,255,255,0.1)',
                color: '#fff',
                fontSize: '0.9rem',
                fontFamily: 'inherit',
              }}
            />
          </div>

          <div style={{ display: 'flex', gap: 12, alignItems: 'center', marginBottom: 20 }}>
            <button
              onClick={handleSendBroadcast}
              disabled={bcastSending || !bcastText.trim()}
              className="btn btn-primary"
              style={{
                padding: '10px 24px',
                background: '#ef4444',
                color: '#fff',
                border: 'none',
                fontWeight: 700,
              }}
            >
              <Send size={16} />
              <span>{bcastSending ? (t('ui_dispatching')) : (t('ui_dispatch_broadcast'))}</span>
            </button>
          </div>

          {bcastResult && (
            <div
              style={{
                padding: 16,
                borderRadius: 10,
                background: 'rgba(34, 197, 94, 0.15)',
                border: '1px solid rgba(34, 197, 94, 0.3)',
                color: '#4ade80',
                fontSize: '0.88rem',
              }}
            >
              ✅ {bcastResult.message || t('ui_broadcast_queued_successfully')}
              <div style={{ marginTop: 6, fontSize: '0.8rem', color: '#86efac' }}>
                {t('ui_broadcast_id')} <code>{bcastResult.broadcast_id}</code> | {t('ui_recipients')} <b>{bcastResult.total_recipients}</b>
              </div>
            </div>
          )}
        </div>
      )}

      {/* SECTION: Force-Subscription */}
      {activeSection === 'forcesub' && (
        <div style={{ background: 'rgba(15, 23, 42, 0.7)', padding: 20, borderRadius: 14, border: '1px solid rgba(255,255,255,0.08)' }}>
          <h3 style={{ margin: '0 0 12px', fontSize: '1.05rem', color: '#f8fafc' }}>
            {t('ui_channel_force_subscription_gate')}
          </h3>
          <p style={{ margin: '0 0 16px', fontSize: '0.84rem', color: '#94a3b8' }}>
            {t('ui_enforce_mandatory_channel_membership_prior_to_granting_access_to')}
          </p>

          <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 20 }}>
            <input
              type="checkbox"
              id="fsToggle"
              checked={fsEnabled}
              onChange={(e) => setFsEnabled(e.target.checked)}
              style={{ width: 18, height: 18, accentColor: '#ef4444' }}
            />
            <label htmlFor="fsToggle" style={{ color: '#f8fafc', fontWeight: 600, fontSize: '0.92rem' }}>
              {t('ui_enable_mandatory_channel_subscription')}
            </label>
          </div>

          <div style={{ marginBottom: 20, maxWidth: 450 }}>
            <label style={{ display: 'block', fontSize: '0.85rem', color: '#cbd5e1', marginBottom: 6 }}>
              {t('ui_required_channel_username_or_numeric_id')}
            </label>
            <input
              type="text"
              value={fsChannel}
              onChange={(e) => setFsChannel(e.target.value)}
              placeholder={t('ui_mychannel_or_100123456789')}
              style={{
                width: '100%',
                padding: 10,
                borderRadius: 8,
                background: 'rgba(30, 41, 59, 0.8)',
                border: '1px solid rgba(255,255,255,0.1)',
                color: '#fff',
                fontSize: '0.9rem',
              }}
            />
          </div>

          <button onClick={handleSaveSettings} className="btn btn-primary" style={{ padding: '8px 20px' }}>
            <span>{saveStatus === 'saving' ? (t('ui_saving')) : (t('ui_save_gate_settings'))}</span>
          </button>
          {saveStatus === 'saved' && <span style={{ marginLeft: 12, color: '#4ade80', fontSize: '0.85rem' }}>✅ {t('ui_saved')}</span>}
        </div>
      )}

      {/* SECTION: Administrators */}
      {activeSection === 'admins' && (
        <div style={{ background: 'rgba(15, 23, 42, 0.7)', padding: 20, borderRadius: 14, border: '1px solid rgba(255,255,255,0.08)' }}>
          <h3 style={{ margin: '0 0 12px', fontSize: '1.05rem', color: '#f8fafc' }}>
            {t('ui_authorized_administrators_rbac')}
          </h3>

          <div style={{ display: 'flex', gap: 10, marginBottom: 20, maxWidth: 400 }}>
            <input
              type="number"
              placeholder={t('ui_numeric_telegram_user_id')}
              value={newAdminId}
              onChange={(e) => setNewAdminId(e.target.value)}
              style={{
                flex: 1,
                padding: 10,
                borderRadius: 8,
                background: 'rgba(30, 41, 59, 0.8)',
                border: '1px solid rgba(255,255,255,0.1)',
                color: '#fff',
                fontSize: '0.9rem',
              }}
            />
            <button onClick={handleAddAdmin} className="btn btn-primary" style={{ padding: '0 16px' }}>
              <Plus size={16} />
              <span>{t('ui_add_admin')}</span>
            </button>
          </div>

          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            {admins.map((a) => (
              <div
                key={a.user_id}
                style={{
                  display: 'flex',
                  justifyContent: 'space-between',
                  alignItems: 'center',
                  padding: '12px 16px',
                  borderRadius: 10,
                  background: 'rgba(30, 41, 59, 0.5)',
                  border: '1px solid rgba(255,255,255,0.06)',
                }}
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                  <ShieldAlert size={18} color={a.role === 'owner' ? '#fbbf24' : '#60a5fa'} />
                  <span style={{ fontFamily: 'monospace', fontWeight: 700, color: '#f8fafc' }}>{a.user_id}</span>
                  <span className="badge" style={{ background: a.role === 'owner' ? '#f59e0b' : '#3b82f6', color: '#fff' }}>
                    {a.role.toUpperCase()}
                  </span>
                </div>

                {a.role !== 'owner' && (
                  <button
                    onClick={() => handleRemoveAdmin(a.user_id)}
                    className="btn"
                    style={{ padding: '4px 8px', color: '#f87171', background: 'rgba(239,68,68,0.1)' }}
                  >
                    <Trash2 size={14} />
                    <span>{t('ui_remove')}</span>
                  </button>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* SECTION: Settings & Extensions */}
      {activeSection === 'settings' && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
          <div style={{ background: 'rgba(15, 23, 42, 0.7)', padding: 20, borderRadius: 14, border: '1px solid rgba(255,255,255,0.08)' }}>
            <h3 style={{ margin: '0 0 12px', fontSize: '1.05rem', color: '#f8fafc' }}>
              {t('ui_public_web_domain_configuration')}
            </h3>
            <div style={{ marginBottom: 16, maxWidth: 500 }}>
              <label style={{ display: 'block', fontSize: '0.85rem', color: '#cbd5e1', marginBottom: 6 }}>
                {t('ui_dashboard_public_url')}
              </label>
              <input
                type="text"
                value={webUrl}
                onChange={(e) => setWebUrl(e.target.value)}
                placeholder="https://api.legoten.com"
                style={{
                  width: '100%',
                  padding: 10,
                  borderRadius: 8,
                  background: 'rgba(30, 41, 59, 0.8)',
                  border: '1px solid rgba(255,255,255,0.1)',
                  color: '#fff',
                  fontSize: '0.9rem',
                }}
              />
            </div>

            <button onClick={handleSaveSettings} className="btn btn-primary" style={{ padding: '8px 20px' }}>
              <span>{t('ui_save_domain')}</span>
            </button>
          </div>

          <div style={{ background: 'rgba(15, 23, 42, 0.7)', padding: 20, borderRadius: 14, border: '1px solid rgba(255,255,255,0.08)' }}>
            <h3 style={{ margin: '0 0 12px', fontSize: '1.05rem', color: '#f8fafc' }}>
              {t('ui_platform_extensions_core_engines')}
            </h3>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(280px, 1fr))', gap: 14 }}>
              {extensions.map((ext) => (
                <div
                  key={ext.id}
                  style={{
                    padding: 16,
                    borderRadius: 12,
                    background: 'rgba(30, 41, 59, 0.6)',
                    border: '1px solid rgba(255,255,255,0.06)',
                  }}
                >
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 8 }}>
                    <span style={{ fontWeight: 700, color: '#f8fafc', fontSize: '0.92rem' }}>{ext.name}</span>
                    <span className="badge badge-success" style={{ fontSize: 'var(--type-xs)' }}>v{ext.version}</span>
                  </div>
                  <p style={{ margin: '0 0 12px', fontSize: '0.8rem', color: '#94a3b8' }}>{ext.description}</p>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                    <span style={{ fontSize: '0.75rem', color: '#4ade80' }}>● {ext.status.toUpperCase()}</span>
                    <span style={{ fontSize: '0.75rem', color: 'var(--color-text-muted)' }}>{ext.author}</span>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
