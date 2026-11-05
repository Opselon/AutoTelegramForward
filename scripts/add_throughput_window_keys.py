#!/usr/bin/env python3
"""Add the §4 throughput-window keys to every locale (10-language parity)."""
import io
import os

NEW = {
    "ui_throughput_windows": {
        "en": "Throughput windows",
        "fa": "پنجره‌های توان عملی",
        "pt": "Janelas de rendimento",
        "uz": "O'tkazuv oynalari",
        "ru": "Окна пропускной способности",
        "id": "Jendela throughput",
        "es": "Ventanas de rendimiento",
        "fr": "Fenêtres de débit",
        "it": "Finestre di throughput",
        "zh": "吞吐量窗口",
    },
    "ui_throughput_windows_hint": {
        "en": "Delivered messages in each aggregation window, from the worker's bounded minute-bucket ring.",
        "fa": "پیام‌های تحویل‌داده‌شده در هر پنجره تجمیع، از حلقه دقیقه‌ای محدود کارگر.",
        "pt": "Mensagens entregues em cada janela de agregação, do anel de buckets por minuto limitado do worker.",
        "uz": "Har bir agregatsiya oynasida yetkazib berilgan xabarlar, workerning cheklangan daqiqali halqasidan.",
        "ru": "Доставленные сообщения в каждом окне агрегации, из ограниченного кольца минутных корзин воркера.",
        "id": "Pesan terkirim di setiap jendela agregasi, dari ring bucket menit yang dibatasi milik worker.",
        "es": "Mensajes entregados en cada ventana de agregación, del anel de buckets por minutos acotado del worker.",
        "fr": "Messages livrés dans chaque fenêtre d'agrégation, issu de l'anneau borné de seaux par minute du worker.",
        "it": "Messaggi consegnati in ogni finestra di aggregazione, dall'anello limitato di bucket al minuto del worker.",
        "zh": "每个聚合窗口内已投递的消息，来自 worker 的有界分钟桶环形结构。",
    },
    "ui_insufficient_history": {
        "en": "not enough history",
        "fa": "تاریخچه کافی نیست",
        "pt": "histórico insuficiente",
        "uz": "tarix yetarli emas",
        "ru": "недостаточно истории",
        "id": "riwayat tidak cukup",
        "es": "historial insuficiente",
        "fr": "historique insuffisant",
        "it": "cronologia insufficiente",
        "zh": "历史数据不足",
    },
    "ui_throughput_history_partial": {
        "en": "Only {mins} min of history so far; longer windows stay 'insufficient' until an hour accumulates.",
        "fa": "تاکنون تنها {mins} دقیقه تاریخچه وجود دارد؛ پنجره‌های طولانی‌تر تا تجمیع یک ساعت به‌صورة «ناکافی» باقی می‌مانند.",
        "pt": "Apenas {mins} min de histórico até agora; janelas mais longas permanecem 'insuficientes' até acumular uma hora.",
        "uz": "Hozircha faqat {mins} daqiqalik tarix bor; uzunroq oynalar bir soat to'planguncha 'yetarli emas' bo'lib qoladi.",
        "ru": "Пока только {mins} мин истории; более длинные окна остаются «недостаточными», пока не накопится час.",
        "id": "Baru {mins} menit riwayat; jendela yang lebih lama tetap 'tidak cukup' hingga satu jam terkumpul.",
        "es": "Solo {mins} min de historial hasta ahora; las ventanas más largas permanecen 'insuficientes' hasta acumular una hora.",
        "fr": "Seulement {mins} min d'historique pour l'instant ; les fenêtres plus longues restent 'insuffisantes' jusqu'à ce qu'une heure s'accumule.",
        "it": "Solo {mins} min di cronologia finora; le finestre più lunghe rimangono 'insufficienti' finché non si accumula un'ora.",
        "zh": "目前仅有 {mins} 分钟历史；长窗口需积累一小时后才有数据。",
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
