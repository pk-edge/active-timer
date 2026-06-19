from app.core import update_signing


def test_sign_verify_roundtrip():
    data = '{"version":"1.2.0"}'
    sig = update_signing.sign(data)
    assert isinstance(sig, str) and sig
    assert update_signing.verify(data, sig) is True


def test_verify_rejects_tampered_payload_and_sig():
    data = '{"version":"1.2.0"}'
    sig = update_signing.sign(data)
    assert update_signing.verify('{"version":"9.9.9"}', sig) is False
    assert update_signing.verify(data, "AAAA") is False


def test_public_key_is_stable_and_b64():
    import base64
    pk = update_signing.public_key_b64()
    assert pk == update_signing.public_key_b64()
    assert len(base64.b64decode(pk)) == 32   # Ed25519 public key is 32 bytes
