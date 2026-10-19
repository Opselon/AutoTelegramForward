"""Add the three task-status strings (STEER 12) to the catalog, 1:1 across langs.

Anchored insertion: each new key goes immediately after `ui_rule_status_paused`
in the *language dict it belongs to*, so no other language can be touched and the
file cannot be rewritten wholesale (which is how the catalog was once corrupted).
Refuses to write unless every language gains exactly the same three keys.
"""

from __future__ import annotations

import collections
import json
import sys

PATH = "core/infrastructure/telegram/locales/translations.json"

ADDITIONS = {
    "en": {
        "ui_rule_status_paused_manual": "PAUSED ⏸ (paused)",
        "ui_rule_status_paused_until": "PAUSED ⏸ until {until}",
        "ui_rule_status_stale_alert": "This task changed elsewhere — its status is now: {status}",
    },
    "es": {
        "ui_rule_status_paused_manual": "EN PAUSA ⏸ (pausada)",
        "ui_rule_status_paused_until": "EN PAUSA ⏸ hasta {until}",
        "ui_rule_status_stale_alert": "Esta tarea cambió en otro lugar — su estado ahora es: {status}",
    },
    "fa": {
        "ui_rule_status_paused_manual": "متوقف ⏸ (متوقف‌شده)",
        "ui_rule_status_paused_until": "متوقف ⏸ تا {until}",
        "ui_rule_status_stale_alert": "این وظیفه در جای دیگری تغییر کرده است — وضعیت کنونی: {status}",
    },
    "fr": {
        "ui_rule_status_paused_manual": "EN PAUSE ⏸ (en pause)",
        "ui_rule_status_paused_until": "EN PAUSE ⏸ jusqu'à {until}",
        "ui_rule_status_stale_alert": "Cette tâche a changé ailleurs — statut actuel : {status}",
    },
    "id": {
        "ui_rule_status_paused_manual": "DIJEDA ⏸ (dijeda)",
        "ui_rule_status_paused_until": "DIJEDA ⏸ sampai {until}",
        "ui_rule_status_stale_alert": "Tugas ini diubah di tempat lain — status saat ini: {status}",
    },
    "it": {
        "ui_rule_status_paused_manual": "IN PAUSA ⏸ (in pausa)",
        "ui_rule_status_paused_until": "IN PAUSA ⏸ fino a {until}",
        "ui_rule_status_stale_alert": "Questa attività è cambiata altrove — stato attuale: {status}",
    },
    "pt": {
        "ui_rule_status_paused_manual": "PAUSADO ⏸ (pausado)",
        "ui_rule_status_paused_until": "PAUSADO ⏸ até {until}",
        "ui_rule_status_stale_alert": "Esta tarefa foi alterada noutro local — estado atual: {status}",
    },
    "ru": {
        "ui_rule_status_paused_manual": "ПАУЗА ⏸ (на паузе)",
        "ui_rule_status_paused_until": "ПАУЗА ⏸ до {until}",
        "ui_rule_status_stale_alert": "Задача изменена в другом месте — текущий статус: {status}",
    },
    "uz": {
        "ui_rule_status_paused_manual": "TO'XTATILGAN ⏸ (pauzada)",
        "ui_rule_status_paused_until": "TO'XTATILGAN ⏸ {until} gacha",
        "ui_rule_status_stale_alert": "Bu vazifa boshqa joyda o'zgartirildi — joriy holat: {status}",
    },
    "zh": {
        "ui_rule_status_paused_manual": "已暂停 ⏸（手动暂停）",
        "ui_rule_status_paused_until": "已暂停 ⏸ 至 {until}",
        "ui_rule_status_stale_alert": "此任务已在别处更改 — 当前状态：{status}",
    },
}
ANCHOR = "ui_rule_status_paused"


def main() -> int:
    with open(PATH, encoding="utf-8") as handle:
        catalog = json.load(handle, object_pairs_hook=collections.OrderedDict)

    before = {lang: len(strings) for lang, strings in catalog.items()}
    for lang, additions in ADDITIONS.items():
        strings = catalog.get(lang)
        if strings is None:
            print(f"FAIL: language {lang!r} is not in the catalog", file=sys.stderr)
            return 1
        if ANCHOR not in strings:
            print(f"FAIL: {lang!r} has no {ANCHOR!r} to anchor to", file=sys.stderr)
            return 1
        fresh: collections.OrderedDict = collections.OrderedDict()
        for key, value in strings.items():
            fresh[key] = value
            if key == ANCHOR:
                for new_key, new_value in additions.items():
                    fresh[new_key] = new_value
        catalog[lang] = fresh

    after = {lang: len(strings) for lang, strings in catalog.items()}
    for lang in catalog:
        gained = after[lang] - before[lang]
        if gained != len(ADDITIONS[lang]):
            print(f"FAIL: {lang} gained {gained} keys, expected {len(ADDITIONS[lang])}",
                  file=sys.stderr)
            return 1
        if after[lang] != after["en"]:
            print(f"FAIL: parity broken — {lang} has {after[lang]}, en has {after['en']}",
                  file=sys.stderr)
            return 1

    with open(PATH, "w", encoding="utf-8") as handle:
        json.dump(catalog, handle, ensure_ascii=False, indent=4)
        handle.write("\n")

    print(f"ok: {len(catalog)} languages, {after['en']} keys each "
          f"(was {before['en']}); added {sorted(ADDITIONS['en'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
