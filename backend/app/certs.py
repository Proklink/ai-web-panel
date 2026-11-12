"""Клиентские сертификаты устройств (mTLS).

Свой CA лежит в /etc/webpanel/client-ca (ключ — только root). Caddy доверяет его публичному
сертификату (/etc/caddy/webpanel-client-ca.crt) и пропускает только устройства с сертификатом
от этого CA. Приватные ключи устройств на сервере не хранятся: .p12 отдаётся один раз при выпуске.
"""
import datetime as dt
import hashlib
import os
import secrets
import shutil
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

CA_DIR = Path(os.environ.get("WEBPANEL_CA_DIR", "/etc/webpanel/client-ca"))
CA_KEY = CA_DIR / "ca.key"
CA_CRT = CA_DIR / "ca.crt"
CADDY_CA = Path(os.environ.get("WEBPANEL_CADDY_CA", "/etc/caddy/webpanel-client-ca.crt"))
CADDY_SERVER_ROOT = Path("/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt")


def _now():
    return dt.datetime.now(dt.timezone.utc)


def ensure_ca():
    """Создать CA, если его ещё нет, и выложить публичную часть для Caddy."""
    if not (CA_KEY.exists() and CA_CRT.exists()):
        CA_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(CA_DIR, 0o700)
        key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Webpanel device CA"),
                          x509.NameAttribute(NameOID.ORGANIZATION_NAME, "webpanel")])
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(_now() - dt.timedelta(minutes=5))
                .not_valid_after(_now() + dt.timedelta(days=3650))
                .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False, key_encipherment=False,
                                             data_encipherment=False, key_agreement=False, key_cert_sign=True,
                                             crl_sign=True, encipher_only=False, decipher_only=False), critical=True)
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
                .sign(key, hashes.SHA256()))
        CA_KEY.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                             serialization.NoEncryption()))
        os.chmod(CA_KEY, 0o600)
        CA_CRT.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    if CADDY_CA.parent.exists():
        if not CADDY_CA.exists() or CADDY_CA.read_bytes() != CA_CRT.read_bytes():
            shutil.copyfile(CA_CRT, CADDY_CA)
        os.chmod(CADDY_CA, 0o644)


def fingerprint(cert: x509.Certificate) -> str:
    """SHA-256 от DER — так же считает Caddy в {http.request.tls.client.fingerprint}."""
    return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


def issue(name: str, days: int = 365) -> dict:
    ensure_ca()
    ca_key = serialization.load_pem_private_key(CA_KEY.read_bytes(), None)
    ca_cert = x509.load_pem_x509_certificate(CA_CRT.read_bytes())
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)  # RSA — лучшая совместимость с телефонами
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name[:64]),
                         x509.NameAttribute(NameOID.ORGANIZATION_NAME, "webpanel device")])
    not_after = _now() + dt.timedelta(days=days)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(ca_cert.subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(_now() - dt.timedelta(minutes=5)).not_valid_after(not_after)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=True,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                         crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    password = "-".join(secrets.token_hex(2) for _ in range(4))
    # Старый формат шифрования PKCS#12 (3DES/SHA1): его понимают и iOS, и старые Android, и macOS.
    enc = (serialization.PrivateFormat.PKCS12.encryption_builder().kdf_rounds(50000)
           .key_cert_algorithm(pkcs12.PBES.PBESv1SHA1And3KeyTripleDESCBC).hmac_hash(hashes.SHA1())
           .build(password.encode()))
    p12 = pkcs12.serialize_key_and_certificates(name[:64].encode(), key, cert, [ca_cert], enc)
    return {"p12": p12, "password": password, "fingerprint": fingerprint(cert),
            "serial": format(cert.serial_number, "x"), "not_after": not_after}


def server_root_ca() -> bytes | None:
    """Корневой сертификат Caddy (для доверия к самоподписанному HTTPS на устройствах)."""
    try:
        return CADDY_SERVER_ROOT.read_bytes()
    except OSError:
        return None
