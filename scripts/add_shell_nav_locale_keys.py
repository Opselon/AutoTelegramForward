#!/usr/bin/env python3
"""Insert the shell/navigation keys into the ten dashboard catalogs, in key order.

One-off (like the previous feature's locale commits): the generator that built
these files is not in the tree, so new keys are added in place, alphabetically,
with the same placeholder sets in every language. The catalog contract test
(tests/uiCatalog.test.mjs) then verifies parity from the files themselves.
"""
import re
from pathlib import Path

LOCALES = Path(__file__).resolve().parent.parent / "web" / "frontend" / "src" / "locales"

KEYS = {
    "ui_collapse_sidebar": {
        "en": "Collapse sidebar",
        "fa": "جمع کردن نوار کناری",
        "pt": "Recolher barra lateral",
        "uz": "Yon panelni yig'ish",
        "ru": "Свернуть боковую панель",
        "id": "Ciutkan bilah samping",
        "es": "Contraer barra lateral",
        "fr": "Réduire la barre latérale",
        "it": "Comprimi barra laterale",
        "zh": "收起侧边栏",
    },
    "ui_expand_sidebar": {
        "en": "Expand sidebar",
        "fa": "باز کردن نوار کناری",
        "pt": "Expandir barra lateral",
        "uz": "Yon panelni yoyish",
        "ru": "Развернуть боковую панель",
        "id": "Bentangkan bilah samping",
        "es": "Expandir barra lateral",
        "fr": "Développer la barre latérale",
        "it": "Espandi barra laterale",
        "zh": "展开侧边栏",
    },
    "ui_more_navigation": {
        "en": "More",
        "fa": "بیشتر",
        "pt": "Mais",
        "uz": "Yana",
        "ru": "Ещё",
        "id": "Lainnya",
        "es": "Más",
        "fr": "Plus",
        "it": "Altro",
        "zh": "更多",
    },
    "ui_nav_group_administration": {
        "en": "Administration",
        "fa": "مدیریت",
        "pt": "Administração",
        "uz": "Administratsiya",
        "ru": "Администрирование",
        "id": "Administrasi",
        "es": "Administración",
        "fr": "Administration",
        "it": "Amministrazione",
        "zh": "管理",
    },
    "ui_nav_group_automation": {
        "en": "Automation",
        "fa": "اتوماسیون",
        "pt": "Automação",
        "uz": "Avtomatlashtirish",
        "ru": "Автоматизация",
        "id": "Otomatisasi",
        "es": "Automatización",
        "fr": "Automatisation",
        "it": "Automazione",
        "zh": "自动化",
    },
    "ui_nav_group_operations": {
        "en": "Operations",
        "fa": "عملیات",
        "pt": "Operações",
        "uz": "Operatsiyalar",
        "ru": "Операции",
        "id": "Operasi",
        "es": "Operaciones",
        "fr": "Opérations",
        "it": "Operazioni",
        "zh": "运维",
    },
    "ui_nav_group_workspace": {
        "en": "Workspace",
        "fa": "فضای کاری",
        "pt": "Espaço de trabalho",
        "uz": "Ish maydoni",
        "ru": "Рабочая область",
        "id": "Ruang kerja",
        "es": "Espacio de trabajo",
        "fr": "Espace de travail",
        "it": "Area di lavoro",
        "zh": "工作区",
    },
    "ui_nav_secondary": {
        "en": "Secondary navigation",
        "fa": "ناوبری ثانویه",
        "pt": "Navegação secundária",
        "uz": "Ikkilamchi navigatsiya",
        "ru": "Дополнительная навигация",
        "id": "Navigasi sekunder",
        "es": "Navegación secundaria",
        "fr": "Navigation secondaire",
        "it": "Navigazione secondaria",
        "zh": "辅助导航",
    },
}


def insert_key(text: str, key: str, value: str) -> str:
    """Insert `"key": "value",` in the alphabetically sorted position."""
    entry = f'  "{key}": {json_value(value)},\n'
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


def json_value(value: str) -> str:
    import json
    return json.dumps(value, ensure_ascii=False)


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
