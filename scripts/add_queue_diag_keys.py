#!/usr/bin/env python3
"""Add the queue-diagnostics chip keys to every locale (10-language parity)."""
import io
import os

NEW = {
    "ui_queue_diagnostics_hint": {
        "en": "Diagnostics: stuck means a CLAIMED lease expired over {claimed}s ago, a PENDING row waited over {pending}s, a retry storm is retries landing within {storm}s.",
        "fa": "تشخیص: گیر کردن یعنی اجاره CLAIMED بیش از {claimed} ثانیه پیش منقضی شده، یک ردیف PENDING بیش از {pending} ثانیه منتظر مانده، طوفان تلاش مجدد = تلاش‌های مجدد در عرض {storm} ثانیه.",
        "pt": "Diagnóstico: \"preso\" significa que um lease CLAIMED expirou há mais de {claimed}s, uma linha PENDING esperou mais de {pending}s, uma tempestade de retentativas são retentativas chegando em {storm}s.",
        "uz": "Diagnostika: 'yopashib qolish' — CLAIMED lizenzasi {claimed}s dan oldin tugagan, PENDING qatori {pending}s dan ko'p kutgan, qayta urinish to'foni — {storm}s ichida keladigan qayta urinishlar.",
        "ru": "Диагностика: «застрял» — аренда CLAIMED истекла более {claimed}s назад, строка PENDING ждёт более {pending}s, шторм повторов — повторы в течение {storm}s.",
        "id": "Diagnostik: 'stuck' berarti lease CLAIMED kedaluwarsa lebih dari {claimed}s lalu, baris PENDING menunggu lebih dari {pending}s, badai retry adalah retry yang tiba dalam {storm}s.",
        "es": "Diagnóstico: 'atascado' significa que un lease CLAIMED expiró hace más de {claimed}s, una fila PENDING esperó más de {pending}s, una tormenta de reintentos son reintentos que llegan en {storm}s.",
        "fr": "Diagnostic : « bloqué » signifie qu'un bail CLAIMED a expiré depuis plus de {claimed}s, une ligne PENDING attend depuis plus de {pending}s, une tempête de relances est des relances arrivant en {storm}s.",
        "it": "Diagnostica: 'bloccato' significa che un lease CLAIMED è scaduto da più di {claimed}s, una riga PENDING aspetta da più di {pending}s, una tempesta di retry sono retry in arrivo entro {storm}s.",
        "zh": "诊断：「卡住」指 CLAIMED 租约已过期超过 {claimed}s，PENDING 行已等待超过 {pending}s，重试风暴指在 {storm}s 内到来的重试。",
    },
    "ui_diag_stuck_claimed": {
        "en": "Stuck claims",
        "fa": "ادعاهای گیر کرده",
        "pt": "Claims presos",
        "uz": "Yopashib qolgan claimlar",
        "ru": "Зависшие аренды",
        "id": "Claim terhenti",
        "es": "Claims atascados",
        "fr": "Bails bloqués",
        "it": "Claim bloccati",
        "zh": "卡住的领取",
    },
    "ui_diag_stuck_pending": {
        "en": "Starved pending",
        "fa": "در انتظار گرسنه",
        "pt": "Pendentes esfomeados",
        "uz": "Ochlangan kutilayotganlar",
        "ru": "Голодные ожидания",
        "id": "Pending kelaparan",
        "es": "Pendientes hambrientos",
        "fr": "En attente affamés",
        "it": "In attesa affamati",
        "zh": "饥饿的待处理",
    },
    "ui_diag_poison_jobs": {
        "en": "Poison jobs",
        "fa": "کارهای سمی",
        "pt": "Jobs venenosos",
        "uz": "Zaharli ishlar",
        "ru": "Ядовитые задания",
        "id": "Job racun",
        "es": "Jobs venenosos",
        "fr": "Jobs poison",
        "it": "Job velenosi",
        "zh": "毒消息作业",
    },
    "ui_diag_retry_storm": {
        "en": "Retry storm (next 60s)",
        "fa": "طوفان تلاش مجدد (۶۰ ثانیه آینده)",
        "pt": "Tempestade de retentativa (próx. 60s)",
        "uz": "Qayta urinish to'foni (keyingi 60s)",
        "ru": "Шторм повторов (ближ. 60с)",
        "id": "Badai retry (60s ke depan)",
        "es": "Tormenta de reintentos (próx. 60s)",
        "fr": "Tempête de relances (60s prochaines)",
        "it": "Tempesta di retry (prossimi 60s)",
        "zh": "重试风暴（未来 60 秒）",
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
