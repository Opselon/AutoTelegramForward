#!/usr/bin/env python3
"""Add the §6 task-monitor sort/filter keys to every locale (10-language parity)."""
import io
import os

NEW = {
    "ui_task_sort": {
        "en": "Sort by",
        "fa": "مرتب‌سازی بر اساس",
        "pt": "Ordenar por",
        "uz": "Saralash",
        "ru": "Сортировать по",
        "id": "Urutkan berdasarkan",
        "es": "Ordenar por",
        "fr": "Trier par",
        "it": "Ordina per",
        "zh": "排序方式",
    },
    "ui_task_sort_hint": {
        "en": "Which task answers the question you are asking: busiest (throughput), most failing, deepest backlog, slowest, or most delivered.",
        "fa": "کدام کار به سؤال شما پاسخ می‌دهد: پرترافیک‌ترین (توان عملی)، با بیشترین شکست، عمیق‌ترین انبار، کندترین یا با بیشترین تحویل.",
        "pt": "Qual tarefa responde à pergunta que você está fazendo: a mais movimentada (throughput), a que mais falha, a maior backlog, a mais lenta ou a com mais entregas.",
        "uz": "Qaysi vazifa sizning savolingizga javob beradi: eng band (o'tkazuv qobiliyati), eng ko'p xatoliklar, eng katta navbat, eng sekin yoki eng ko'p yetkazib berilgan.",
        "ru": "Какая задача отвечает на ваш вопрос: самая загруженная (пропускная способность), чаще всего падающая, с самым большим backlog, самая медленная или с большей частью доставок.",
        "id": "Tugas mana yang menjawab pertanyaan Anda: paling sibuk (throughput), paling banyak gagal, antrian terdalam, paling lambat, atau paling banyak terkirim.",
        "es": "Qué tarea responde a tu pregunta: la más activa (rendimiento), la que más falla, la de mayor backlog, la más lenta o la con más entregas.",
        "fr": "Quelle tâche répond à votre question : la plus chargée (débit), celle qui échoue le plus, au backlog le plus profond, la plus lente ou ayant le plus de livraisons.",
        "it": "Quale task risponde alla tua domanda: il più trafficato (throughput), quello che fallisce di più, con il backlog più profondo, il più lento o con più consegne.",
        "zh": "哪个任务回答了你的问题：最繁忙（吞吐量）、失败最多、积压最深、最慢，或投递最多。",
    },
    "ui_task_sort_throughput": {
        "en": "Throughput (busiest)",
        "fa": "توان عملی (پرترافیک‌ترین)",
        "pt": "Rendimento (mais movimentada)",
        "uz": "O'tkazuv qobiliyati (eng band)",
        "ru": "Пропускная способность (загруженность)",
        "id": "Throughput (paling sibuk)",
        "es": "Rendimiento (más activa)",
        "fr": "Débit (plus chargée)",
        "it": "Throughput (più trafficato)",
        "zh": "吞吐量（最繁忙）",
    },
    "ui_task_sort_failed": {
        "en": "Failures (most)",
        "fa": "شکست‌ها (بیشترین)",
        "pt": "Falhas (mais)",
        "uz": "Xatoliklar (eng ko'p)",
        "ru": "Ошибки (больше)",
        "id": "Kegagalan (terbanyak)",
        "es": "Fallos (más)",
        "fr": "Échecs (le plus)",
        "it": "Fallimenti (di più)",
        "zh": "失败（最多）",
    },
    "ui_task_sort_pending": {
        "en": "Backlog (deepest)",
        "fa": "انبار (عمیق‌ترین)",
        "pt": "Backlog (mais profundo)",
        "uz": "Navbat (eng katta)",
        "ru": "Backlog (глубже)",
        "id": "Antrian (terbanyak)",
        "es": "Backlog (más profundo)",
        "fr": "Backlog (le plus profond)",
        "it": "Backlog (più profondo)",
        "zh": "积压（最深）",
    },
    "ui_task_sort_latency": {
        "en": "Latency (slowest)",
        "fa": "تأخیر (کندترین)",
        "pt": "Latência (mais lenta)",
        "uz": "Kechikish (eng sekin)",
        "ru": "Задержка (медленнее)",
        "id": "Latensi (paling lambat)",
        "es": "Latencia (más lenta)",
        "fr": "Latence (la plus lente)",
        "it:": "Latenza (più lento)",
        "zh": "延迟（最慢）",
    },
    "ui_task_sort_delivered": {
        "en": "Delivered (most)",
        "fa": "تحویل‌داده‌شده (بیشترین)",
        "pt": "Entregues (mais)",
        "uz": "Yetkazib berilgan (eng ko'p)",
        "ru": "Доставлено (больше)",
        "id": "Terkirim (terbanyak)",
        "es": "Entregados (más)",
        "fr": "Livrés (le plus)",
        "it:": "Consegnati (di più)",
        "zh": "已投递（最多）",
    },
    "ui_task_attention": {
        "en": "Needs attention only",
        "fa": "فقط نیازمند توجه",
        "pt": "Apenas com problemas",
        "uz": "Faqat e'tibor talab qiladiganlar",
        "ru": "Только требующие внимания",
        "id": "Hanya yang butuh perhatian",
        "es": "Solo las que necesitan atención",
        "fr": "Uniquement celles nécessitant attention",
        "it:": "Solo quelli che necessitano attenzione",
        "zh": "仅显示需关注",
    },
    "ui_task_attention_hint": {
        "en": "Hide tasks with zero failures and zero pending jobs — the healthy ones are not the answer to 'what needs me now'.",
        "fa": "مخفی کردن کارهای بدون شکست و بدون کار معلق — کارهای سالم پاسخِ «الان چه چیزی نیازمنده» نیستند.",
        "pt": "Oculta tarefas sem falhas e sem jobs pendentes — as saudáveis não respondem a 'o que precisa de mim agora'.",
        "uz": "Xatoligi va kutilayotgan ishi bo'lmagan vazifalarni yashirish — sog'lomlar 'hozir nima kerak' savoliga javob emas.",
        "ru": "Скрыть задачи без ошибок и без ожидающих заданий — здоровые не отвечают на вопрос «что сейчас требует внимания».",
        "id": "Sembunyikan task tanpa kegagalan dan tanpa job tertunda — yang sehat tidak menjawab 'apa yang butuh saya sekarang'.",
        "es": "Oculta tareas sin fallos y sin jobs pendientes — las sanas no responden a 'qué me necesita ahora'.",
        "fr": "Masque les tâches sans échec et sans job en attente — les saines ne répondent pas à 'que dois-je traiter maintenant'.",
        "it:": "Nasconde task senza fallimenti e senza job in attesa — quelli sani non rispondono a 'cosa richiede la mia attenzione ora'.",
        "zh": "隐藏没有失败且没有待处理作业的任务——健康的任务不是“现在什么需要我”的答案。",
    },
}

LOCALES = ["en", "fa", "pt", "uz", "ru", "id", "es", "fr", "it", "zh"]
BASE = "/root/AutoTelegramForward/web/frontend/src/locales"

# fix the accidental "it:" keys before writing
for k in NEW:
    for loc in LOCALES:
        if "it:" in NEW[k] and loc == "it":
            NEW[k]["it"] = NEW[k].pop("it:")

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
