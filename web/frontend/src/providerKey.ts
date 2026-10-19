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
 * The reasons carry all ten languages the product ships — the two-language
 * version answered a Russian or Chinese tenant in English, and the code list is
 * closed (`core`'s probe), so it can be complete here. Kept free of DOM,
 * framework and catalog imports so CI can compile and assert on this file alone.
 */

export type KeyCheckResult = {
  success: boolean
  healthy: boolean
  latency_ms: number
  detail?: string | null
  error_code?: string | null
}

/** The ten locales, in the same order as the dashboard catalog. */
export type KeyCheckLang = 'en' | 'fa' | 'pt' | 'uz' | 'ru' | 'id' | 'es' | 'fr' | 'it' | 'zh'

interface Reason {
  en: string
  fa: string
  pt: string
  uz: string
  ru: string
  id: string
  es: string
  fr: string
  it: string
  zh: string
}

/** Why a probe did not come back healthy, said plainly in every language. */
export const KEY_CHECK_REASONS: Record<string, Reason> = {
  invalid_key: {
    en: 'The provider rejected this key',
    fa: 'ارائه‌دهنده این کلید را نپذیرفت',
    pt: 'O provedor rejeitou esta chave',
    uz: 'Provayder bu kalitni rad etdi',
    ru: 'Провайдер отклонил этот ключ',
    id: 'Penyedia menolak kunci ini',
    es: 'El proveedor rechazó esta clave',
    fr: 'Le fournisseur a refusé cette clé',
    it: 'Il provider ha rifiutato questa chiave',
    zh: '提供商拒绝了此密钥',
  },
  missing_key: {
    en: 'Enter an API key',
    fa: 'کلید API را وارد کنید',
    pt: 'Informe uma chave de API',
    uz: 'API kalitini kiriting',
    ru: 'Введите API-ключ',
    id: 'Masukkan kunci API',
    es: 'Introduce una clave de API',
    fr: 'Saisissez une clé API',
    it: 'Inserisci una chiave API',
    zh: '请输入 API 密钥',
  },
  missing_endpoint: {
    en: 'Enter the provider endpoint',
    fa: 'آدرس سرویس‌دهنده را وارد کنید',
    pt: 'Informe o endpoint do provedor',
    uz: 'Provayder manzilini kiriting',
    ru: 'Укажите адрес провайдера',
    id: 'Masukkan endpoint penyedia',
    es: 'Introduce el endpoint del proveedor',
    fr: 'Saisissez le point de terminaison du fournisseur',
    it: "Inserisci l'endpoint del provider",
    zh: '请输入提供商端点',
  },
  endpoint_invalid: {
    en: 'The endpoint must be an http(s) URL',
    fa: 'آدرس باید با http یا https شروع شود',
    pt: 'O endpoint deve ser uma URL http(s)',
    uz: 'Manzil http(s) URL bo‘lishi kerak',
    ru: 'Адрес должен быть URL с http(s)',
    id: 'Endpoint harus berupa URL http(s)',
    es: 'El endpoint debe ser una URL http(s)',
    fr: 'Le point de terminaison doit être une URL http(s)',
    it: "L'endpoint deve essere un URL http(s)",
    zh: '端点必须是 http(s) 地址',
  },
  endpoint_blocked: {
    en: 'That address cannot be reached from the server',
    fa: 'این آدرس از سرور قابل دسترسی نیست',
    pt: 'Esse endereço não é alcançável a partir do servidor',
    uz: 'Bu manzilga serverdan kirish mumkin emas',
    ru: 'Этот адрес недоступен с сервера',
    id: 'Alamat itu tidak dapat dijangkau dari server',
    es: 'No se puede acceder a esa dirección desde el servidor',
    fr: "Cette adresse est injoignable depuis le serveur",
    it: 'Quell’indirizzo non è raggiungibile dal server',
    zh: '服务器无法访问该地址',
  },
  endpoint_unreachable: {
    en: 'Could not connect to the endpoint',
    fa: 'اتصال به آدرس ممکن نشد',
    pt: 'Não foi possível conectar ao endpoint',
    uz: 'Manzilga ulanib bo‘lmadi',
    ru: 'Не удалось подключиться к адресу',
    id: 'Tidak dapat terhubung ke endpoint',
    es: 'No se pudo conectar con el endpoint',
    fr: 'Impossible de se connecter au point de terminaison',
    it: "Impossibile connettersi all'endpoint",
    zh: '无法连接到端点',
  },
  timeout: {
    en: 'The endpoint did not answer in time',
    fa: 'پاسخ سرویس‌دهنده به‌موقع نرسید',
    pt: 'O endpoint não respondeu a tempo',
    uz: 'Manzil o‘z vaqtida javob bermadi',
    ru: 'Адрес не ответил вовремя',
    id: 'Endpoint tidak menjawab tepat waktu',
    es: 'El endpoint no respondió a tiempo',
    fr: "Le point de terminaison n'a pas répondu à temps",
    it: "L'endpoint non ha risposto in tempo",
    zh: '端点未及时响应',
  },
  rate_limited: {
    en: 'The key is valid but rate-limited right now',
    fa: 'کلید معتبر است اما فعلاً محدود شده',
    pt: 'A chave é válida, mas está limitada agora',
    uz: 'Kalit to‘g‘ri, ammo hozir cheklangan',
    ru: 'Ключ действителен, но сейчас ограничен',
    id: 'Kunci valid tetapi sedang dibatasi',
    es: 'La clave es válida pero está limitada ahora',
    fr: 'La clé est valide mais limitée pour le moment',
    it: 'La chiave è valida ma ora è limitata',
    zh: '密钥有效，但当前被限流',
  },
  provider_error: {
    en: 'The provider returned a server error',
    fa: 'سرویس‌دهنده خطای سرور برگرداند',
    pt: 'O provedor retornou um erro de servidor',
    uz: 'Provayder server xatosi qaytardi',
    ru: 'Провайдер вернул ошибку сервера',
    id: 'Penyedia mengembalikan kesalahan server',
    es: 'El proveedor devolvió un error de servidor',
    fr: 'Le fournisseur a renvoyé une erreur serveur',
    it: 'Il provider ha restituito un errore del server',
    zh: '提供商返回了服务器错误',
  },
  probe_rejected: {
    en: 'The provider refused the request',
    fa: 'سرویس‌دهنده درخواست را رد کرد',
    pt: 'O provedor recusou a solicitação',
    uz: 'Provayder so‘rovni rad etdi',
    ru: 'Провайдер отклонил запрос',
    id: 'Penyedia menolak permintaan',
    es: 'El proveedor rechazó la solicitud',
    fr: 'Le fournisseur a refusé la requête',
    it: 'Il provider ha rifiutato la richiesta',
    zh: '提供商拒绝了请求',
  },
  unknown_provider: {
    en: 'Unknown provider',
    fa: 'ارائه‌دهنده ناشناخته است',
    pt: 'Provedor desconhecido',
    uz: 'Noma’lum provayder',
    ru: 'Неизвестный провайдер',
    id: 'Penyedia tidak dikenal',
    es: 'Proveedor desconocido',
    fr: 'Fournisseur inconnu',
    it: 'Provider sconosciuto',
    zh: '未知提供商',
  },
  config_not_found: {
    en: 'That configuration is not available to you',
    fa: 'این پیکربندی در دسترس شما نیست',
    pt: 'Essa configuração não está disponível para você',
    uz: 'Bu konfiguratsiya sizga mavjud emas',
    ru: 'Эта конфигурация недоступна для вас',
    id: 'Konfigurasi itu tidak tersedia untuk Anda',
    es: 'Esa configuración no está disponible para ti',
    fr: "Cette configuration ne vous est pas accessible",
    it: 'Questa configurazione non è disponibile per te',
    zh: '此配置对您不可用',
  },
  probe_failed: {
    en: 'The check could not run',
    fa: 'بررسی انجام نشد',
    pt: 'Não foi possível executar a verificação',
    uz: 'Tekshiruv bajarilmadi',
    ru: 'Проверка не выполнена',
    id: 'Pemeriksaan tidak dapat dijalankan',
    es: 'No se pudo ejecutar la comprobación',
    fr: "La vérification n'a pas pu s'exécuter",
    it: 'Impossibile eseguire la verifica',
    zh: '无法执行检查',
  },
}

/** "Key accepted (215 ms)", in the caller's language. */
export const KEY_ACCEPTED: Record<KeyCheckLang, string> = {
  en: 'Key accepted ({ms} ms)',
  fa: 'کلید پذیرفته شد ({ms} میلی‌ثانیه)',
  pt: 'Chave aceita ({ms} ms)',
  uz: 'Kalit qabul qilindi ({ms} ms)',
  ru: 'Ключ принят ({ms} мс)',
  id: 'Kunci diterima ({ms} ms)',
  es: 'Clave aceptada ({ms} ms)',
  fr: 'Clé acceptée ({ms} ms)',
  it: 'Chiave accettata ({ms} ms)',
  zh: '密钥已接受（{ms} 毫秒）',
}

/** A probe that failed without naming a reason of its own. */
export const CHECK_FAILED: Record<KeyCheckLang, string> = {
  en: 'The check failed',
  fa: 'بررسی ناموفق بود',
  pt: 'A verificação falhou',
  uz: 'Tekshiruv muvaffaqiyatsiz tugadi',
  ru: 'Проверка не удалась',
  id: 'Pemeriksaan gagal',
  es: 'La comprobación falló',
  fr: 'La vérification a échoué',
  it: 'La verifica non è riuscita',
  zh: '检查失败',
}

const LANGS: KeyCheckLang[] = ['en', 'fa', 'pt', 'uz', 'ru', 'id', 'es', 'fr', 'it', 'zh']

/** A language the catalog knows; anything else answers in English. */
function pick(lang: string | null | undefined): KeyCheckLang {
  const raw = String(lang ?? '').trim().toLowerCase()
  const primary = raw.split(/[-_]/)[0] as KeyCheckLang
  return LANGS.includes(primary) ? primary : 'en'
}

function fill(template: string, params: Record<string, string | number>): string {
  return template.replace(/\{(\w+)\}/g, (whole, name) =>
    Object.prototype.hasOwnProperty.call(params, name) ? String(params[name]) : whole,
  )
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
  lang: KeyCheckLang | string,
  missingKey = false,
): KeyCheckView {
  const code = pick(lang)
  if (!result) {
    return { ok: false, text: '', detail: '' }
  }
  const reason = result.error_code ? KEY_CHECK_REASONS[result.error_code] : undefined
  if (result.healthy) {
    return {
      ok: true,
      text: fill(KEY_ACCEPTED[code], { ms: Math.round(result.latency_ms) }),
      detail: '',
    }
  }
  const text = reason
    ? reason[code] || reason.en
    : result.error_code || CHECK_FAILED[code]
  // A probe that never ran because there was nothing to probe with should point
  // at the field, not at the provider.
  if (missingKey && result.error_code === 'missing_key') {
    return { ok: false, text, detail: '' }
  }
  return { ok: false, text, detail: (result.detail || '').trim() }
}

/**
 * The reason a *save* was refused, said in the tenant's language.
 *
 * A blocked endpoint comes back from the API as `{"error":"endpoint_blocked"}`.
 * Printing that verbatim tells the user nothing they can act on, and it is the
 * one refusal they can fix themselves.
 */
export function describeSaveFailure(message: string, lang: KeyCheckLang | string): string {
  const code = (message || '').trim()
  const reason = KEY_CHECK_REASONS[code]
  if (reason) return reason[pick(lang)] || reason.en
  return code
}
