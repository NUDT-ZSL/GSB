"""Deterministic certificate creation helpers built on ``cryptography``.

All keys use fixed public exponents and a deterministic ``random_alternate``
style backend is unnecessary because RSA is generated with a fixed exponent and
we pin every serial, subject attribute, validity bound and extension.  The key
material itself still comes from the OS RNG, but topology, names, timestamps,
fingerprints and the resulting trust store content are fully deterministic in
structure and every stored digest is recomputed from the emitted DER bytes.
"""

from __future__ import annotations

import datetime as _dt

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa, padding
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID


def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def ec_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _not_before(epoch_ms: int) -> _dt.datetime:
    return _dt.datetime.fromtimestamp(epoch_ms / 1000, tz=_dt.timezone.utc)


def _not_after(epoch_ms: int, validity_ms: int) -> _dt.datetime:
    return _dt.datetime.fromtimestamp((epoch_ms + validity_ms) / 1000, tz=_dt.timezone.utc)


def make_ca(
    subject_cn: str,
    issuer_key,
    issuer_name: x509.Name,
    serial: int,
    epoch_ms: int,
    validity_ms: int,
    path_length: int | None,
    is_root: bool = False,
):
    key = ec_key()
    subject = name(subject_cn)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject if is_root else issuer_name)
        .public_key(key.public_key())
        .serial_number(serial)
        .not_valid_before(_not_before(epoch_ms))
        .not_valid_after(_not_after(epoch_ms, validity_ms))
        .add_extension(x509.BasicConstraints(ca=True, path_length=path_length), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
    )
    signing_key = key if is_root else issuer_key
    cert = builder.sign(signing_key, hashes.SHA256())
    return key, cert


def make_leaf(
    subject_cn: str,
    san_hosts: list[str],
    issuer_key,
    issuer_name: x509.Name,
    serial: int,
    epoch_ms: int,
    validity_ms: int,
    purposes: list[str],
    dns_critical: bool = False,
):
    key = rsa_key()
    eku_oids = []
    for purpose in purposes:
        if purpose == "serverAuth":
            eku_oids.append(ExtendedKeyUsageOID.SERVER_AUTH)
        elif purpose == "clientAuth":
            eku_oids.append(ExtendedKeyUsageOID.CLIENT_AUTH)
        else:
            raise ValueError(f"unsupported purpose {purpose}")
    builder = (
        x509.CertificateBuilder()
        .subject_name(name(subject_cn))
        .issuer_name(issuer_name)
        .public_key(key.public_key())
        .serial_number(serial)
        .not_valid_before(_not_before(epoch_ms))
        .not_valid_after(_not_after(epoch_ms, validity_ms))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_encipherment=True,
                content_commitment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage(eku_oids), critical=False)
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(host) for host in san_hosts]),
            critical=dns_critical,
        )
    )
    cert = builder.sign(issuer_key, hashes.SHA256())
    return key, cert


def pem_cert(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def pem_key(key) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def sha256_fingerprint(cert: x509.Certificate) -> str:
    return cert.fingerprint(hashes.SHA256()).hex()


def verify_signature(child: x509.Certificate, issuer: x509.Certificate) -> None:
    """Raise ``InvalidSignature`` (or algorithm error) on a bad signature."""
    public_key = issuer.public_key()
    if isinstance(public_key, rsa.RSAPublicKey):
        public_key.verify(child.signature, child.tbs_certificate_bytes, padding.PKCS1v15(), child.signature_hash_algorithm)
    elif isinstance(public_key, ec.EllipticCurvePublicKey):
        public_key.verify(child.signature, child.tbs_certificate_bytes, ec.ECDSA(child.signature_hash_algorithm))
    else:
        raise TypeError("unsupported issuer key type")
