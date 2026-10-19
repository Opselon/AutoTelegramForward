/**
 * The paid/free add-on catalogue, in one place.
 *
 * It used to exist three times in `TaskOverviewModal`: the catalogue tab, the
 * six-card grid on the overview, and a third list of titles — all in two
 * languages, so a Russian or Chinese user saw the same feature under an
 * English name with no name of their own at all.
 *
 * The names are catalog keys now, so the ten locales come from one list and the
 * API key (`key`) stays the identifier the server expects.
 */
export interface AddonDef {
  key: string
  /** Catalog key for the add-on's name. */
  labelKey: string
  /** Catalog key for the one-line description under the name. */
  descKey: string
  /** Locked behind a paid plan; shown, but not switchable from the panel. */
  locked: boolean
}

export const ADDONS: AddonDef[] = [
  {
    key: 'channel_comments',
    labelKey: 'ui_addon_channel_comments',
    descKey: 'ui_addon_channel_comments_desc',
    locked: false,
  },
  {
    key: 'every_n',
    labelKey: 'ui_addon_every_n',
    descKey: 'ui_addon_every_n_desc',
    locked: true,
  },
  {
    key: 'kw_confirm',
    labelKey: 'ui_addon_kw_confirm',
    descKey: 'ui_addon_kw_confirm_desc',
    locked: true,
  },
  {
    key: 'kw_reply',
    labelKey: 'ui_addon_kw_reply',
    descKey: 'ui_addon_kw_reply_desc',
    locked: true,
  },
  {
    key: 'auto_del',
    labelKey: 'ui_addon_auto_del',
    descKey: 'ui_addon_auto_del_desc',
    locked: true,
  },
  {
    key: 'webhook',
    labelKey: 'ui_addon_webhook',
    descKey: 'ui_addon_webhook_desc',
    locked: true,
  },
  {
    key: 'crypto',
    labelKey: 'ui_addon_crypto',
    descKey: 'ui_addon_crypto_desc',
    locked: true,
  },
  {
    key: 'affiliate',
    labelKey: 'ui_addon_affiliate',
    descKey: 'ui_addon_affiliate_desc',
    locked: true,
  },
  {
    key: 'link_buttons',
    labelKey: 'ui_addon_link_buttons',
    descKey: 'ui_addon_link_buttons_desc',
    locked: false,
  },
  {
    key: 'watermark',
    labelKey: 'ui_addon_watermark',
    descKey: 'ui_addon_watermark_desc',
    locked: true,
  },
  {
    key: 'duplicate',
    labelKey: 'ui_addon_duplicate',
    descKey: 'ui_addon_duplicate_desc',
    locked: false,
  },
  {
    key: 'cleaner',
    labelKey: 'ui_addon_cleaner',
    descKey: 'ui_addon_cleaner_desc',
    locked: false,
  },
  {
    key: 'scheduler',
    labelKey: 'ui_addon_scheduler',
    descKey: 'ui_addon_scheduler_desc',
    locked: false,
  },
  {
    key: 'power_schedule',
    labelKey: 'ui_addon_power_schedule',
    descKey: 'ui_addon_power_schedule_desc',
    locked: false,
  },
  {
    key: 'raw_text',
    labelKey: 'ui_addon_raw_text',
    descKey: 'ui_addon_raw_text_desc',
    locked: false,
  },
  {
    key: 'button_to_text',
    labelKey: 'ui_addon_button_to_text',
    descKey: 'ui_addon_button_to_text_desc',
    locked: false,
  },
  {
    key: 'ocr_ai',
    labelKey: 'ui_addon_ocr_ai',
    descKey: 'ui_addon_ocr_ai_desc',
    locked: false,
  },
  {
    key: 'ai_rewrite',
    labelKey: 'ui_addon_ai_rewrite',
    descKey: 'ui_addon_ai_rewrite_desc',
    locked: false,
  },
]

/** The six features the overview grid shows, in catalogue order. */
export const OVERVIEW_ADDONS = ADDONS.slice(0, 6)
