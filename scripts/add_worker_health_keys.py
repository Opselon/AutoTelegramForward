#!/usr/bin/env python3
"""Add the worker health-state chip keys to every locale (10-language parity)."""
import io
import os

NEW = {
    "ui_worker_health_hint": {
        "en": "Derived from heartbeat freshness and execution state — not a stored record.",
        "fa": "بر اساس تازگی ضربان قلب و وضعیت اجرا — نه یک رکورد ذخیره‌شده.",
        "pt": "Derivado da frescura do heartbeat e do estado de execução — não de um registro armazenado.",
        "uz": "Heartbeat yangiligi va ijro holatidan olingan — saqlangan yozuvdan emas.",
        "ru": "Выводится из свежести пульса и состояния выполнения, а не из сохранённой записи.",
        "id": "Diturunkan dari kesegaran heartbeat dan status eksekusi — bu catatan tersimpan.",
        "es": "Derivado de la frescura del pulso y del estado de ejecución, no de un registro almacenado.",
        "fr": "Dérivé de la fraîcheur du pouls et de l'état d'exécution — pas d'un enregistrement stocké.",
        "it": "Derivato dalla freschezza del battito e dallo stato di esecuzione, non da un record memorizzato.",
        "zh": "根据心跳新鲜度和执行状态推导，而非存储的记录。",
    },
    "ui_worker_execution_state_hint": {
        "en": "Execution state: what the worker is doing (starting/ready/degraded/stopping).",
        "fa": "وضعیت اجرا: کارگر در حال انجام چه کاری است.",
        "pt": "Estado de execução: o que o worker está fazendo.",
        "uz": "Ijro holati: worker nimа qilmoqda.",
        "ru": "Состояние выполнения: чем воркер занимается сейчас.",
        "id": "Status eksekusi: apa yang sedang dikerjakan worker.",
        "es": "Estado de ejecución: qué está haciendo el worker.",
        "fr": "État d'exécution : ce que fait le worker.",
        "it": "Stato di esecuzione: cosa sta facendo il worker.",
        "zh": "执行状态：worker 正在做什么。",
    },
    "ui_worker_heartbeat": {
        "en": "Heartbeat age",
        "fa": "سن ضربان قلب",
        "pt": "Idade do pulso",
        "uz": "Heartbeat yoshi",
        "ru": "Возраст пульса",
        "id": "Usia heartbeat",
        "es": "Antigüedad del pulso",
        "fr": "Ancienneté du pouls",
        "it": "Età del battito",
        "zh": "心跳间隔",
    },
    "ui_worker_heartbeat_hint": {
        "en": "Seconds since the last heartbeat (worker self-tick or /ops poll).",
        "fa": "ثانیه‌ها از آخرین ضربان قلب (تیک خودکار کارگر یا نظرسنجی /ops).",
        "pt": "Segundos desde o último pulso (auto-tick do worker ou pesquisa /ops).",
        "uz": "So'nggi heartbeaddan buyon soniyalar (worker o'z tiki yoki /ops so'rovi).",
        "ru": "Секунд с последнего пульса (собственный тик воркера или опрос /ops).",
        "id": "Detik sejak heartbeat terakhir (tick worker atau polling /ops).",
        "es": "Segundos desde el último pulso (auto-tick del worker o polling /ops).",
        "fr": "Secondes depuis le dernier pouls (tick propre au worker ou sondage /ops).",
        "it": "Secondi dall'ultimo battito (tick del worker o polling /ops).",
        "zh": "距离上次心跳的秒数（worker 自检或 /ops 轮询）。",
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
