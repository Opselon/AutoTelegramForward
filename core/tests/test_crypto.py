import pytest

from core.infrastructure.security.crypto import CryptoService


def test_roundtrip():
    c = CryptoService("secret-master-key")
    token = c.encrypt("hello world 123")
    assert token != "hello world 123"
    assert c.decrypt(token) == "hello world 123"


def test_unique_nonces():
    c = CryptoService("k")
    a = c.encrypt("same text")
    b = c.encrypt("same text")
    assert a != b


def test_wrong_key_fails():
    c1 = CryptoService("key-one")
    c2 = CryptoService("key-two")
    token = c1.encrypt("payload")
    with pytest.raises(Exception):
        c2.decrypt(token)


def test_empty_key_rejected():
    with pytest.raises(ValueError):
        CryptoService("")
