import { useEffect, useState } from 'react'
import { KeyRound, Shield, UserCheck, CheckCircle2, AlertCircle, Save } from 'lucide-react'
import { api } from '../api'

interface Props {
  lang: 'fa' | 'en'
}

export function ClientSettings({ lang }: Props) {
  const isRtl = lang === 'fa'
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
        text: isRtl ? 'لطفاً یک API ID معتبر وارد نمایید.' : 'Please provide a valid numeric API ID.',
        ok: false,
      })
      return
    }
    if (cleanHash.length !== 32) {
      setStatusMsg({
        text: isRtl ? 'API Hash باید دقیقاً ۳۲ کاراکتر هگزادسیمال باشد.' : 'API Hash must be 32 hexadecimal characters.',
        ok: false,
      })
      return
    }

    setSaving(true)
    setStatusMsg(null)
    try {
      const res = await api.clientSaveCredentials(numId, cleanHash)
      setStatusMsg({ text: res.message || (isRtl ? 'کلیدهای اختصاصی ذخیره شد.' : 'Credentials saved.'), ok: true })
      setApiHash('')
      loadSettings()
    } catch (e: any) {
      setStatusMsg({ text: e?.message || (isRtl ? 'خطا در ذخیره کلیدها' : 'Error saving credentials'), ok: false })
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
          background: 'linear-gradient(135deg, rgba(99, 102, 241, 0.15), rgba(15, 23, 42, 0.8))',
          padding: '16px 20px',
          borderRadius: 16,
          border: '1px solid rgba(99, 102, 241, 0.3)',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <div
            style={{
              padding: 10,
              borderRadius: 12,
              background: 'rgba(99, 102, 241, 0.2)',
              color: '#818cf8',
            }}
          >
            <KeyRound size={28} />
          </div>
          <div>
            <h2 style={{ margin: 0, fontSize: '1.25rem', fontWeight: 700, color: '#818cf8' }}>
              {isRtl ? 'تنظیمات و کلیدهای شخصی تلگرام' : 'Client Settings & Credentials'}
            </h2>
            <p style={{ margin: 0, fontSize: '0.82rem', color: '#94a3b8' }}>
              {isRtl
                ? 'پیکربندی اختصاصی کلیدهای API ID و API Hash مستقل برای هر کاربر و مدیریت سهمیه'
                : 'Isolated workspace credentials, personal Telegram API keys, and account preferences'}
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
            background: 'rgba(15, 23, 42, 0.7)',
            padding: 20,
            borderRadius: 14,
            border: '1px solid rgba(255,255,255,0.08)',
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 14 }}>
            <UserCheck size={20} color="#38bdf8" />
            <h3 style={{ margin: 0, fontSize: '1rem', color: '#f8fafc' }}>
              {isRtl ? 'اطلاعات حساب و دسترسی' : 'Account & Quota Overview'}
            </h3>
          </div>

          <div style={{ display: 'flex', flexDirection: 'column', gap: 10, fontSize: '0.88rem' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', padding: '8px 0', borderBottom: '1px solid rgba(255,255,255,0.05)' }}>
              <span style={{ color: '#94a3b8' }}>{isRtl ? 'شناسه کاربری عددی:' : 'Numeric User ID:'}</span>
              <span style={{ fontFamily: 'monospace', fontWeight: 700, color: '#f8fafc' }}>{settings?.user_id || '—'}</span>
            </div>

            <div style={{ display: 'flex', justifyContent: 'space-between', padding: '8px 0', borderBottom: '1px solid rgba(255,255,255,0.05)' }}>
              <span style={{ color: '#94a3b8' }}>{isRtl ? 'سطح اشتراک (Plan):' : 'Subscription Plan:'}</span>
              <span className="badge badge-vip" style={{ textTransform: 'uppercase' }}>{settings?.plan || 'FREE'}</span>
            </div>

            <div style={{ display: 'flex', justifyContent: 'space-between', padding: '8px 0', borderBottom: '1px solid rgba(255,255,255,0.05)' }}>
              <span style={{ color: '#94a3b8' }}>{isRtl ? 'پیام‌های فورواردشده:' : 'Forwarded Messages:'}</span>
              <span style={{ fontWeight: 700, color: '#34d399' }}>{settings?.quota_forwarded ?? 0}</span>
            </div>

            <div style={{ display: 'flex', justifyContent: 'space-between', padding: '8px 0' }}>
              <span style={{ color: '#94a3b8' }}>{isRtl ? 'وضعیت کلید اختصاصی:' : 'Custom Credentials:'}</span>
              <span style={{ color: settings?.has_custom_credentials ? '#4ade80' : '#fbbf24', fontWeight: 600 }}>
                {settings?.has_custom_credentials
                  ? (isRtl ? '✅ فعال و رمزنگاری‌شده' : 'Active & Encrypted')
                  : (isRtl ? 'استفاده از پیش‌فرض سامانه' : 'Using System Default')}
              </span>
            </div>
          </div>
        </div>

        {/* Telegram API Credentials Form */}
        <div
          style={{
            background: 'rgba(15, 23, 42, 0.7)',
            padding: 20,
            borderRadius: 14,
            border: '1px solid rgba(255,255,255,0.08)',
          }}
        >
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 14 }}>
            <Shield size={20} color="#818cf8" />
            <h3 style={{ margin: 0, fontSize: '1rem', color: '#f8fafc' }}>
              {isRtl ? 'کلیدهای اختصاصی API تلگرام' : 'Custom Telegram API Credentials'}
            </h3>
          </div>

          <p style={{ margin: '0 0 16px', fontSize: '0.82rem', color: '#94a3b8' }}>
            {isRtl
              ? 'در صورتی که می‌خواهید فورواردها با شناسه برنامه اختصاصی شما در my.telegram.org انجام شوند، اطلاعات زیر را وارد نمایید.'
              : 'Enter your own API ID and API Hash from my.telegram.org to run your MTProto sessions under your own developer keys.'}
          </p>

          <div style={{ marginBottom: 14 }}>
            <label style={{ display: 'block', fontSize: '0.82rem', color: '#cbd5e1', marginBottom: 6 }}>
              {isRtl ? 'Telegram API ID (عدد):' : 'Telegram API ID (Numeric):'}
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
                background: 'rgba(30, 41, 59, 0.8)',
                border: '1px solid rgba(255,255,255,0.1)',
                color: '#fff',
                fontSize: '0.9rem',
              }}
            />
          </div>

          <div style={{ marginBottom: 16 }}>
            <label style={{ display: 'block', fontSize: '0.82rem', color: '#cbd5e1', marginBottom: 6 }}>
              {isRtl ? 'Telegram API Hash (۳۲ کاراکتر):' : 'Telegram API Hash (32 characters):'}
            </label>
            <input
              type="password"
              value={apiHash}
              onChange={(e) => setApiHash(e.target.value)}
              placeholder={settings?.api_hash_masked ? `Masked: ${settings.api_hash_masked}` : 'e.g. 0123456789abcdef0123456789abcdef'}
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

          <button
            onClick={handleSaveCredentials}
            disabled={saving}
            className="btn btn-primary"
            style={{ width: '100%', padding: '10px 0', fontWeight: 600 }}
          >
            <Save size={16} />
            <span>{saving ? (isRtl ? 'در حال ذخیره‌سازی...' : 'Saving...') : (isRtl ? 'ذخیره کلیدهای اختصاصی' : 'Save Custom Keys')}</span>
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
                background: statusMsg.ok ? 'rgba(34,197,94,0.15)' : 'rgba(239,68,68,0.15)',
                border: statusMsg.ok ? '1px solid rgba(34,197,94,0.3)' : '1px solid rgba(239,68,68,0.3)',
                color: statusMsg.ok ? '#4ade80' : '#f87171',
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
