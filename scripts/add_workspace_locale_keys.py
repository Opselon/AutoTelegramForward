#!/usr/bin/env python3
"""Insert the task-workspace and observability keys into the ten catalogs.

One-off (like the previous feature's locale commits): the generator that built
these files is not in the tree, so new keys are added in place, alphabetically,
with the same placeholder sets in every language. The catalog contract test
(tests/uiCatalog.test.mjs) then verifies parity from the files themselves.
"""
import json
import re
from pathlib import Path

LOCALES = Path(__file__).resolve().parent.parent / "web" / "frontend" / "src" / "locales"

KEYS = {
    "ui_clear_search": {
        "en": "Clear search",
        "fa": "پاک کردن جستجو",
        "pt": "Limpar pesquisa",
        "uz": "Qidiruvni tozalash",
        "ru": "Очистить поиск",
        "id": "Hapus pencarian",
        "es": "Borrar búsqueda",
        "fr": "Effacer la recherche",
        "it": "Cancella ricerca",
        "zh": "清除搜索",
    },
    "ui_correlation_id": {
        "en": "Correlation ID",
        "fa": "شناسه همبستگی",
        "pt": "ID de correlação",
        "uz": "Korrelyatsiya IDsi",
        "ru": "ID корреляции",
        "id": "ID korelasi",
        "es": "ID de correlación",
        "fr": "ID de corrélation",
        "it": "ID di correlazione",
        "zh": "关联 ID",
    },
    "ui_delivery_timeline": {
        "en": "Delivery timeline",
        "fa": "خط زمانی تحویل",
        "pt": "Cronologia de entrega",
        "uz": "Yetkazib berish xronologiyasi",
        "ru": "Хронология доставки",
        "id": "Linimasa pengiriman",
        "es": "Cronología de entrega",
        "fr": "Chronologie de livraison",
        "it": "Cronologia della consegna",
        "zh": "投递时间线",
    },
    "ui_diagnostic_events": {
        "en": "Diagnostic events",
        "fa": "رویدادهای عیب‌یابی",
        "pt": "Eventos de diagnóstico",
        "uz": "Diagnostika hodisalari",
        "ru": "Диагностические события",
        "id": "Peristiwa diagnostik",
        "es": "Eventos de diagnóstico",
        "fr": "Événements de diagnostic",
        "it": "Eventi diagnostici",
        "zh": "诊断事件",
    },
    "ui_filter_status": {
        "en": "Filter by status",
        "fa": "فیلتر بر اساس وضعیت",
        "pt": "Filtrar por estado",
        "uz": "Holat bo'yicha filtrlash",
        "ru": "Фильтр по статусу",
        "id": "Filter berdasarkan status",
        "es": "Filtrar por estado",
        "fr": "Filtrer par état",
        "it": "Filtra per stato",
        "zh": "按状态筛选",
    },
    "ui_logs_pipeline": {
        "en": "Logging pipeline",
        "fa": "خط لوله ثبت گزارش",
        "pt": "Pipeline de registos",
        "uz": "Jurnal liniyasi",
        "ru": "Конвейер журналирования",
        "id": "Pipeline pencatatan",
        "es": "Canal de registro",
        "fr": "Pipeline de journalisation",
        "it": "Pipeline di registrazione",
        "zh": "日志管道",
    },
    "ui_search_tasks": {
        "en": "Search tasks",
        "fa": "جستجوی وظایف",
        "pt": "Pesquisar tarefas",
        "uz": "Vazifalarni qidirish",
        "ru": "Поиск задач",
        "id": "Cari tugas",
        "es": "Buscar tareas",
        "fr": "Rechercher des tâches",
        "it": "Cerca attività",
        "zh": "搜索任务",
    },
    "ui_showing_tasks": {
        "en": "Showing {shown} of {total} tasks",
        "fa": "نمایش {shown} از {total} وظیفه",
        "pt": "A mostrar {shown} de {total} tarefas",
        "uz": "{total} vazifadan {shown} tasi ko'rsatilmoqda",
        "ru": "Показано {shown} из {total} задач",
        "id": "Menampilkan {shown} dari {total} tugas",
        "es": "Mostrando {shown} de {total} tareas",
        "fr": "{shown} tâches affichées sur {total}",
        "it": "Visualizzate {shown} di {total} attività",
        "zh": "显示 {total} 个任务中的 {shown} 个",
    },
}


def insert_key(text: str, key: str, value: str) -> str:
    """Insert `"key": "value",` in the alphabetically sorted position."""
    entry = f'  "{key}": {json.dumps(value, ensure_ascii=False)},\n'
    pattern = re.compile(r'(  "[a-z0-9_]+": )', re.M)
    matches = list(pattern.finditer(text))
    if any(m.group(1).strip().startswith(f'"{key}"') for m in matches):
        raise SystemExit(f"key already present: {key}")
    target = None
    for m in matches:
        existing = m.group(1).strip().strip('"').rstrip('":')
        if existing > key:
            target = m.start()
            break
    if target is None:
        raise SystemExit(f"no insertion point found for {key}")
    return text[:target] + entry + text[target:]


def main() -> None:
    for lang_file in sorted(LOCALES.glob("*.ts")):
        lang = lang_file.stem
        if lang not in ("en", "fa", "pt", "uz", "ru", "id", "es", "fr", "it", "zh"):
            continue
        text = lang_file.read_text(encoding="utf-8")
        for key in sorted(KEYS):
            text = insert_key(text, key, KEYS[key][lang])
        lang_file.write_text(text, encoding="utf-8", newline="\n")
        print(f"ok  {lang}: +{len(KEYS)} keys")


if __name__ == "__main__":
    main()
