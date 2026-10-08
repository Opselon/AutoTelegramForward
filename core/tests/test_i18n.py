import pytest

from core.infrastructure.telegram.i18n import I18n, SUPPORTED_LANGUAGES


@pytest.fixture()
def i18n():
    return I18n()


def test_supported_languages(i18n):
    assert set(SUPPORTED_LANGUAGES) == {"en", "fa", "ru", "zh"}
    for lang in SUPPORTED_LANGUAGES:
        assert i18n.set_language(lang)


def test_unknown_language_rejected(i18n):
    assert not i18n.set_language("xx")
    assert i18n.current == "en"


def test_fallback_to_english(i18n):
    i18n.set_language("en")
    assert i18n.t("definitely_missing_key_xyz") == "definitely_missing_key_xyz"


def test_formatting(i18n):
    assert "123" in i18n.t("login_success", phone="123")


def test_fa_rtl_content(i18n):
    assert i18n.set_language("fa")
    text = i18n.t("choose_lang")
    assert "زبان" in text


def test_ru_and_zh_have_start(i18n):
    i18n.set_language("ru")
    assert "Добро пожаловать" in i18n.t("start")
    i18n.set_language("zh")
    assert "欢迎" in i18n.t("start")
