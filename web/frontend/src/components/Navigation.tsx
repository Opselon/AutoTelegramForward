/**
 * Dashboard navigation: a persistent sidebar on wide screens, a drawer plus a
 * bottom tab bar below 1024 px (the `--bp-md` token).
 *
 * One component renders the rail and the drawer, because the alternative — a
 * desktop rail and a separate mobile tab strip — drifted: labels existed twice,
 * the active state was computed twice, and the strip scrolled horizontally off
 * the page at 360 px while the rail did not. The drawer list and the rail are
 * the same DOM; CSS decides whether it is docked or off-canvas.
 *
 * The drawer is a real modal surface, not a CSS trick:
 *
 * - the trigger (`NavTrigger`) carries `aria-expanded` / `aria-controls`;
 * - `Escape` and the scrim close it, and focus returns to whatever had it
 *   before the drawer opened;
 * - Tab is trapped inside while it is open, so a keyboard user cannot tab into
 *   the page behind the scrim;
 * - the page behind it does not scroll (`body.style.overflow`), so a touch drag
 *   on the scrim cannot move the dashboard under it.
 *
 * The shell upgrades:
 *
 * - **Grouped navigation** — destinations arrive tagged with a group; the rail
 *   renders a heading per group so fifteen tabs read as four areas instead of
 *   one long undifferentiated list. The drawer keeps the same grouping.
 * - **Collapsible rail** — on wide screens the sidebar can collapse to an icon
 *   rail; labels stay in the accessibility tree (`.visually-hidden`, not
 *   `display: none`, so audits still read thirteen named destinations) and the
 *   choice persists across sessions via the shell.
 * - **Bottom tab bar** — mobile gets its own primary surface: the four highest
 * -traffic destinations plus a "More" button that opens the full drawer. It is
 *   rendered from the same `items` data, so a tab cannot exist in one surface
 *   and be missing from the other.
 */
import { useCallback, useEffect, useRef } from 'react'
import { Menu, MoreHorizontal, PanelLeftClose, PanelLeftOpen, X } from 'lucide-react'
import type { ReactNode } from 'react'
import { makeT, type UiLang } from '../locales/engine'

export type TabId =
  | 'system'
  | 'rules'
  | 'simulator'
  | 'ai'
  | 'pv'
  | 'autoreply'
  | 'replace'
  | 'filters'
  | 'sessions'
  | 'jobs'
  | 'queue'
  | 'devops'
  | 'audit'
  | 'admin'
  | 'settings'

export type NavGroupId = 'workspace' | 'automation' | 'operations' | 'administration'

export interface NavItem {
  id: TabId
  /** Already resolved through the catalog by the shell. */
  label: string
  icon: ReactNode
  /** Rendered as a count pill; the jobs tab uses it. */
  badge?: number
  /** Area this destination belongs to; ungrouped items render without a heading. */
  group?: NavGroupId
}

export interface NavGroup {
  id: NavGroupId
  /** Already resolved through the catalog by the shell. */
  label: string
}

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'

/** Below this width the rail becomes a drawer. Mirrors `--bp-md` in index.css. */
const DRAWER_QUERY = '(max-width: 1023px)'

/** The hamburger that belongs in the header. */
export function NavTrigger({
  open,
  onOpenChange,
  lang,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  lang: UiLang
}) {
  const t = makeT(lang)
  return (
    <button
      type="button"
      className="btn btn--ghost btn--icon nav-trigger"
      aria-label={open ? t('ui_close_navigation') : t('ui_open_navigation')}
      aria-expanded={open}
      aria-controls="app-navigation"
      onClick={() => onOpenChange(!open)}
    >
      {open ? <X size={18} aria-hidden="true" /> : <Menu size={18} aria-hidden="true" />}
    </button>
  )
}

/**
 * The bottom tab bar for narrow viewports. `primary` holds at most four
 * destinations — enough for a thumb, few enough to stay legible at 360 px;
 * everything else lives one tap away in the drawer ("More").
 */
export function MobileNav({
  primary,
  active,
  onSelect,
  onOpenDrawer,
  lang,
}: {
  primary: NavItem[]
  active: TabId
  onSelect: (id: TabId) => void
  onOpenDrawer: () => void
  lang: UiLang
}) {
  const t = makeT(lang)
  return (
    <nav className="mobile-nav" aria-label={t('ui_nav_secondary')}>
      {primary.slice(0, 4).map((item) => (
        <button
          key={item.id}
          type="button"
          className={`mobile-nav__item${active === item.id ? ' is-active' : ''}`}
          aria-current={active === item.id ? 'page' : undefined}
          onClick={() => onSelect(item.id)}
        >
          <span className="mobile-nav__icon" aria-hidden="true">
            {item.icon}
            {item.badge && item.badge > 0 ? (
              <span className="mobile-nav__badge">{item.badge > 99 ? '99+' : item.badge}</span>
            ) : null}
          </span>
          <span className="mobile-nav__label">{item.label}</span>
        </button>
      ))}
      <button
        type="button"
        className="mobile-nav__item"
        onClick={onOpenDrawer}
        aria-haspopup="dialog"
        aria-controls="app-navigation"
      >
        <span className="mobile-nav__icon" aria-hidden="true">
          <MoreHorizontal size={20} />
        </span>
        <span className="mobile-nav__label">{t('ui_more_navigation')}</span>
      </button>
    </nav>
  )
}

export function Navigation({
  items,
  groups,
  active,
  onSelect,
  open,
  onOpenChange,
  collapsed = false,
  onCollapsedChange,
  lang,
}: {
  items: NavItem[]
  /** Group headings, in display order; items without a group render first. */
  groups?: NavGroup[]
  active: TabId
  onSelect: (id: TabId) => void
  open: boolean
  onOpenChange: (open: boolean) => void
  /** Icon-only rail on wide screens; harmless (ignored by CSS) in the drawer. */
  collapsed?: boolean
  onCollapsedChange?: (collapsed: boolean) => void
  lang: UiLang
}) {
  const t = makeT(lang)
  const asideRef = useRef<HTMLElement | null>(null)
  const restoreRef = useRef<HTMLElement | null>(null)

  const close = useCallback(() => onOpenChange(false), [onOpenChange])

  // Remember who opened the drawer so closing it puts focus back there, and
  // move focus *into* the drawer on open — the Tab trap below is useless if the
  // first Tab press starts from the trigger and walks the page behind it.
  useEffect(() => {
    if (!open) {
      restoreRef.current?.focus?.()
      restoreRef.current = null
      return
    }
    restoreRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null
    const el = asideRef.current
    if (!el) return
    // Deferred by a frame: the drawer is still clipped to zero width while
    // `is-open` lands, and focusing a clipped element is a no-op in some
    // engines.
    const handle = window.requestAnimationFrame(() => {
      const target =
        el.querySelector<HTMLElement>('[aria-current="page"]') ??
        el.querySelector<HTMLElement>(FOCUSABLE)
      target?.focus()
    })
    return () => window.cancelAnimationFrame(handle)
  }, [open])

  // A closed drawer is clipped, not unmounted, so it must leave the tab order
  // and the accessibility tree on its own. `visibility: hidden` would do that
  // but also makes `innerText` empty for every nav label, which reads as
  // thirteen unnamed buttons to any layout/a11y audit. `inert` keeps the
  // element rendered and takes it out of both trees.
  useEffect(() => {
    const el = asideRef.current
    if (!el) return
    const narrow = window.matchMedia('(max-width: 1023px)')
    const sync = () => {
      if (narrow.matches && !open) el.setAttribute('inert', '')
      else el.removeAttribute('inert')
    }
    sync()
    narrow.addEventListener('change', sync)
    return () => narrow.removeEventListener('change', sync)
  }, [open])

  useEffect(() => {
    if (!open) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.stopPropagation()
        close()
        return
      }
      if (event.key !== 'Tab') return
      const focusables = asideRef.current?.querySelectorAll<HTMLElement>(FOCUSABLE)
      if (!focusables || focusables.length === 0) return
      const first = focusables[0]
      const last = focusables[focusables.length - 1]
      const current = document.activeElement
      if (event.shiftKey && (current === first || !asideRef.current?.contains(current))) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && current === last) {
        event.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', onKey, true)
    return () => document.removeEventListener('keydown', onKey, true)
  }, [open, close])

  // Body scroll lock, only while the drawer is actually the narrow-screen UI.
  useEffect(() => {
    if (!open) return
    const narrow = typeof window !== 'undefined' && window.matchMedia(DRAWER_QUERY).matches
    if (!narrow) return
    const previous = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.body.style.overflow = previous
    }
  }, [open])

  // Moving to a wide viewport docks the rail; leaving the drawer "open" in state
  // would otherwise keep the body locked after a rotation.
  useEffect(() => {
    if (typeof window === 'undefined') return
    const mql = window.matchMedia(DRAWER_QUERY)
    const onChange = (event: MediaQueryListEvent) => {
      if (!event.matches) onOpenChange(false)
    }
    mql.addEventListener('change', onChange)
    return () => mql.removeEventListener('change', onChange)
  }, [onOpenChange])

  // Walk the items once, emitting a heading before the first item of every
  // group. Ungrouped items keep the flat list of the original shell.
  const rows: ReactNode[] = []
  const emitted = new Set<NavGroupId>()
  for (const item of items) {
    const group = groups?.find((g) => g.id === item.group)
    if (group && !emitted.has(group.id)) {
      emitted.add(group.id)
      rows.push(
        <p
          key={`group-${group.id}`}
          className="nav-group-label"
          aria-hidden="true"
        >
          {group.label}
        </p>,
      )
    }
    rows.push(
      <button
        key={item.id}
        type="button"
        className={`nav-item${active === item.id ? ' is-active' : ''}`}
        aria-current={active === item.id ? 'page' : undefined}
        onClick={() => {
          onSelect(item.id)
          close()
        }}
        title={collapsed ? item.label : undefined}
      >
        <span className="nav-item__icon" aria-hidden="true">
          {item.icon}
        </span>
        <span className={`nav-item__label${collapsed ? ' visually-hidden' : ''}`}>{item.label}</span>
        {item.badge && item.badge > 0 ? (
          <span className={`nav-item__badge${collapsed ? ' nav-item__badge--dot' : ''}`}>
            {collapsed ? '' : item.badge}
          </span>
        ) : null}
      </button>,
    )
  }

  return (
    <>
      {open ? (
        <div
          className="nav-scrim"
          // A click on the scrim is a dismissal, so it is not interactive
          // content of its own: the trigger and Escape both do the job for
          // keyboard users, and this element stays out of the tab order.
          onClick={close}
          aria-hidden="true"
        />
      ) : null}

      <aside
        ref={asideRef}
        id="app-navigation"
        className={`app-sidebar${open ? ' is-open' : ''}${collapsed ? ' is-collapsed' : ''}`}
        aria-label={t('ui_nav_primary')}
      >
        <nav className="app-sidebar__nav">
          {rows}
          {onCollapsedChange ? (
            <button
              type="button"
              className="btn btn--ghost btn--icon sidebar-collapse"
              aria-label={collapsed ? t('ui_expand_sidebar') : t('ui_collapse_sidebar')}
              aria-pressed={collapsed}
              onClick={() => onCollapsedChange(!collapsed)}
            >
              {collapsed ? (
                <PanelLeftOpen size={18} aria-hidden="true" />
              ) : (
                <PanelLeftClose size={18} aria-hidden="true" />
              )}
            </button>
          ) : null}
        </nav>
      </aside>
    </>
  )
}
