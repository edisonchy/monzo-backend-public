"""Real TLS listener with synthetic credentials and the isolated PostgreSQL DB."""

import http.client
import json
import os
import socket
import ssl
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from monzo_backend.db import engine, session
from monzo_backend.domain import ingest
from monzo_backend.models import CategoryWriteControl


def certificates(directory):
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Gate C isolated CA")])
    stamp = datetime.now(UTC)
    ca = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(stamp - timedelta(minutes=1))
        .not_valid_after(stamp + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=None,
                decipher_only=None,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )
    server = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "category-api")]))
        .issuer_name(subject)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(stamp - timedelta(minutes=1))
        .not_valid_after(stamp + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("category-api")]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(server_key.public_key()), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=None,
                decipher_only=None,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    (directory / "ca.pem").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    (directory / "server.pem").write_bytes(server.public_bytes(serialization.Encoding.PEM))
    key_path = directory / "server.key"
    key_path.write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)


def request(port, context, path, *, method="GET", body=None, bearer=None, hostname="category-api"):
    with socket.create_connection(("127.0.0.1", port), timeout=3) as raw:
        with context.wrap_socket(raw, server_hostname=hostname) as sock:
            payload = json.dumps(body).encode() if body is not None else b""
            headers = f"{method} {path} HTTP/1.1\r\nHost: category-api:8443\r\nConnection: close\r\nContent-Length: {len(payload)}\r\nContent-Type: application/json\r\n"
            if bearer:
                headers += "Authorization: Bearer " + bearer + "\r\n"
            sock.sendall(headers.encode() + b"\r\n" + payload)
            response = http.client.HTTPResponse(sock)
            response.begin()
            return response.status, response.read()


def test_real_https_commands_ca_and_hostname_validation(tmp_path, payment):
    certificates(tmp_path)
    signing = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    (tmp_path / "jwks.json").write_text(jwt.algorithms.RSAAlgorithm.to_jwk(signing.public_key()))
    with session() as s, s.begin():
        s.add(CategoryWriteControl(id=1, enabled=True))
        ingest(s, "acc_test", payment)
    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
    env = {k: v for k, v in os.environ.items() if not k.startswith("MONZO_")}
    env.update(
        MONZO_CATEGORY_ENABLED="true",
        MONZO_CATEGORY_DATABASE_URL=engine().url.render_as_string(hide_password=False),
        MONZO_CATEGORY_ISSUER="https://identity.test/realms/finance",
        MONZO_CATEGORY_JWKS_URL="https://identity.test/realms/finance/certs",
        MONZO_CATEGORY_OWNER_SUBJECT="test-owner",
        GATE_C_DIRECTORY=str(tmp_path),
        GATE_C_PORT=str(port),
    )
    code = """
import os,time,jwt,uvicorn
from pathlib import Path
from monzo_backend.category_api.app import create_app
from monzo_backend.category_api.security import CommandSettings,CommandVerifier
p=Path(os.environ["GATE_C_DIRECTORY"])
s=CommandSettings();v=CommandVerifier(s)
v.keys={"test-key":jwt.PyJWK.from_json((p/"jwks.json").read_text()).key}
v.expires=time.monotonic()+60
uvicorn.run(create_app(s,verifier=v),host="127.0.0.1",port=int(os.environ["GATE_C_PORT"]),ssl_certfile=str(p/"server.pem"),ssl_keyfile=str(p/"server.key"),access_log=False,proxy_headers=False,log_level="critical")
"""
    context = ssl.create_default_context(cafile=str(tmp_path / "ca.pem"))
    process = subprocess.Popen(
        [sys.executable, "-c", code], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        end = time.monotonic() + 10
        while True:
            assert process.poll() is None, "TLS listener exited"
            try:
                assert request(port, context, "/health")[0] == 200
                break
            except ConnectionRefusedError:
                assert time.monotonic() < end, "TLS listener did not start"
                time.sleep(0.05)
        with pytest.raises(ssl.SSLCertVerificationError):
            request(port, ssl.create_default_context(), "/health")
        with pytest.raises(ssl.SSLCertVerificationError):
            request(port, context, "/health", hostname="wrong-service")
        assert request(port, context, "/v1/status")[0] == 404
        assert request(port, context, "/internal/categories/options")[0] == 401
        stamp = int(time.time())
        bearer = jwt.encode(
            {
                "iss": env["MONZO_CATEGORY_ISSUER"],
                "sub": "test-owner",
                "azp": "https://spending.example.invalid/mcp",
                "aud": "finance-category-api",
                "iat": stamp,
                "exp": stamp + 60,
                "typ": "Bearer",
                "scope": "finance:read finance:categories:write",
            },
            signing,
            algorithm="RS256",
            headers={"kid": "test-key"},
        )
        status, raw = request(
            port,
            context,
            "/internal/categories/previews",
            method="POST",
            body={"transaction_id": "tx_test", "category": "groceries", "expected_version": 1},
            bearer=bearer,
        )
        assert status == 201, raw
        operation = json.loads(raw)["operation_id"]
        status, raw = request(
            port,
            context,
            f"/internal/categories/operations/{operation}/apply",
            method="POST",
            body={},
            bearer=bearer,
        )
        assert status == 200, raw
        assert json.loads(raw)["receipt"]["changed"] == 1
        status, raw = request(
            port, context, f"/internal/categories/operations/{operation}", bearer=bearer
        )
        assert status == 200 and json.loads(raw)["state"] == "committed"
        with socket.create_connection(("127.0.0.1", port), timeout=3) as plain:
            plain.sendall(b"GET /health HTTP/1.1\r\nHost: category-api:8443\r\n\r\n")
            try:
                reply = plain.recv(1024)
            except ConnectionResetError:
                reply = b""
            assert b"200 OK" not in reply
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
