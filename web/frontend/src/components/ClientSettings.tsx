import { useEffect, useState } from 'react'
import { KeyRound, Shield, UserCheck, CheckCircle2, AlertCircle, Save } from 'lucide-react'
import { api } from '../api'
import { makeT, type UiLang } from '../locales/engine'
import { SkeletonRows } from './ui/Primitives'

interface Props {
  lang: UiLang
}

export function ClientSettings({ lang }: Props) {
  const isRtl = lang === 'fa'
  const t = makeT(lang)
  const [settings, setSettings] = useState<any>(null)
  const [apiId, setApiId] = useState('')
  const [apiHash, setApiHash] = useState('')
  const [saving, setSaving] = useState(false)
  const [statusMsg, setStatusMsg] = useState<{ text: string; ok: boolean } | null>(null)

  const loadSettings = async () => {
    try {
      const data = await api.clientGetSettings()
      setSettings(data)
      if (data?.api_id) setApiId(String(data.api_id))
    } catch (e: any) {
      console.error(e)
    }
  }

  useEffect(() => {
    loadSettings()
  }, [])

  const handleSaveCredentials = async () => {
    const numId = parseInt(apiId, 10)
    const cleanHash = apiHash.trim()
    if (!numId || numId <= 0) {
      setStatusMsg({
        text: t('ui_please_provide_a_valid_numeric_api_id'),
        ok: false,
      })
      return
    }
    if (cleanHash.length !== 32) {
      setStatusMsg({
        text: t('ui_api_hash_must_be_32_hexadecimal_characters'),
        ok: false,
      })
      return
    }

    setSaving(true)
    setStatusMsg(null)
    try {
      const res = await api.clientSaveCredentials(numId, cleanHash)
      setStatusMsg({ text: res.message || (t('ui_credentials_saved')), ok: true })
      setApiHash('')
      loadSettings()
    } catch (e: any) {
      setStatusMsg({ text: e?.message || (t('ui_error_saving_credentials')), ok: false })
    } finally {
      setSaving(false)
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
          background: 'linear-gradient(135deg, var(--color-primary-soft), var(--color-surface-2))',
          padding: '16px 20px',
          borderRadius: 16,
          border: '1px solid color-mix(in oklab, var(--color-primary) 30%, transparent)',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <div
            style={{
              padding: 10,
              borderRadius: 12,
              background: 'var(--color-primary-soft)',
              color: 'var(--color-accent-violet)',
            }}
          >
            <KeyRound size={28} />
          </div>
          <div>
            <h2 style={{ margin: 0, fontSize: 'var(--type-xl)', fontWeight: 700, color: 'var(--color-accent-violet)' }}>
              {t('ui_client_settings_credentials')}
            </h2>
            <p style={{ margin: 0, fontSize: '0.82rem', color: 'var(--color-text-muted)' }}>
              {t('ui_isolated_workspace_credentials_personal_telegram_api_keys_and_ac')}
            </p>
          </div>
        </div>
      </div>

      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fit, minmax(300px, 1fr))',
          gap: 20,
        }}
      >
        {/* Workspace Account Status */}
        <div
          style={{
            background: 'var(--color-surface-2)',
            padding: 20,
            borderRadius: 14,
            border: '1px solid var(--color-border)',
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 14 }}>
            <UserCheck size={20} color="var(--color-info)" />
            <h3 style={{ margin: 0, fontSize: '1rem', color: 'var(--color-text)' }}>
              {t('ui_account_quota_overview')}
            </h3>
          </div>

          {settings ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10, fontSize: '0.88rem' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', padding: '8px 0', borderBottom: '1px solid var(--color-border)' }}>
              <span style={{ color: 'var(--color-text-muted)' }}>{t('ui_numeric_user_id')}</span>
              <span style={{ fontFamily: 'monospace', fontWeight: 700, color: 'var(--color-text)' }}>{settings?.user_id || '—'}</span>
            </div>

            <div style={{ display: 'flex', justifyContent: 'space-between', padding: '8px 0', borderBottom: '1px solid var(--color-border)' }}>
              <span style={{ color: 'var(--color-text-muted)' }}>{t('ui_subscription_plan')}</span>
              <span className="badge badge-vip" style={{ textTransform: 'uppercase' }}>{settings?.plan || 'FREE'}</span>
            </div>

            <div style={{ display: 'flex', justifyContent: 'space-between', padding: '8px 0', borderBottom: '1px solid var(--color-border)' }}>
              <span style={{ color: 'var(--color-text-muted)' }}>{t('ui_forwarded_messages')}</span>
              <span style={{ fontWeight: 700, color: 'var(--color-success)' }}>{settings?.quota_forwarded ?? 0}</span>
            </div>

            <div style={{ display: 'flex', justifyContent: 'space-between', padding: '8px 0' }}>
              <span style={{ color: 'var(--color-text-muted)' }}>{t('ui_custom_credentials')}</span>
              <span style={{ color: settings?.has_custom_credentials ? 'var(--color-success)' : 'var(--color-warning)', fontWeight: 600 }}>
                {settings?.has_custom_credentials
                  ? (t('ui_active_encrypted'))
                  : (t('ui_using_system_default'))}
              </span>
            </div>
          </div>
          ) : (
            <SkeletonRows lang={lang} />
          )}
        </div>

        {/* Telegram API Credentials Form */}
        <div
          style={{
            background: 'var(--color-surface-2)',
            padding: 20,
            borderRadius: 14,
            border: '1px solid var(--color-border)',
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 14 }}>
            <Shield size={20} color="var(--color-accent-violet)" />
            <h3 style={{ margin: 0, fontSize: '1rem', color: 'var(--color-text)' }}>
              {t('ui_custom_telegram_api_credentials')}
            </h3>
          </div>

          <p style={{ margin: '0 0 16px', fontSize: '0.82rem', color: 'var(--color-text-muted)' }}>
            {t('ui_enter_your_own_api_id_and_api_hash_from_my_telegram_org_to_run_y')}
          </p>

          <div style={{ marginBottom: 14 }}>
            <label style={{ display: 'block', fontSize: '0.82rem', color: 'var(--color-text)', marginBottom: 6 }}>
              {t('ui_telegram_api_id_numeric')}
            </label>
            <input
              type="number"
              value={apiId}
              onChange={(e) => setApiId(e.target.value)}
              placeholder="e.g. 12345678"
              style={{
                width: '100%',
                padding: 10,
                borderRadius: 8,
                background: 'var(--color-surface-inset)',
                border: '1px solid var(--color-border)',
                color: 'var(--color-text)',
                fontSize: 'var(--type-md)',
              }}
            />
          </div>

          <div style={{ marginBottom: 16 }}>
            <label style={{ display: 'block', fontSize: '0.82rem', color: 'var(--color-text)', marginBottom: 6 }}>
              {t('ui_telegram_api_hash_32_characters')}
            </label>
            <input
              type="password"
              value={apiHash}
              onChange={(e) => setApiHash(e.target.value)}
              placeholder={settings?.api_hash_masked ? t('ui_masked_value', { value: settings.api_hash_masked }) : 'e.g. 0123456789abcdef0123456789abcdef'}
              style={{
                width: '100%',
                padding: 10,
                borderRadius: 8,
                background: 'var(--color-surface-inset)',
                border: '1px solid var(--color-border)',
                color: 'var(--color-text)',
                fontSize: 'var(--type-md)',
              }}
            />
          </div>

          <button
            onClick={handleSaveCredentials}
            disabled={saving}
            className="btn btn-primary"
            style={{ width: '100%', padding: '10px 0', fontWeight: 600 }}
          >
            <Save size={16} />
            <span>{saving ? (t('ui_saving')) : (t('ui_save_custom_keys'))}</span>
          </button>

          {statusMsg && (
            <div
              style={{
                marginTop: 14,
                padding: 10,
                borderRadius: 8,
                display: 'flex',
                alignItems: 'center',
                gap: 8,
                fontSize: '0.84rem',
                background: statusMsg.ok ? 'var(--color-success-soft)' : 'var(--color-danger-soft)',
                border: statusMsg.ok ? '1px solid color-mix(in oklab, var(--color-success) 30%, transparent)' : '1px solid color-mix(in oklab, var(--color-danger) 30%, transparent)',
                color: statusMsg.ok ? 'var(--color-success)' : 'var(--color-danger)',
              }}
            >
              {statusMsg.ok ? <CheckCircle2 size={16} /> : <AlertCircle size={16} />}
              <span>{statusMsg.text}</span>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
