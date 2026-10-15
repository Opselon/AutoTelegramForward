import { useCallback, useEffect, useState } from 'react'
import { AlertCircle, ChevronDown, ChevronRight, ChevronUp, Copy, Download, Loader2, RefreshCw } from 'lucide-react'
import { api } from '../api'
import type { LogItem, LogPage } from '../types'

interface Props {
  ruleId: string
  lang?: 'fa' | 'en'
}

const LEVEL_COLORS: Record<string, { fg: string; bg: string }> = {
  ERROR: { fg: '#f87171', bg: 'rgba(239, 68, 68, 0.18)' },
  WARN: { fg: '#fbbf24', bg: 'rgba(245, 158, 11, 0.18)' },
  WARNING: { fg: '#fbbf24', bg: 'rgba(245, 158, 11, 0.18)' },
  INFO: { fg: '#34d399', bg: 'rgba(16, 185, 129, 0.18)' },
  DEBUG: { fg: '#38bdf8', bg: 'rgba(56, 189, 248, 0.18)' },
}

function levelStyle(level?: string) {
  return LEVEL_COLORS[(level || 'INFO').toUpperCase()] || LEVEL_COLORS.INFO
}

function fmtTime(ts: number): string {
  if (!ts) return '--:--:--'
  return new Date(ts * 1000).toLocaleString()
}

/**
 * One task's complete log history.
 *
 * The whole point of this panel is reach: it pages through the archive with an
 * explicit offset and shows the real match count, so an operator can walk back
 * to the oldest event a task ever produced instead of being stuck with the
 * newest page.
 */
export function TaskLogsPanel({ ruleId, lang = 'en' }: Props) {
  const isRtl = lang === 'fa'
  const [page, setPage] = useState<LogPage | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [levels, setLevels] = useState<string[]>([])
  const [search, setSearch] = useState('')
  const [appliedSearch, setAppliedSearch] = useState('')
  const [hours, setHours] = useState<number>(0)
  const [order, setOrder] = useState<'desc' | 'asc'>('desc')
  const [pageSize, setPageSize] = useState(25)
  const [offset, setOffset] = useState(0)
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set())
  const [copied, setCopied] = useState<string | null>(null)
  const [exporting, setExporting] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await api.getRuleLogs(ruleId, {
        levels: levels.length ? levels : undefined,
        search: appliedSearch || undefined,
        hours: hours || undefined,
        offset,
        limit: pageSize,
        ascending: order === 'asc',
      })
      setPage(res)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load this task’s logs')
      setPage(null)
    } finally {
      setLoading(false)
    }
  }, [ruleId, levels, appliedSearch, hours, offset, pageSize, order])

  useEffect(() => {
    void load()
  }, [load])

  // A filter change invalidates the current offset: page 3 of the old filter is
  // a different window than page 3 of the new one.
  useEffect(() => {
    setOffset(0)
  }, [levels, appliedSearch, hours, pageSize, order, ruleId])

  const logs = page?.logs ?? []
  const total = page?.total ?? 0
  const first = total === 0 ? 0 : offset + 1
  const last = offset + logs.length

  const toggle = (key: string) => {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  const copyRow = async (item: LogItem, key: string) => {
    const line = `[${fmtTime(item.ts)}] [${(item.level || 'INFO').toUpperCase()}] ` +
      `[${item.service || 'core'}/${item.category || 'sys'}] ${item.message || ''}` +
      (item.correlation_id ? ` corr=${item.correlation_id}` : '')
    try {
      await navigator.clipboard.writeText(item.detail ? `${line}\n${item.detail}` : line)
      setCopied(key)
      setTimeout(() => setCopied(null), 1500)
    } catch {
      /* clipboard blocked — the row is still selectable */
    }
  }

  const handleExport = async (format: 'json' | 'csv') => {
    setExporting(true)
    try {
      await api.exportLogs({
        rule_id: ruleId,
        levels: levels.length ? levels : undefined,
        search: appliedSearch || undefined,
        hours: hours || undefined,
        format,
      })
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Export failed')
    } finally {
      setExporting(false)
    }
  }

  const chip = (value: string) => {
    const on = levels.includes(value)
    const style = levelStyle(value)
    return (
      <button
        key={value}
        type="button"
        onClick={() =>
          setLevels((prev) => (on ? prev.filter((l) => l !== value) : [...prev, value]))
        }
        style={{
          padding: '3px 10px',
          borderRadius: '999px',
          fontSize: '0.7rem',
          fontWeight: 700,
          cursor: 'pointer',
          border: `1px solid ${on ? style.fg : 'rgba(255,255,255,0.12)'}`,
          background: on ? style.bg : 'transparent',
          color: on ? style.fg : '#94a3b8',
        }}
      >
        {value}
      </button>
    )
  }

  const controls: React.CSSProperties = {
    background: '#0b1220',
    border: '1px solid rgba(255,255,255,0.12)',
    color: '#e2e8f0',
    borderRadius: 8,
    padding: '6px 10px',
    fontSize: '0.75rem',
  }

  return (
    <div dir={isRtl ? 'rtl' : 'ltr'}>
      {/* -------------------------------------------------- filters + paging */}
      <div
        style={{
          display: 'flex',
          flexWrap: 'wrap',
          gap: 8,
          alignItems: 'center',
          padding: '10px 12px',
          background: 'rgba(15, 23, 42, 0.5)',
          border: '1px solid rgba(255,255,255,0.06)',
          borderRadius: 10,
          marginBottom: 12,
        }}
      >
        <div style={{ display: 'flex', gap: 4 }}>{['ERROR', 'WARN', 'INFO', 'DEBUG'].map(chip)}</div>
        <input
          placeholder={isRtl ? 'جستجو در پیام و جزئیات…' : 'Search message + detail…'}
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') setAppliedSearch(search.trim())
          }}
          style={{ ...controls, minWidth: 190, flex: 1 }}
        />
        <button
          type="button"
          onClick={() => setAppliedSearch(search.trim())}
          style={{ ...controls, cursor: 'pointer', fontWeight: 600 }}
        >
          {isRtl ? 'جستجو' : 'Search'}
        </button>
        <select
          value={hours}
          onChange={(e) => setHours(Number(e.target.value))}
          style={controls}
          title={isRtl ? 'بازه زمانی' : 'Time window'}
        >
          <option value={0}>{isRtl ? 'همه زمان‌ها' : 'All time'}</option>
          <option value={1}>{isRtl ? '۱ ساعت اخیر' : 'Last hour'}</option>
          <option value={24}>{isRtl ? '۲۴ ساعت اخیر' : 'Last 24h'}</option>
          <option value={168}>{isRtl ? '۷ روز اخیر' : 'Last 7d'}</option>
          <option value={720}>{isRtl ? '۳۰ روز اخیر' : 'Last 30d'}</option>
        </select>
        <select
          value={order}
          onChange={(e) => setOrder(e.target.value as 'asc' | 'desc')}
          style={controls}
          title={isRtl ? 'ترتیب' : 'Order'}
        >
          <option value="desc">{isRtl ? 'جدیدترین اول' : 'Newest first'}</option>
          <option value="asc">{isRtl ? 'قدیمی‌ترین اول' : 'Oldest first'}</option>
        </select>
        <select
          value={pageSize}
          onChange={(e) => setPageSize(Number(e.target.value))}
          style={controls}
        >
          {[25, 50, 100, 200].map((n) => (
            <option key={n} value={n}>
              {n} / {isRtl ? 'صفحه' : 'page'}
            </option>
          ))}
        </select>
        <button
          type="button"
          onClick={() => void load()}
          disabled={loading}
          style={{ ...controls, cursor: loading ? 'wait' : 'pointer', display: 'flex', gap: 5, alignItems: 'center' }}
        >
          <RefreshCw size={12} /> {isRtl ? 'بروزرسانی' : 'Refresh'}
        </button>
        <button
          type="button"
          onClick={() => void handleExport('json')}
          disabled={exporting || total === 0}
          style={{ ...controls, cursor: 'pointer', display: 'flex', gap: 5, alignItems: 'center' }}
        >
          <Download size={12} /> JSON
        </button>
        <button
          type="button"
          onClick={() => void handleExport('csv')}
          disabled={exporting || total === 0}
          style={{ ...controls, cursor: 'pointer', display: 'flex', gap: 5, alignItems: 'center' }}
        >
          <Download size={12} /> CSV
        </button>
      </div>

      {/* ------------------------------------------------------- range badge */}
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 10, fontSize: '0.72rem' }}>
        <span className="badge badge-gray">
          {total === 0
            ? isRtl ? 'بدون لاگ' : 'no log events'
            : `${first}–${last} / ${total.toLocaleString()}`}
        </span>
        <button
          type="button"
          onClick={() => setOffset(Math.max(0, offset - pageSize))}
          disabled={offset === 0 || loading}
          style={{ ...controls, padding: '3px 10px', cursor: offset === 0 ? 'not-allowed' : 'pointer' }}
        >
          <ChevronUp size={12} style={{ verticalAlign: 'middle' }} /> {isRtl ? 'جدیدتر' : 'Newer'}
        </button>
        <button
          type="button"
          onClick={() => setOffset(offset + pageSize)}
          disabled={!page?.has_more || loading}
          style={{
            ...controls,
            padding: '3px 10px',
            cursor: page?.has_more ? 'pointer' : 'not-allowed',
          }}
        >
          <ChevronDown size={12} style={{ verticalAlign: 'middle' }} /> {isRtl ? 'قدیمی‌تر' : 'Older'}
        </button>
        {exporting && <span style={{ color: '#93c5fd' }}>{isRtl ? 'در حال آماده‌سازی…' : 'preparing export…'}</span>}
      </div>

      {error && (
        <div
          style={{
            display: 'flex',
            gap: 8,
            alignItems: 'flex-start',
            background: '#450a0a',
            border: '1px solid #7f1d1d',
            borderRadius: 10,
            padding: 10,
            color: '#fecaca',
            fontSize: '0.75rem',
            marginBottom: 10,
          }}
        >
          <AlertCircle size={13} style={{ marginTop: 2, flexShrink: 0 }} />
          <span>{error}</span>
        </div>
      )}

      {/* ------------------------------------------------------------- rows */}
      <div
        style={{
          background: '#070b14',
          border: '1px solid rgba(255,255,255,0.08)',
          borderRadius: 12,
          padding: '8px 10px',
          maxHeight: 440,
          overflowY: 'auto',
          fontFamily: "'JetBrains Mono', Consolas, Monaco, monospace",
          fontSize: '0.75rem',
        }}
      >
        {loading && !logs.length ? (
          <div style={{ textAlign: 'center', padding: '28px 0', color: '#64748b' }}>
            <Loader2 size={18} className="spin" /> {isRtl ? 'بارگذاری…' : 'loading…'}
          </div>
        ) : logs.length === 0 ? (
          <div style={{ textAlign: 'center', padding: '28px 0', color: '#64748b' }}>
            {isRtl ? 'رویدادی برای این تسک ثبت نشده است.' : 'No log events recorded for this task yet.'}
          </div>
        ) : (
          logs.map((item, idx) => {
            const key = `${item.ts}-${idx}-${item.correlation_id || ''}`
            const style = levelStyle(item.level)
            const isOpen = expanded.has(key)
            const lvl = (item.level || 'INFO').toUpperCase()
            return (
              <div
                key={key}
                style={{
                  padding: '6px 8px',
                  borderRadius: 6,
                  marginBottom: 4,
                  background: lvl === 'ERROR' ? 'rgba(239, 68, 68, 0.06)' : 'rgba(255,255,255,0.015)',
                  borderLeft: isRtl ? 'none' : `3px solid ${style.fg}`,
                  borderRight: isRtl ? `3px solid ${style.fg}` : 'none',
                }}
              >
                <div style={{ display: 'flex', gap: 8, alignItems: 'flex-start' }}>
                  <button
                    type="button"
                    onClick={() => toggle(key)}
                    style={{ background: 'none', border: 'none', color: '#64748b', cursor: 'pointer', padding: 0, marginTop: 2 }}
                    title={isRtl ? 'نمایش جزئیات' : 'Show detail'}
                  >
                    {isOpen ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                  </button>
                  <span style={{ color: '#475569', flexShrink: 0, fontSize: '0.68rem' }}>
                    {new Date(item.ts * 1000).toLocaleTimeString()}
                  </span>
                  <span
                    style={{
                      color: style.fg,
                      background: style.bg,
                      borderRadius: 4,
                      padding: '1px 6px',
                      fontSize: '0.62rem',
                      fontWeight: 700,
                      flexShrink: 0,
                    }}
                  >
                    {lvl}
                  </span>
                  <span style={{ color: '#94a3b8', flexShrink: 0, fontSize: '0.68rem' }}>
                    {item.service || 'core'}/{item.category || 'sys'}
                  </span>
                  <span style={{ color: '#e2e8f0', flex: 1, wordBreak: 'break-word' }}>
                    {item.message}
                  </span>
                  <button
                    type="button"
                    onClick={() => void copyRow(item, key)}
                    style={{ background: 'none', border: 'none', color: copied === key ? '#34d399' : '#64748b', cursor: 'pointer', padding: 0 }}
                    title={isRtl ? 'کپی' : 'Copy row'}
                  >
                    <Copy size={11} />
                  </button>
                </div>
                {(isOpen || item.correlation_id) && (
                  <div style={{ marginTop: 5, marginInlineStart: 20, fontSize: '0.68rem' }}>
                    {item.correlation_id && (
                      <div style={{ color: '#64748b' }}>
                        corr: <span style={{ color: '#93c5fd' }}>{item.correlation_id}</span>
                      </div>
                    )}
                    {isOpen && item.detail && (
                      <pre
                        style={{
                          margin: '5px 0 2px',
                          padding: '6px 8px',
                          background: '#0b1220',
                          borderRadius: 6,
                          color: '#cbd5e1',
                          whiteSpace: 'pre-wrap',
                          wordBreak: 'break-word',
                          maxHeight: 200,
                          overflowY: 'auto',
                        }}
                      >
                        {(() => {
                          try {
                            return JSON.stringify(JSON.parse(item.detail), null, 2)
                          } catch {
                            return item.detail
                          }
                        })()}
                      </pre>
                    )}
                  </div>
                )}
              </div>
            )
          })
        )}
      </div>

      <div style={{ fontSize: '0.68rem', color: '#64748b', marginTop: 8 }}>
        {isRtl
          ? 'این پنل کل تاریخچهٔ این تسک را با صفحه‌بندی نشان می‌دهد؛ جستجو روی پیام و جزئیات ساختاریافته اعمال می‌شود.'
          : 'This panel pages through the task’s entire history — search matches both the message and the structured detail.'}
      </div>
    </div>
  )
}
