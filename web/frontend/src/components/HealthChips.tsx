/**
 * The one service-health chip row: header and home hero render the same
 * component instead of two hand-maintained copies that drift (they already
 * disagreed once about where "web" came from).
 *
 * A chip must say what it means. The dot alone turned "not probed yet"
 * (undefined on first paint) into the same red as a real outage, and a real
 * outage named no service — the operator had to open the DevOps panel to
 * find out which one. Verdict text uses the same catalog keys the System
 * panel uses, so every surface agrees word for word.
 */
import type { UiLang } from '../locales/engine'
import { makeT } from '../locales/engine'

export function HealthChips({
  lang,
  core,
  logger,
  web,
}: {
  lang: UiLang
  /** undefined = not probed yet, never painted as an outage. */
  core: boolean | undefined
  logger: boolean | undefined
  web: boolean | undefined
}) {
  const t = makeT(lang)
  const healthVerdict = (online: boolean | undefined) =>
    online === undefined
      ? t('ui_probing')
      : online
        ? t('ui_system_online')
        : t('ui_system_offline')

  const chips = [
    { label: 'Core:50051', online: core },
    { label: 'Logger:50052', online: logger },
    { label: 'Web:8088', online: web },
  ] as const

  return (
    <>
      {chips.map((chip) => (
        <div
          key={chip.label}
          className={`header-chip${chip.online === false ? ' header-chip--down' : ''}`}
          title={`${chip.label} — ${healthVerdict(chip.online)}`}
        >
          <span
            className={`header-chip__dot${chip.online ? ' is-online' : ''}${chip.online === undefined ? ' is-unknown' : ''}`}
            aria-hidden="true"
          />
          <span>{chip.label}</span>
          <span className="header-chip__verdict">{healthVerdict(chip.online)}</span>
        </div>
      ))}
    </>
  )
}
