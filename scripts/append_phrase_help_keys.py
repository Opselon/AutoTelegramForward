#!/usr/bin/env python3
"""Append the long-phrase whitelist help keys to translations.json.

One-shot script: adds the four `ui_words_*` / `cmd_words_*` keys that teach a
user how a multi-word phrase matches, in all ten locales, through the repo's
own batch writer so the file's 4-space indent and review surface survive.
"""
import importlib.util
import json
import pathlib

WRITER = pathlib.Path("scripts/append_catalog.py")
spec = importlib.util.spec_from_file_location("append_catalog_under_test", WRITER)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

PHRASE_EXAMPLE = "Whale Update on a VIP call"

HELP_TITLE = {
    "en": "📖 **How keywords work**\n\n• One entry matches as a whole — `{phrase}` matches that full phrase, `5x` matches `5x`.\n• Matching ignores letter case, extra spaces, new lines between the phrase's words and Persian/Arabic letter variants.\n• Add several entries at once with a comma or one per line.\n• Whitelist: forward only matching messages. Blacklist: never forward matching messages (blacklist wins).\n• Mode: `any` = one entry is enough · `all` = every entry must appear · `phrase` = the words must appear in that order.",
    "fa": "📖 **کلمات چگونه کار می‌کنند**\n\n• هر عبارت به‌صورت یک کلید کامل تطبیق داده می‌شود — `{phrase}` همان عبارة کامل را می‌گیرد و `5x` خودِ `5x` را.\n• تطبیق به بزرگی/کوچکی حروف، فاصله‌های اضافی، خط جدید میان کلمات عبارت و گونه‌های حروف فارسی/عربی حساس نیست.\n• چند کلید را با کاما یا هر کدام در یک خط جدا اضافه کنید.\n• لیست سفید: تنها پیام‌های منطبق فرستاده می‌شوند. لیست سیاه: پیام‌های منطبق هرگز فرستاده نمی‌شوند (لیست سیاه مقدم است).\n• حالت: `any` = یک کلید کافی است · `all` = همهٔ کلیدها باید باشند · `phrase` = کلمات باید به همین ترتیب بیایند.",
    "es": "📖 **Cómo funcionan las palabras clave**\n\n• Cada entrada coincide como un todo — `{phrase}` coincide con esa frase completa y `5x` con `5x`.\n• La coincidencia ignora mayúsculas, espacios repetidos, saltos de línea entre las palabras de la frase y variantes de letras persas/árabes.\n• Añade varias entradas a la vez con una coma o una por línea.\n• Lista blanca: solo se reenvían los mensajes coincidentes. Lista negra: nunca se reenvían los coincidentes (gana la lista negra).\n• Modo: `any` = basta una entrada · `all` = deben aparecer todas · `phrase` = las palabras deben aparecer en ese orden.",
    "fr": "📖 **Comment fonctionnent les mots-clés**\n\n• Chaque entrée correspond dans son ensemble — `{phrase}` correspond à cette phrase complète, `5x` à `5x`.\n• La correspondance ignore la casse, les espaces répétés, les sauts de ligne entre les mots de la phrase et les variantes de lettres persanes/arabes.\n• Ajoutez plusieurs entrées à la fois avec une virgule ou une par ligne.\n• Liste blanche : seuls les messages correspondants sont transférés. Liste noire : les messages correspondants ne le sont jamais (la liste noire l'emporte).\n• Mode : `any` = une entrée suffit · `all` = toutes doivent apparaître · `phrase` = les mots doivent apparaître dans cet ordre.",
    "id": "📖 **Cara kerja kata kunci**\n\n• Setiap entri cocok sebagai satu kesatuan — `{phrase}` cocok dengan frasa utuh itu, `5x` cocok dengan `5x`.\n• Pencocokan mengabaikan besar-kecil huruf, spasi berlebih, baris baru di antara kata dalam frasa, dan varian huruf Persia/Arab.\n• Tambahkan beberapa entri sekaligus dengan koma atau satu per baris.\n• Daftar putih: hanya pesan yang cocok yang diteruskan. Daftar hitam: pesan yang cocok tidak pernah diteruskan (daftar hitam menang).\n• Mode: `any` = satu entri cukup · `all` = semua entri harus ada · `phrase` = kata-kata harus muncul dalam urutan itu.",
    "it": "📖 **Come funzionano le parole chiave**\n\n• Ogni voce corrisponde nel suo insieme — `{phrase}` corrisponde a quella frase completa, `5x` a `5x`.\n• La corrispondenza ignora maiuscole, spazi ripetuti, a capo tra le parole della frase e varianti di lettere persiane/arabe.\n• Aggiungi più voci insieme con una virgola o una per riga.\n• Lista bianca: si inoltrano solo i messaggi corrispondenti. Lista nera: i messaggi corrispondenti non si inoltrano mai (vince la lista nera).\n• Modalità: `any` = basta una voce · `all` = devono esserci tutte · `phrase` = le parole devono comparire in quell'ordine.",
    "pt": "📖 **Como as palavras-chave funcionam**\n\n• Cada entrada corresponde como um todo — `{phrase}` corresponde a essa frase completa, `5x` corresponde a `5x`.\n• A correspondência ignora maiúsculas, espaços repetidos, quebras de linha entre as palavras da frase e variantes de letras persas/árabes.\n• Adicione várias entradas de uma vez com vírgula ou uma por linha.\n• Lista branca: só mensagens correspondentes são encaminhadas. Lista negra: mensagens correspondentes nunca são encaminhadas (a lista negra vence).\n• Modo: `any` = uma entrada basta · `all` = todas devem aparecer · `phrase` = as palavras devem aparecer nessa ordem.",
    "ru": "📖 **Как работают ключевые слова**\n\n• Каждая запись сопоставляется целиком — `{phrase}` совпадает с этой полной фразой, `5x` — с `5x`.\n• Сопоставление не учитывает регистр, лишние пробелы, переводы строк между словами фразы и варианты персидских/арабских букв.\n• Добавляйте несколько записей сразу через запятую или по одной в строке.\n• Белый список: пересылаются только совпавшие сообщения. Чёрный список: совпавшие никогда не пересылаются (чёрный список важнее).\n• Режим: `any` = достаточно одной записи · `all` = должны быть все · `phrase` = слова должны идти в этом порядке.",
    "uz": "📖 **Kalit so'zlar qanday ishlaydi**\n\n• Har bir yozuv bir butun sifatida solishtiriladi — `{phrase}` shu to'liq iboraga mos keladi, `5x` esa `5x` ga.\n• Solishtirish harf katta-kichikligi, ortiqcha bo'shliqlar, ibora so'zlari orasidagi yangi qatorlar va fors/arab harf variantlarini hisobga olmaydi.\n• Bir nechta yozuvni vergul bilan yoki har birini alohida qatorda qo'shing.\n• Oq ro'yxat: faqat mos kelgan xabarlar yuboriladi. Qora ro'yxat: mos kelganlar hech qachon yuborilmaydi (qora ro'yxat ustun).\n• Rejim: `any` = bitta yozuv yetarli · `all` = hammasi bo'lishi kerak · `phrase` = so'zlar shu tartibda kelishi kerak.",
    "zh": "📖 **关键词如何工作**\n\n• 每个条目作为一个整体匹配 —— `{phrase}` 匹配该完整短语，`5x` 匹配 `5x`。\n• 匹配忽略大小写、多余空格、短语单词之间的换行以及波斯/阿拉伯字母变体。\n• 用逗号或每行一个来一次添加多个条目。\n• 白名单：只转发匹配的消息。黑名单：匹配的消息永不转发（黑名单优先）。\n• 模式：`any` = 一个条目即可 · `all` = 每个条目都必须出现 · `phrase` = 单词必须按该顺序出现。",
}
HELP_TITLE = {k: v.format(phrase=PHRASE_EXAMPLE) for k, v in HELP_TITLE.items()}

PHRASE_EXAMPLE_TXT = {
    "en": f"Example: `5x, 10x` or `{PHRASE_EXAMPLE}`",
    "fa": f"مثال: `5x, 10x` یا `{PHRASE_EXAMPLE}`",
    "es": f"Ejemplo: `5x, 10x` o `{PHRASE_EXAMPLE}`",
    "fr": f"Exemple : `5x, 10x` ou `{PHRASE_EXAMPLE}`",
    "id": f"Contoh: `5x, 10x` atau `{PHRASE_EXAMPLE}`",
    "it": f"Esempio: `5x, 10x` o `{PHRASE_EXAMPLE}`",
    "pt": f"Exemplo: `5x, 10x` ou `{PHRASE_EXAMPLE}`",
    "ru": f"Пример: `5x, 10x` или `{PHRASE_EXAMPLE}`",
    "uz": f"Misol: `5x, 10x` yoki `{PHRASE_EXAMPLE}`",
    "zh": f"示例：`5x, 10x` 或 `{PHRASE_EXAMPLE}`",
}

ADD_PROMPT_WL = {
    "en": "✅ **Add to whitelist:**\n\nSend words **or a whole phrase** to allow. Separate several entries with a comma or one per line.\n" + PHRASE_EXAMPLE_TXT["en"],
    "fa": "✅ **افزودن به لیست سفید:**\n\nکلمه **یا یک عبارت کامل** را بفرستید. چند کلید را با کاما یا هر کدام در یک خط جدا کنید.\n" + PHRASE_EXAMPLE_TXT["fa"],
    "es": "✅ **Añadir a la lista blanca:**\n\nEnvía palabras **o una frase completa** para permitir. Separa varias entradas con una coma o una por línea.\n" + PHRASE_EXAMPLE_TXT["es"],
    "fr": "✅ **Ajouter à la liste blanche :**\n\nEnvoyez des mots **ou une phrase entière** à autoriser. Séparez plusieurs entrées par une virgule ou une par ligne.\n" + PHRASE_EXAMPLE_TXT["fr"],
    "id": "✅ **Tambah ke daftar putih:**\n\nKirim kata **atau frasa utuh** untuk diizinkan. Pisahkan beberapa entri dengan koma atau satu per baris.\n" + PHRASE_EXAMPLE_TXT["id"],
    "it": "✅ **Aggiungi alla lista bianca:**\n\nInvia parole **o una frase intera** da consentire. Separa più voci con una virgola o una per riga.\n" + PHRASE_EXAMPLE_TXT["it"],
    "pt": "✅ **Adicionar à lista branca:**\n\nEnvie palavras **ou uma frase inteira** para permitir. Separe várias entradas com vírgula ou uma por linha.\n" + PHRASE_EXAMPLE_TXT["pt"],
    "ru": "✅ **Добавить в белый список:**\n\nОтправьте слова **или целую фразу** для разрешения. Несколько записей разделяйте запятой или по одной в строке.\n" + PHRASE_EXAMPLE_TXT["ru"],
    "uz": "✅ **Oq ro'yxatga qo'shish:**\n\nSo'z **yoki to'liq ibora** yuboring. Bir nechta yozuvni vergul bilan yoki har birini alohida qatorda ajrating.\n" + PHRASE_EXAMPLE_TXT["uz"],
    "zh": "✅ **添加到白名单：**\n\n发送单词**或整个短语**进行允许。用逗号或每行一个分隔多个条目。\n" + PHRASE_EXAMPLE_TXT["zh"],
}

ADD_PROMPT_BL = {
    "en": "🚫 **Add to blacklist:**\n\nSend words **or a whole phrase** to block. Separate several entries with a comma or one per line.\n" + PHRASE_EXAMPLE_TXT["en"],
    "fa": "🚫 **افزودن به لیست سیاه:**\n\nکلمه **یا یک عبارت کامل** را بفرستید. چند کلید را با کاما یا هر کدام در یک خط جدا کنید.\n" + PHRASE_EXAMPLE_TXT["fa"],
    "es": "🚫 **Añadir a la lista negra:**\n\nEnvía palabras **o una frase completa** para bloquear. Separa varias entradas con una coma o una por línea.\n" + PHRASE_EXAMPLE_TXT["es"],
    "fr": "🚫 **Ajouter à la liste noire :**\n\nEnvoyez des mots **ou une phrase entière** à bloquer. Séparez plusieurs entrées par une virgule ou une par ligne.\n" + PHRASE_EXAMPLE_TXT["fr"],
    "id": "🚫 **Tambah ke daftar hitam:**\n\nKirim kata **atau frasa utuh** untuk diblokir. Pisahkan beberapa entri dengan koma atau satu per baris.\n" + PHRASE_EXAMPLE_TXT["id"],
    "it": "🚫 **Aggiungi alla lista nera:**\n\nInvia parole **o una frase intera** da bloccare. Separa più voci con una virgola o una per riga.\n" + PHRASE_EXAMPLE_TXT["it"],
    "pt": "🚫 **Adicionar à lista negra:**\n\nEnvie palavras **ou uma frase inteira** para bloquear. Separe várias entradas com vírgula ou uma por linha.\n" + PHRASE_EXAMPLE_TXT["pt"],
    "ru": "🚫 **Добавить в чёрный список:**\n\nОтправьте слова **или целую фразу** для блокировки. Несколько записей разделяйте запятой или по одной в строке.\n" + PHRASE_EXAMPLE_TXT["ru"],
    "uz": "🚫 **Qora ro'yxatga qo'shish:**\n\nSo'z **yoki to'liq ibora** yuboring. Bir nechta yozuvni vergul bilan yoki har birini alohida qatorda ajrating.\n" + PHRASE_EXAMPLE_TXT["uz"],
    "zh": "🚫 **添加到黑名单：**\n\n发送单词**或整个短语**进行屏蔽。用逗号或每行一个分隔多个条目。\n" + PHRASE_EXAMPLE_TXT["zh"],
}

VERBS_HINT = {
    "en": "Manage `{cmd}` with: `list` · `add` · `del` · `clear` · `help`",
    "fa": "مدیریت `{cmd}` با: `list` · `add` · `del` · `clear` · `help`",
    "es": "Gestiona `{cmd}` con: `list` · `add` · `del` · `clear` · `help`",
    "fr": "Gérez `{cmd}` avec : `list` · `add` · `del` · `clear` · `help`",
    "id": "Kelola `{cmd}` dengan: `list` · `add` · `del` · `clear` · `help`",
    "it": "Gestisci `{cmd}` con: `list` · `add` · `del` · `clear` · `help`",
    "pt": "Gerencie `{cmd}` com: `list` · `add` · `del` · `clear` · `help`",
    "ru": "Управляйте `{cmd}`: `list` · `add` · `del` · `clear` · `help`",
    "uz": "`{cmd}` ni boshqaring: `list` · `add` · `del` · `clear` · `help`",
    "zh": "管理 `{cmd}`：`list` · `add` · `del` · `clear` · `help`",
}

batch = {
    "ui_words_help_title": HELP_TITLE,
    "ui_words_phrase_example": PHRASE_EXAMPLE_TXT,
    "ui_prompt_add_whitelist": ADD_PROMPT_WL,
    "ui_prompt_add_blacklist": ADD_PROMPT_BL,
    "cmd_words_verbs_hint": VERBS_HINT,
}

report = module.validate_and_append(batch)
print("exit:", report)
raise SystemExit(report)
