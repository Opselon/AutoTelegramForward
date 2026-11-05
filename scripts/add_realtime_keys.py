#!/usr/bin/env python3
"""Add the §11 real-time-quality keys (staleness + last update) to every locale."""
import io
import os

NEW = {
    "ui_stale_data": {
        "en": "Stale ({age}s old)",
        "fa": "قدیمی ({age} ثانیه)",
        "pt": "Obsoleto ({age}s)",
        "uz": "Eski ({age}s)",
        "ru": "Устарело ({age}s)",
        "id": "Stale ({age}s)",
        "es": "Obsoleto ({age}s)",
        "fr": "Obsolète ({age}s)",
        "it": "Obsoleto ({age}s)",
        "zh": "已过期（{age}秒）",
    },
    "ui_stale_data_hint": {
        "en": "Data this old is labelled stale, not shown as live: the last poll failed and these are the most recent numbers the worker answered with.",
        "fa": "داده‌های این قدیمی به‌جای زنده نشان‌ داده می‌شوند قدیمی برچسب می‌خورند: آخرین نظرسنجی ناموفق بود و این‌ها آخرین اعدادی هستند که کارگر با آن‌ها پاسخ داد.",
        "pt": "Dados tão antigos são marcados como obsoletos, não mostrados como ao vivo: a última pesquisa falhou e estes são os números mais recentes que o worker forneceu.",
        "uz": "Shunchalik eski ma'lumotlar jonli emas, eskilan deb belgilanadi: oxirgi so'rov amalga oshmadi va bular worker javob bergan eng so'nggi raqamlar.",
        "ru": "Такие старые данные помечаются как устаревшие, а не показываются живыми: последний опрос завершился неудачей, и это последние цифры, которые ответил воркер.",
        "id": "Data yang sudah lama diberi label stale, bukan ditampilkan sebagai live: poll terakhir gagal dan ini adalah angka terbaru yang dijawab worker.",
        "es": "Los datos tan antiguos se marcan como obsoletos, no como en vivo: el último sondeo falló y estos son los números más recientes que el worker respondió.",
        "fr": "Les données aussi anciennes sont marquées comme obsolètes, jamais comme 'en direct' : le dernier sondage a échoué et ce sont les derniers chiffres renvoyés par le worker.",
        "it": "I dati così vecchi sono etichettati come obsoleti, non mostrati come live: l'ultimo poll è fallito e questi sono i numeri più recenti ricevuti dal worker.",
        "zh": "如此旧的数据会被标记为过期，而不会显示为实时：上次轮询失败，这些是 worker 最近一次回答的数据。",
    },
    "ui_last_update_hint": {
        "en": "The most recent poll succeeded; this is live data from the worker.",
        "fa": "آخرین نظرسنجی موفق بود؛ این داده‌های زنده از کارگر است.",
        "pt": "A pesquisa mais recente foi bem-sucedida; estes são dados ao vivo do worker.",
        "uz": "Oxirgi so'rov muvaffaqiyatli; bu workerdan jonli ma'lumot.",
        "ru": "Последний опрос успешен; это живые данные от воркера.",
        "id": "Poll terbaru berhasil; ini adalah data langsung dari worker.",
        "es": "El sondeo más reciente tuvo éxito; estos son datos en vivo del worker.",
        "fr": "Le dernier sondage a réussi ; ce sont des données en direct du worker.",
        "it": "L'ultimo poll è andato a buon fine; questi sono dati live dal worker.",
        "zh": "最近一次轮询成功；这是来自 worker 的实时数据。",
    },
}

LOCALES = ["en", "fa", "pt", "uz", "ru", "id", "es", "fr", "it", "zh"]
BASE = "/root/AutoTelegramForward/web/frontend/src/locales"

for loc in LOCALES:
    path = os.path.join(BASE, f"{loc}.ts")
    with io.open(path, encoding="utf-8") as fh:
        src = fh.read()
    missing = [k for k in NEW if f'"{k}":' not in src]
    if not missing:
        print(f"{loc}: up to date")
        continue
    body = "".join(f'  "{k}": "{NEW[k][loc]}",\n' for k in missing)
    idx = src.rstrip().rfind("}")
    assert idx > 0, f"no closing brace in {path}"
    while idx > 0 and src[idx - 1] in "\n":
        idx -= 1
    src = src[:idx] + body + "\n" + src[idx:]
    with io.open(path, "w", encoding="utf-8") as fh:
        fh.write(src)
    print(f"{loc}: added {len(missing)} keys")
