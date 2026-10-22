#!/usr/bin/env python3
"""Insert the chat-browser keys into the ten dashboard catalogs, in key order.

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
    "ui_browse_chats": {
        "en": "Browse chats",
        "fa": "مرور گفتگوها",
        "pt": "Explorar chats",
        "uz": "Chatlarni ko'rish",
        "ru": "Обзор чатов",
        "id": "Jelajahi chat",
        "es": "Explorar chats",
        "fr": "Parcourir les chats",
        "it": "Sfoglia chat",
        "zh": "浏览会话",
    },
    "ui_chat_picker_title": {
        "en": "Pick chats",
        "fa": "انتخاب گفتگو",
        "pt": "Escolher chats",
        "uz": "Chatlarni tanlash",
        "ru": "Выбор чатов",
        "id": "Pilih chat",
        "es": "Elegir chats",
        "fr": "Choisir des chats",
        "it": "Scegli chat",
        "zh": "选择会话",
    },
    "ui_chat_picker_subtitle": {
        "en": "From the chats this session can see — {total} available.",
        "fa": "از گفتگوهایی که این نشست می‌بیند — {total} مورد موجود است.",
        "pt": "Dos chats que esta sessão pode ver — {total} disponíveis.",
        "uz": "Bu seans ko'radigan chatlardan — {total} ta mavjud.",
        "ru": "Из чатов, доступных этой сессии — всего {total}.",
        "id": "Dari chat yang terlihat oleh sesi ini — {total} tersedia.",
        "es": "De los chats que ve esta sesión — {total} disponibles.",
        "fr": "Parmi les chats visibles par cette session — {total} disponibles.",
        "it": "Dalle chat visibili a questa sessione — {total} disponibili.",
        "zh": "来自此会话可见的聊天 — 共 {total} 个。",
    },
    "ui_chat_picker_search_hint": {
        "en": "Search by name, @username or id",
        "fa": "جستجو با نام، @نام‌کاربری یا شناسه",
        "pt": "Pesquisar por nome, @usuário ou id",
        "uz": "Nom, @foydalanuvchi yoki id bo'yicha qidirish",
        "ru": "Поиск по названию, @имени или id",
        "id": "Cari berdasarkan nama, @pengguna, atau id",
        "es": "Buscar por nombre, @usuario o id",
        "fr": "Rechercher par nom, @utilisateur ou id",
        "it": "Cerca per nome, @utente o id",
        "zh": "按名称、@用户名 或 id 搜索",
    },
    "ui_chat_picker_no_matches": {
        "en": "No chats match this search.",
        "fa": "هیچ گفتگویی با این جستجو مطابقت ندارد.",
        "pt": "Nenhum chat corresponde a esta pesquisa.",
        "uz": "Bu qidiruvga mos chat topilmadi.",
        "ru": "По этому запросу чатов нет.",
        "id": "Tidak ada chat yang cocok dengan pencarian ini.",
        "es": "Ningún chat coincide con esta búsqueda.",
        "fr": "Aucun chat ne correspond à cette recherche.",
        "it": "Nessuna chat corrisponde a questa ricerca.",
        "zh": "没有匹配的会话。",
    },
    "ui_chat_picker_unavailable": {
        "en": "This session's chat list is unavailable right now — type the id or @username instead.",
        "fa": "فهرست گفتگوهای این نشست در حال حاضر در دسترس نیست — شناسه یا @نام‌کاربری را دستی وارد کنید.",
        "pt": "A lista de chats desta sessão está indisponível agora — digite o id ou @usuário.",
        "uz": "Bu seansning chat ro'yxati hozir mavjud emas — id yoki @foydalanuvchini kiriting.",
        "ru": "Список чатов этой сессии сейчас недоступен — введите id или @имя вручную.",
        "id": "Daftar chat sesi ini sedang tidak tersedia — ketik id atau @pengguna.",
        "es": "La lista de chats de esta sesión no está disponible ahora — escribe el id o @usuario.",
        "fr": "La liste des chats de cette session est indisponible — saisissez l'id ou le @nom.",
        "it": "L'elenco chat di questa sessione non è disponibile — digita l'id o l'@utente.",
        "zh": "此会话的聊天列表当前不可用 — 请手动输入 id 或 @用户名。",
    },
    "ui_chat_picker_load_failed": {
        "en": "Could not load the chat list.",
        "fa": "بارگذاری فهرست گفتگوها ممکن نشد.",
        "pt": "Não foi possível carregar a lista de chats.",
        "uz": "Chat ro'yxatini yuklab bo'lmadi.",
        "ru": "Не удалось загрузить список чатов.",
        "id": "Tidak dapat memuat daftar chat.",
        "es": "No se pudo cargar la lista de chats.",
        "fr": "Impossible de charger la liste des chats.",
        "it": "Impossibile caricare l'elenco chat.",
        "zh": "无法加载聊天列表。",
    },
    "ui_chat_picker_load_more": {
        "en": "Load more ({shown} of {total})",
        "fa": "بیشتر ({shown} از {total})",
        "pt": "Carregar mais ({shown} de {total})",
        "uz": "Ko'proq yuklash ({shown} / {total})",
        "ru": "Показать ещё ({shown} из {total})",
        "id": "Muat lagi ({shown} dari {total})",
        "es": "Cargar más ({shown} de {total})",
        "fr": "Charger plus ({shown} sur {total})",
        "it": "Carica altre ({shown} di {total})",
        "zh": "加载更多（{shown}/{total}）",
    },
    "ui_chat_picker_selected_count": {
        "en": "{n} selected",
        "fa": "{n} انتخاب‌شده",
        "pt": "{n} selecionados",
        "uz": "{n} ta tanlandi",
        "ru": "Выбрано: {n}",
        "id": "{n} dipilih",
        "es": "{n} seleccionados",
        "fr": "{n} sélectionnés",
        "it": "{n} selezionate",
        "zh": "已选择 {n} 个",
    },
    "ui_chat_picker_none_selected": {
        "en": "Select one or more chats",
        "fa": "یک یا چند گفتگو انتخاب کنید",
        "pt": "Selecione um ou mais chats",
        "uz": "Bir yoki bir nechta chat tanlang",
        "ru": "Выберите один или несколько чатов",
        "id": "Pilih satu atau beberapa chat",
        "es": "Selecciona uno o más chats",
        "fr": "Sélectionnez un ou plusieurs chats",
        "it": "Seleziona una o più chat",
        "zh": "请选择一个或多个会话",
    },
    "ui_chat_picker_add_selected": {
        "en": "Add {n}",
        "fa": "افزودن {n}",
        "pt": "Adicionar {n}",
        "uz": "{n} ta qo'shish",
        "ru": "Добавить {n}",
        "id": "Tambah {n}",
        "es": "Añadir {n}",
        "fr": "Ajouter {n}",
        "it": "Aggiungi {n}",
        "zh": "添加 {n} 个",
    },
    "ui_chat_picker_already_added": {
        "en": "already added",
        "fa": "قبلاً افزوده شده",
        "pt": "já adicionado",
        "uz": "allaqachon qo'shilgan",
        "ru": "уже добавлен",
        "id": "sudah ditambahkan",
        "es": "ya añadido",
        "fr": "déjà ajouté",
        "it": "già aggiunta",
        "zh": "已添加",
    },
    "ui_close": {
        "en": "Close",
        "fa": "بستن",
        "pt": "Fechar",
        "uz": "Yopish",
        "ru": "Закрыть",
        "id": "Tutup",
        "es": "Cerrar",
        "fr": "Fermer",
        "it": "Chiudi",
        "zh": "关闭",
    },
}

KEY_LINE = re.compile(r'^  "((?:[^"\\]|\\.)+)":')


def insert(path: Path, key: str, value: str) -> bool:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    out = []
    inserted = False
    for line in lines:
        match = KEY_LINE.match(line)
        if not inserted and match and match.group(1) == key:
            return False  # already present
        if not inserted and match and match.group(1) > key:
            out.append(f'  "{key}": {json.dumps(value, ensure_ascii=False)},\n')
            inserted = True
        out.append(line)
    if not inserted:
        # Nothing sorts after it: put it last, before the closing brace.
        for index in range(len(out) - 1, -1, -1):
            if out[index].strip() == "}":
                out.insert(index, f'  "{key}": {json.dumps(value, ensure_ascii=False)},\n')
                inserted = True
                break
    if not inserted:
        raise SystemExit(f"{path}: could not find an insertion point for {key}")
    path.write_text("".join(out), encoding="utf-8")
    return True


def main() -> None:
    langs = sorted(KEYS["ui_browse_chats"].keys())
    for lang in langs:
        path = LOCALES / f"{lang}.ts"
        added = 0
        for key in sorted(KEYS):
            if insert(path, key, KEYS[key][lang]):
                added += 1
        print(f"{lang}: +{added} keys")


if __name__ == "__main__":
    main()
