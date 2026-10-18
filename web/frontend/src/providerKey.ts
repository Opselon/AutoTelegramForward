/**
 * "Is my endpoint and key usable?" — the answer, in the tenant's language.
 *
 * The core answers the probe with `{success, healthy, latency_ms, detail,
 * error_code}`. `success` means the check ran and reached an answer; `healthy`
 * means the provider accepted the credentials. They are different questions and
 * the UI must not collapse them: a refused probe (`success: false`,
 * `error_code: "invalid_key"`) and a probe that never left the process
 * (`success: false`, `error_code: "endpoint_blocked"`) both need the reason, not
 * a spinner that stops.
 *
 * Kept free of DOM and framework code so CI can compile and assert on it.
 */

export type KeyCheckResult = {
  success: boolean
  healthy: boolean
  latency_ms: number
  detail?: string | null
  error_code?: string | null
}

/** Why a probe did not come back healthy, said plainly in both languages. */
export const KEY_CHECK_REASONS: Record<string, { en: string; fa: string }> = {
  invalid_key: { en: 'The provider rejected this key', fa: 'ارائه‌دهنده این کلید را نپذیرفت' },
  missing_key: { en: 'Enter an API key', fa: 'کلید API را وارد کنید' },
  missing_endpoint: { en: 'Enter the provider endpoint', fa: 'آدرس سرویس‌دهنده را وارد کنید' },
  endpoint_invalid: { en: 'The endpoint must be an http(s) URL', fa: 'آدرس باید با http یا https شروع شود' },
  endpoint_blocked: { en: 'That address cannot be reached from the server', fa: 'این آدرس از سرور قابل دسترسی نیست' },
  endpoint_unreachable: { en: 'Could not connect to the endpoint', fa: 'اتصال به آدرس ممکن نشد' },
  timeout: { en: 'The endpoint did not answer in time', fa: 'پاسخ سرویس‌دهنده به‌موقع نرسید' },
  rate_limited: { en: 'The key is valid but rate-limited right now', fa: 'کلید معتبر است اما فعلاً محدود شده' },
  provider_error: { en: 'The provider returned a server error', fa: 'سرویس‌دهنده خطای سرور برگرداند' },
  probe_rejected: { en: 'The provider refused the request', fa: 'سرویس‌دهنده درخواست را رد کرد' },
  unknown_provider: { en: 'Unknown provider', fa: 'ارائه‌دهنده ناشناخته است' },
  config_not_found: { en: 'That configuration is not available to you', fa: 'این پیکربندی در دسترس شما نیست' },
  probe_failed: { en: 'The check could not run', fa: 'بررسی انجام نشد' },
}

export type KeyCheckView = {
  /** true only when the provider accepted the credentials */
  ok: boolean
  /** one line, already localized */
  text: string
  /** the provider's own words, redacted server-side; '' when there are none */
  detail: string
}

export function describeKeyCheck(
  result: KeyCheckResult | null | undefined,
  isRtl: boolean,
  missingKey = false,
): KeyCheckView {
  if (!result) {
    return { ok: false, text: '', detail: '' }
  }
  const reason = result.error_code ? KEY_CHECK_REASONS[result.error_code] : undefined
  if (result.healthy) {
    return {
      ok: true,
      text: isRtl
        ? `کلید پذیرفته شد (${Math.round(result.latency_ms)} میلی‌ثانیه)`
        : `Key accepted (${Math.round(result.latency_ms)} ms)`,
      detail: '',
    }
  }
  const text = reason
    ? isRtl
      ? reason.fa
      : reason.en
    : result.error_code || (isRtl ? 'بررسی ناموفق بود' : 'The check failed')
  // A probe that never ran because there was nothing to probe with should point
  // at the field, not at the provider.
  if (missingKey && result.error_code === 'missing_key') {
    return { ok: false, text, detail: '' }
  }
  return { ok: false, text, detail: (result.detail || '').trim() }
}
