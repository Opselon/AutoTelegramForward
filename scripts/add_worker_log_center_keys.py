#!/usr/bin/env python3
"""Add the WorkerLogCenter keys to every locale catalog (10-language parity)."""
import io
import os

# key -> {locale: value}
NEW = {
    "ui_worker_log_center": {
        "en": "Worker Log Center",
        "fa": "مرکز لاگ ورکر",
        "pt": "Central de Logs do Worker",
        "uz": "Worker Log Markazi",
        "ru": "Центр логов воркера",
        "id": "Pusat Log Worker",
        "es": "Centro de Registros del Worker",
        "fr": "Centre de Journaux du Worker",
        "it": "Centro Log del Worker",
        "zh": "Worker 日志中心",
    },
    "ui_live_tail": {
        "en": "Live tail",
        "fa": "دنباله زنده",
        "pt": "Transmissão ao vivo",
        "uz": "Jonli oqim",
        "ru": "Живой хвост",
        "id": "Langsung",
        "es": "Transmisión en vivo",
        "fr": "Flux en direct",
        "it": "Flusso live",
        "zh": "实时跟踪",
    },
    "ui_export_csv": {
        "en": "Export CSV",
        "fa": "خروجی CSV",
        "pt": "Exportar CSV",
        "uz": "CSV eksport",
        "ru": "Экспорт CSV",
        "id": "Ekspor CSV",
        "es": "Exportar CSV",
        "fr": "Exporter CSV",
        "it": "Esporta CSV",
        "zh": "导出 CSV",
    },
    "ui_search_logs": {
        "en": "Search logs…",
        "fa": "جستجوی لاگ…",
        "pt": "Pesquisar logs…",
        "uz": "Loglarni qidirish…",
        "ru": "Поиск по логам…",
        "id": "Cari log…",
        "es": "Buscar registros…",
        "fr": "Rechercher dans les journaux…",
        "it": "Cerca log…",
        "zh": "搜索日志…",
    },
    "ui_log_fetch_failed": {
        "en": "Log fetch failed",
        "fa": "دریافت لاگ ناموفق بود",
        "pt": "Falha ao buscar logs",
        "uz": "Loglarni olish amalga oshmadi",
        "ru": "Не удалось получить логи",
        "id": "Pengambilan log gagal",
        "es": "Error al obtener registros",
        "fr": "Échec de la récupération des journaux",
        "it": "Recupero log fallito",
        "zh": "日志获取失败",
    },
    "ui_log_empty": {
        "en": "No log entries match these filters.",
        "fa": "هیچ لاگ‌ای با این فیلترها مطابقت ندارد.",
        "pt": "Nenhum registro corresponde a estes filtros.",
        "uz": "Bu filtrlarga mos loglar yo‘q.",
        "ru": "Нет записей, соответствующих этим фильтрам.",
        "id": "Tidak ada log yang cocok dengan filter ini.",
        "es": "Ningún registro coincide con estos filtros.",
        "fr": "Aucune entrée ne correspond à ces filtres.",
        "it": "Nessun log corrisponde a questi filtri.",
        "zh": "没有符合条件的日志。",
    },
    "ui_copy_diagnostics": {
        "en": "Copy sanitized diagnostics",
        "fa": "کپی تشخیص‌های پاک‌سازی‌شده",
        "pt": "Copiar diagnósticos sanitizados",
        "uz": "Tozalangan diagnostikani nusxalash",
        "ru": "Скопировать дезинфицированную диагностику",
        "id": "Salin diagnostik yang disanitasi",
        "es": "Copiar diagnósticos sanitizados",
        "fr": "Copier les diagnostics nettoyés",
        "it": "Copia diagnostica sanificata",
        "zh": "复制脱敏诊断",
    },
    "ui_load_more": {
        "en": "Load more",
        "fa": "بیشتر بارگذاری کنید",
        "pt": "Carregar mais",
        "uz": "Ko‘proq yuklash",
        "ru": "Загрузить ещё",
        "id": "Muat lebih banyak",
        "es": "Cargar más",
        "fr": "Charger plus",
        "it": "Carica altri",
        "zh": "加载更多",
    },
    "ui_log_count": {
        "en": "{shown} shown of {total}",
        "fa": "{shown} نمایش از {total}",
        "pt": "{shown} exibidos de {total}",
        "uz": "{total} dan {shown} ko‘rsatilgan",
        "ru": "Показано {shown} из {total}",
        "id": "{shown} ditampilkan dari {total}",
        "es": "{shown} mostrados de {total}",
        "fr": "{shown} affichés sur {total}",
        "it": "{shown} mostrati di {total}",
        "zh": "已显示 {shown} / 共 {total}",
    },
    "ui_last_update": {
        "en": "Last update",
        "fa": "آخرین به‌روزرسانی",
        "pt": "Última atualização",
        "uz": "So‘nggi yangilanish",
        "ru": "Последнее обновление",
        "id": "Pembaruan terakhir",
        "es": "Última actualización",
        "fr": "Dernière mise à jour",
        "it": "Ultimo aggiornamento",
        "zh": "最后更新",
    },
    "ui_component": {
        "en": "Component",
        "fa": "مولفه",
        "pt": "Componente",
        "uz": "Komponent",
        "ru": "Компонент",
        "id": "Komponen",
        "es": "Componente",
        "fr": "Composant",
        "it": "Componente",
        "zh": "组件",
    },
    "ui_log_severity_error": {
        "en": "errors",
        "fa": "خطاها",
        "pt": "erros",
        "uz": "xatolar",
        "ru": "ошибки",
        "id": "kesalahan",
        "es": "errores",
        "fr": "erreurs",
        "it": "errori",
        "zh": "错误",
    },
    "ui_log_severity_warn": {
        "en": "warnings",
        "fa": "هشدارها",
        "pt": "avisos",
        "uz": "ogohlantirishlar",
        "ru": "предупреждения",
        "id": "peringatan",
        "es": "advertencias",
        "fr": "avertissements",
        "it": "avvisi",
        "zh": "警告",
    },
    "ui_log_severity_info": {
        "en": "info",
        "fa": "اطلاعات",
        "pt": "informações",
        "uz": "ma'lumot",
        "ru": "информация",
        "id": "info",
        "es": "información",
        "fr": "informations",
        "it": "informazioni",
        "zh": "信息",
    },
    "ui_log_severity_debug": {
        "en": "debug",
        "fa": "دیباگ",
        "pt": "depuração",
        "uz": "debug",
        "ru": "отладка",
        "id": "debug",
        "es": "depuración",
        "fr": "débogage",
        "it": "debug",
        "zh": "调试",
    },
}

LOCALES = ["en", "fa", "pt", "uz", "ru", "id", "es", "fr", "it", "zh"]
BASE = "/root/AutoTelegramForward/web/frontend/src/locales"

for loc in LOCALES:
    path = os.path.join(BASE, f"{loc}.ts")
    with io.open(path, encoding="utf-8") as fh:
        src = fh.read()
    existing = {k: True for k in NEW if f'"{k}":' in src}
    missing = [k for k in NEW if k not in existing]
    if not missing:
        print(f"{loc}: up to date")
        continue
    # Append before the closing brace of the catalog object.
    body = "".join(f'  "{k}": "{NEW[k][loc]}",\n' for k in missing)
    idx = src.rstrip().rfind("}")
    assert idx > 0, f"no closing brace in {path}"
    # back off trailing newline before '}'
    while idx > 0 and src[idx - 1] in "\n":
        idx -= 1
    src = src[:idx] + body + "\n" + src[idx:]
    with io.open(path, "w", encoding="utf-8") as fh:
        fh.write(src)
    print(f"{loc}: added {len(missing)} keys")
