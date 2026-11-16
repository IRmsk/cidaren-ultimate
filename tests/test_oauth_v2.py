# -*- coding: utf-8 -*-
"""OAuth V2 login, generated crypto.json, and jv=99 regression tests."""
from __future__ import annotations

import base64
import hashlib
import json as jsonlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class FakeResponse:
    def __init__(self, value):
        self.value = value

    def raise_for_status(self):
        return None

    def json(self):
        return self.value


class FakeOAuthSession:
    def __init__(self):
        self.shared_secret = None
        self.salt = b"synthetic-oauth-salt"

    def post(self, _url, *, json, **_kwargs):
        from core.oauth import CRYPTO_VERSION, LOGIN_AAD, LOGIN_HKDF_INFO

        body = json
        client_public = serialization.load_der_public_key(base64.b64decode(body["cpub_k"]))
        server_private = ec.generate_private_key(ec.SECP256R1())
        self.shared_secret = server_private.exchange(ec.ECDH(), client_public)
        server_der = server_private.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=self.salt,
            info=LOGIN_HKDF_INFO,
        ).derive(self.shared_secret)
        iv = bytes(range(12))
        ciphertext = AESGCM(key).encrypt(
            iv,
            jsonlib.dumps({"token": "synthetic-token"}).encode("utf-8"),
            LOGIN_AAD,
        )
        return FakeResponse(
            {
                "code": 1,
                "data": {
                    "handshake_required": False,
                    "encrypted": True,
                    "handshake": {
                        "version": CRYPTO_VERSION,
                        "spub_k": base64.b64encode(server_der).decode("ascii"),
                        "salt": base64.b64encode(self.salt).decode("ascii"),
                    },
                    "payload": {
                        "version": CRYPTO_VERSION,
                        "aad": LOGIN_AAD.decode("ascii"),
                        "iv": base64.b64encode(iv).decode("ascii"),
                        "cipher_text": base64.b64encode(ciphertext).decode("ascii"),
                    },
                },
            }
        )

    def get(self, _url, **_kwargs):
        return FakeResponse(
            {
                "code": 1,
                "data": {"user_info": {"student_name": "synthetic-student"}},
            }
        )


def callback_for(authorization):
    values = parse_qs(urlsplit(authorization.authorization_url).query)
    state = values["state"][0]
    redirect = unquote(values["redirect_uri"][0])
    marker = parse_qs(urlsplit(redirect).query)["authorize"][0]
    return f"https://app.vocabgo.com/student/?authorize={marker}&code=synthetic-code&state={state}"


class OAuthV2Test(unittest.TestCase):
    def test_login_writes_token_and_crypto_file(self):
        from core import config
        from core.crypto import clear_crypto_document
        from core.login import complete_login, login_start
        from core.oauth import OAUTH_USER_AGENT

        old_env_file = config.ENV_FILE
        old_env = {key: os.environ.get(key) for key in config.CONFIG_FIELDS}
        old_crypto_env = os.environ.get("CIDAREN_CRYPTO_FILE")
        tmp = Path(tempfile.mkdtemp())
        config.set_env_file(str(tmp / ".env"))
        os.environ["CIDAREN_CRYPTO_FILE"] = str(tmp / "config" / "crypto.json")
        fake = FakeOAuthSession()
        try:
            session = login_start()
            result = complete_login(
                callback_for(type("Auth", (), {
                    "authorization_url": session["authorization_url"],
                })()),
                session["state_digest"],
                session["marker_digest"],
                session=fake,
            )
            self.assertEqual(result["token"], "synthetic-token")
            self.assertEqual(result["name"], "synthetic-student")
            self.assertEqual(result["version"], "2.7.0.261019_01")

            env_text = (tmp / ".env").read_text(encoding="utf-8")
            self.assertIn("USERTOKEN=synthetic-token", env_text)
            self.assertIn(
                f"ABC={hashlib.md5(OAUTH_USER_AGENT.encode('utf-8')).hexdigest()}",
                env_text,
            )

            crypto = jsonlib.loads((tmp / "config" / "crypto.json").read_text(encoding="utf-8"))
            self.assertEqual(crypto["token"], "synthetic-token")
            self.assertEqual(base64.b64decode(crypto["shared_secret"]), fake.shared_secret)
            self.assertEqual(base64.b64decode(crypto["salt"]), fake.salt)
            self.assertEqual(
                base64.b64decode(crypto["login_info"]["a"][1:]),
                fake.shared_secret,
            )
        finally:
            config.set_env_file(str(old_env_file))
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            if old_crypto_env is None:
                os.environ.pop("CIDAREN_CRYPTO_FILE", None)
            else:
                os.environ["CIDAREN_CRYPTO_FILE"] = old_crypto_env
            clear_crypto_document()

    def test_jv99_dict_payload_is_decrypted(self):
        from core.crypto import clear_crypto_document, decrypt_response, set_crypto_document

        secret = bytes(range(32))
        salt = b"synthetic-salt"
        iv = bytes(range(12))
        aad = "vcg-exam:test-account"
        key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            info=b"vcg-exam-aes:test-account",
        ).derive(secret)
        ciphertext = AESGCM(key).encrypt(iv, b'{"ok": true}', aad.encode("utf-8"))
        set_crypto_document(
            {
                "login_info": {
                    "a": "h" + base64.b64encode(secret).decode("ascii"),
                    "b": "a" + base64.b64encode(salt).decode("ascii"),
                }
            }
        )
        try:
            response = decrypt_response(
                {
                    "code": 1,
                    "jv": 99,
                    "data": {
                        "iv": base64.b64encode(iv).decode("ascii"),
                        "cipher_text": base64.b64encode(ciphertext).decode("ascii"),
                        "aad": aad,
                    },
                }
            )
            self.assertEqual(response["data"], {"ok": True})
        finally:
            clear_crypto_document()

    def test_client_derives_auth_headers_from_actual_ua(self):
        from core.api import Client, DEFAULT_AUTH_V, VERSION

        ua = "Synthetic User Agent"
        client = Client("token", ua=ua)
        self.assertEqual(client.s.headers["abc"], hashlib.md5(ua.encode("utf-8")).hexdigest())
        self.assertEqual(client.s.headers["authorization-v"], DEFAULT_AUTH_V)
        self.assertEqual(DEFAULT_AUTH_V, "cfcd208495d565ef66e7dff9f98764da")
        self.assertEqual(VERSION, "2.7.0.261019_01")

    def test_class_and_study_requests_use_their_protocol_bases(self):
        from core import api
        from core.api import Client

        client = Client("token")
        seen = []

        def fake_get(url, **_kwargs):
            seen.append(("GET", url))
            return FakeResponse({"code": 1, "data": {}})

        def fake_post(url, **_kwargs):
            seen.append(("POST", url))
            return FakeResponse({"code": 1, "data": {}})

        client.s.get = fake_get
        client.s.post = fake_post
        client.page_task()
        client.study_task_list()
        self.assertTrue(seen[0][1].startswith(api.BASE))
        self.assertTrue(seen[1][1].startswith(api.STUDENT_API))
        self.assertIn("/Student/StudyTask/List", seen[1][1])
        self.assertNotIn("/Student/Student/", seen[1][1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
