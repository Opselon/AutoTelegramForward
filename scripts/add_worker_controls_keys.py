#!/usr/bin/env python3
"""Add the §10 worker-controls keys to every locale (10-language parity)."""
import io
import os

NEW = {
    "ui_worker_controls": {
        "en": "Worker Controls",
        "fa": "کنترل‌های کارگر",
        "pt": "Controles do worker",
        "uz": "Worker boshqaruvlari",
        "ru": "Управление воркером",
        "id": "Kontrol worker",
        "es": "Controles del worker",
        "fr": "Contrôles du worker",
        "it": "Controlli del worker",
        "zh": "Worker 控制",
    },
    "ui_control_running": {
        "en": "RUNNING",
        "fa": "در حال اجرا",
        "pt": "EM EXECUÇÃO",
        "uz": "ISHLAMOQDA",
        "ru": "РАБОТАЕТ",
        "id": "BERJALAN",
        "es": "EN EJECUCIÓN",
        "fr": "EN COURS",
        "it": "IN ESECUZIONE",
        "zh": "运行中",
    },
    "ui_control_paused": {
        "en": "PAUSED",
        "fa": "متوقف موقت",
        "pt": "PAUSADO",
        "uz": "TO'XTATILGAN",
        "ru": "ПРИОСТАНОВЛЕН",
        "id": "DIJEDA",
        "es": "PAUSADO",
        "fr": "EN PAUSE",
        "it": "IN PAUSA",
        "zh": "已暂停",
    },
    "ui_control_pause": {
        "en": "Pause processing",
        "fa": "توقف موقت پردازش",
        "pt": "Pausar processamento",
        "uz": "Qayta ishlashni to'xtatish",
        "ru": "Приостановить обработку",
        "id": "Jeda pemrosesan",
        "es": "Pausar el procesamiento",
        "fr": "Mettre en pause le traitement",
        "it": "Metti in pausa l'elaborazione",
        "zh": "暂停处理",
    },
    "ui_control_resume": {
        "en": "Resume processing",
        "fa": "ازسرگیری پردازش",
        "pt": "Retomar processamento",
        "uz": "Qayta ishlashni davom ettirish",
        "ru": "Возобновить обработку",
        "id": "Lanjutkan pemrosesan",
        "es": "Reanudar el procesamiento",
        "fr": "Reprendre le traitement",
        "it:": "Riprendi l'elaborazione",
        "zh": "恢复处理",
    },
    "ui_control_pause_confirm": {
        "en": "Pause processing? The worker stays alive and healthy; the claim loop stops taking new work and in-flight deliveries complete (they are not cancelled).",
        "fa": "پردازش متوقف شود؟ کارگر زنده و سالم می‌ماند؛ حلقه ادعا کار جدید را متوقف می‌کند و تحویل‌های در حال انجام کامل می‌شوند (لغو نمی‌شوند).",
        "pt": "Pausar o processamento? O worker continua vivo e saudável; o loop de claim para de aceitar novos jobs e as entregas em andamento são concluídas (não canceladas).",
        "uz": "Qayta ishlash to'xtatilsinmi? Worker tirik va sog'lom qoladi; claim loop yangi ishlarni olishni to'xtatadi va jarayondagi yetkazib berishlar yakunlanadi (bekor qilinmaydi).",
        "ru": "Приостановить обработку? Воркер остаётся живым и здоровым; цикл клейма перестаёт брать новую работу, а доставки в полёте завершаются (не отменяются).",
        "id": "Jeda pemrosesan? Worker tetap hidup dan sehat; claim loop berhenti mengambil job baru dan pengiriman yang sedang berjalan diselesaikan (tidak dibatalkan).",
        "es": "¿Pausar el procesamiento? El worker sigue vivo y sano; el loop de claim deja de aceptar jobs nuevos y las entregas en curso se completan (no se cancelan).",
        "fr": "Mettre en pause le traitement ? Le worker reste vivant et sain ; la boucle de claim cesse de prendre du nouveau travail et les livraisons en cours se terminent (elles ne sont pas annulées).",
        "it:": "Mettere in pausa l'elaborazione? Il worker resta vivo e sano; il loop di claim smette di prendere nuovi job e le consegne in corso vengono completate (non annullate).",
        "zh": "暂停处理？worker 保持存活和健康；领取循环停止获取新作业，进行中的投递会完成（不会取消）。",
    },
    "ui_control_pause_hint": {
        "en": "Pause stops the claim loop from taking new work; the worker stays alive and healthy. In-flight deliveries complete and are never cancelled mid-job.",
        "fa": "توقف موقت حلقه ادعا را از گرفتن کار جدید متوقف می‌کند؛ کارگر زنده و سالم می‌ماند. تحویل‌های در حال انجام کامل می‌شوند و هرگز در وسط کار لغو نمی‌شوند.",
        "pt": "A pausa faz o loop de claim parar de aceitar novos jobs; o worker continua vivo e saudável. Entregas em andamento são concluídas e nunca canceladas no meio.",
        "uz": "To'xtatish claim loopga yangi ish olishni to'xtatadi; worker tirik va sog'lom qoladi. Jarayondagi yetkazib berishlar yakunlanadi va hech qachon yarim yo'lda bekor qilinmaydi.",
        "ru": "Пауза останавливает взятие новой работы циклом клейма; воркер остаётся живым и здоровым. Доставки в полёте завершаются и никогда не отменяются mid-job.",
        "id": "Jeda membuat claim loop berhenti mengambil job baru; worker tetap hidup dan sehat. Pengiriman yang berjalan diselesaikan dan tidak pernah dibatalkan di tengah.",
        "es": "La pausa evita que el loop de claim tome nuevos jobs; el worker sigue vivo y sano. Las entregas en curso se completan y nunca se cancelan a mitad de trabajo.",
        "fr": "La pause empêche la boucle de claim de prendre du nouveau travail ; le worker reste vivant et sain. Les livraisons en cours se terminent et ne sont jamais annulées mid-job.",
        "it:": "La pausa impedisce al loop di claim di prendere nuovi job; il worker resta vivo e sano. Le consegne in corso vengono completate e non vengono mai annullate a metà.",
        "zh": "暂停会停止领取循环获取新作业；worker 保持存活和健康。进行中的投递会完成，绝不在作业中途取消。",
    },
    "ui_control_paused_by": {
        "en": "Paused by {who} at {at}",
        "fa": "متوقف توسط {who} در {at}",
        "pt": "Pausado por {who} em {at}",
        "uz": "{who} tomonidan {at} da to'xtatilgan",
        "ru": "Приостановил {who} в {at}",
        "id": "Dijeda oleh {who} pada {at}",
        "es": "Pausado por {who} en {at}",
        "fr": "Mis en pause par {who} à {at}",
        "it:": "Messo in pausa da {who} alle {at}",
        "zh": "由 {who} 于 {at} 暂停",
    },
    "ui_control_audit": {
        "en": "Audit trail (newest first, bounded)",
        "fa": "رد حسابرسی (اول جدیدترین، محدود)",
        "pt": "Trilha de auditoria (mais recente primeiro, limitada)",
        "uz": "Audit izi (yangisi birinchi, cheklangan)",
        "ru": "Журнал аудита (сначала новые, ограничен)",
        "id": "Jejak audit (terbaru lebih dulu, dibatasi)",
        "es": "Pista de auditoría (más reciente primero, acotada)",
        "fr:": "Piste d'audit (plus récent d'abord, bornée)",
        "fr": "Piste d'audit (plus récent d'abord, bornée)",
        "it:": "Traccia di audit (prima i più recenti, limitata)",
        "zh": "审计记录（最新优先，有界）",
    },
    "ui_control_no_ops": {
        "en": "No control operations yet.",
        "fa": "هنوز هیچ عملیات کنترلی وجود ندارد.",
        "pt": "Nenhuma operação de controle ainda.",
        "uz:": "Hali boshqaruv operatsiyasi yo'q.",
        "uz": "Hali boshqaruv operatsiyasi yo'q.",
        "ru": "Операций управления ещё не было.",
        "id": "Belum ada operasi kontrol.",
        "es": "Aún no hay operaciones de control.",
        "fr": "Aucune opération de contrôle pour l'instant.",
        "it:": "Nessuna operazione di controllo finora.",
        "zh": "尚无控制操作。",
    },
    "ui_control_unavailable": {
        "en": "Worker control surface unavailable on this build — pause/resume is not available.",
        "fa": "سطح کنترل کارگر در این نسخه در دسترس نیست — توقف/ازسرگیری ممکن نیست.",
        "pt": "Surface de controle do worker indisponível nesta versão — pause/resume indisponível.",
        "uz": "Ushbu build-da worker boshqaruv yuzasi mavjud emas — to'xtatish/davom ettirish mumkin emas.",
        "ru": "Поверхность управления воркера недоступна в этой сборке — пауза/возобновление невозможны.",
        "id": "Surface kontrol worker tidak tersedia di build ini — jeda/lanjut tidak tersedia.",
        "es": "La superficie de control del worker no está disponible en este build — pausa/reanudación no disponibles.",
        "fr": "La surface de contrôle du worker est indisponible dans ce build — pause/reprise indisponibles.",
        "it:": "La superficie di controllo del worker non è disponibile in questa build — pausa/riprendi non disponibili.",
        "zh": "此构建中 worker 控制面不可用——无法暂停/恢复。",
    },
    "ui_control_worker_unreachable": {
        "en": "Worker unreachable — control state is unknown, not 'running'.",
        "fa": "کارگر در دسترس نیست — وضعیت کنترل ناشناخته است، نه «در حال اجرا».",
        "pt": "Worker inacessível — o estado de controle é desconhecido, não 'em execução'.",
        "uz": "Worker yetib bo'lmaydi — boshqaruv holati noma'lum, 'ishlamoqda' emas.",
        "ru": "Воркер недоступен — состояние управления неизвестно, а не «работает».",
        "id": "Worker tidak terjangkau — status kontrol tidak diketahui, bukan 'berjalan'.",
        "es": "Worker inalcanzable — el estado de control es desconocido, no 'en ejecución'.",
        "fr": "Worker injoignable — l'état de contrôle est inconnu, pas 'en cours'.",
        "it:": "Worker non raggiungibile — lo stato di controllo è sconosciuto, non 'in esecuzione'.",
        "zh": "Worker 不可达——控制状态未知，而非“运行中”。",
    },
}

LOCALES = ["en", "fa", "pt", "uz", "ru", "id", "es", "fr", "it", "zh"]
BASE = "/root/AutoTelegramForward/web/frontend/src/locales"

# fix accidental "key:" variants before writing
for k in list(NEW.keys()):
    for loc in list(NEW[k].keys()):
        if loc.endswith(":") and loc[:-1] in LOCALES:
            NEW[k][loc[:-1]] = NEW[k].pop(loc)

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
