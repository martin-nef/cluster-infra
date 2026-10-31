"""Tests for the token minter (minter.py), on its own.

  cd nefcloud/ops-broker/broker && python3 -I -m unittest -v

test_broker.py drives it through the broker; these cover what only the minter
decides: which tokens it hands out, the key it loads, and setup.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import githubops  # noqa: E402
import minter  # noqa: E402
from test_broker import MinterBase, write_key  # noqa: E402


class TestRS256(unittest.TestCase):
    @unittest.skipUnless(shutil.which("openssl"), "needs the openssl CLI to compare against")
    def test_matches_openssl(self):
        with tempfile.TemporaryDirectory() as d:
            for fmt in ("-traditional", ""):  # PKCS#1, as GitHub hands out; PKCS#8
                with self.subTest(fmt or "pkcs8"):
                    key = os.path.join(d, "key.pem")
                    subprocess.run(["openssl", "genrsa", "-out", key, "2048"], check=True, capture_output=True)
                    if fmt:
                        subprocess.run(["openssl", "rsa", fmt, "-in", key, "-out", key], check=True, capture_output=True)
                    expected = subprocess.run(["openssl", "dgst", "-sha256", "-sign", key], input=b"header.claims",
                                              check=True, capture_output=True).stdout
                    with open(key) as f:
                        self.assertEqual(minter.rs256(f.read(), b"header.claims"), expected)


class TestTokens(MinterBase):
    def setUp(self):
        super().setUp()
        write_key(self.key_dir)

    def raw(self, data):
        """Send bytes as they are, for requests the client wouldn't make."""
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(self.minter.path)
            s.sendall(data)
            s.shutdown(socket.SHUT_WR)
            return json.loads(b"".join(iter(lambda: s.recv(65536), b"")))

    def test_every_verb_has_a_token_table_entry(self):
        self.assertEqual(set(minter.TOKENS), set(githubops.VERBS))
        for verb, steps in minter.TOKENS.items():
            self.assertEqual(set(steps), {"check", "run"}, verb)
            for perms in steps.values():
                # Each step's permissions are within what the App was created with.
                self.assertLessEqual(perms.keys(), minter.PERMISSIONS.keys(), verb)
            self.assertTrue(all(v == "read" for v in steps["check"].values()), verb)

    def test_permissions_come_from_the_table(self):
        self.minter.token("web", "github-protect-branch", "run")
        self.assertEqual(self.gh.tokens, [(["web"], {"administration": "write"})])

    def test_refuses_what_isnt_in_the_table(self):
        cases = [
            ({"op": "token", "repo": "web", "verb": "github-protect-branch", "step": "admin"}, 400),
            ({"op": "token", "repo": "web", "verb": "delete-pod", "step": "run"}, 400),
            ({"op": "token", "repo": "web", "verb": ["github-protect-branch"], "step": "run"}, 400),
            ({"op": "token", "repo": "web", "verb": "github-protect-branch", "step": "run",
              "permissions": {"secrets": "write"}}, None),  # ignored, not honoured
            ({"op": "token", "repo": "..", "verb": "github-protect-branch", "step": "run"}, 400),
            ({"op": "token", "repo": "a/b", "verb": "github-protect-branch", "step": "run"}, 400),
            ({"op": "token", "verb": "github-protect-branch", "step": "run"}, 400),
            ({"op": "key"}, 400),
        ]
        for req, code in cases:
            with self.subTest(req):
                self.gh.tokens.clear()
                reply = self.raw(json.dumps(req).encode())
                if code is None:
                    self.assertEqual(self.gh.tokens, [(["web"], {"administration": "write"})])
                else:
                    self.assertEqual(reply["error"]["code"], code)
                    self.assertEqual(self.gh.tokens, [])

    def test_bad_requests(self):
        self.assertEqual(self.raw(b"not json")["error"]["code"], 400)
        self.assertEqual(self.raw(b"[]")["error"]["code"], 400)
        self.assertEqual(self.raw(b" " * (minter.MAX_REQUEST + 1))["error"]["code"], 413)

    def test_replies_never_carry_the_key(self):
        for op in ("status", "manifest"):
            self.assertNotIn("unused", json.dumps(self.raw(json.dumps({"op": op}).encode())))

    def test_github_errors_pass_through(self):
        self.gh.fail[("POST", "/app/installations/9/access_tokens")] = githubops.GitHubError(502, "bad gateway")
        with self.assertRaises(githubops.GitHubError) as cm:
            self.minter.token("web", "github-protect-branch", "check")
        self.assertEqual(cm.exception.code, 502)
        self.assertTrue(cm.exception.transient)

    def test_github_unreachable_is_transient(self):
        self.gh.fail[("GET", "/users/owner/installation")] = ConnectionResetError("reset")
        with self.assertRaises(githubops.GitHubError) as cm:
            self.minter.token("web", "github-protect-branch", "check")
        self.assertEqual(cm.exception.code, 503)

    def test_not_installed(self):
        self.gh.fail[("GET", "/users/owner/installation")] = githubops.GitHubError(404, "Not Found")
        with self.assertRaisesRegex(githubops.GitHubError, "installations/new"):
            self.minter.token("web", "github-protect-branch", "check")


class TestSetup(MinterBase):
    CREDS = {"app-id": "5", "slug": "martin-nef-ops", "private-key.pem": "KEY"}

    def test_mounted_key_wins_over_memory(self):
        with mock.patch.object(minter, "convert", return_value=self.CREDS), mock.patch.object(minter, "store_secret"):
            self.assertEqual(self.minter.setup("c")["slug"], "martin-nef-ops")
        write_key(self.key_dir, app_id="6", slug="from-the-volume")
        self.assertEqual(self.minter.status(), {"app_id": "6", "slug": "from-the-volume"})

    def test_refused_once_set_up(self):
        write_key(self.key_dir)
        with mock.patch.object(minter, "convert") as convert:
            with self.assertRaises(githubops.GitHubError) as cm:
                self.minter.setup("c")
        self.assertEqual(cm.exception.code, 409)
        convert.assert_not_called()

    def test_storing_failed_keeps_nothing(self):
        with mock.patch.object(minter, "convert", return_value=self.CREDS), \
                mock.patch.object(minter, "store_secret", side_effect=minter.GitHubError(409, "Conflict")):
            with self.assertRaisesRegex(githubops.GitHubError, "Delete the App"):
                self.minter.setup("c")
        self.assertIsNone(self.minter.status())

    def test_manifest(self):
        m = self.minter.manifest()
        self.assertEqual(m["redirect_url"], "https://ops.example.test/github/callback")
        self.assertEqual(m["default_permissions"], minter.PERMISSIONS)
        self.assertFalse(m["public"])

    def test_convert_refuses_an_app_it_didnt_ask_for(self):
        made = {"id": 5, "slug": "x", "pem": "KEY", "html_url": "https://github.test/apps/x",
                "owner": {"login": "Owner"}, "permissions": dict(minter.PERMISSIONS)}
        with mock.patch.object(minter, "api", return_value=made):
            self.assertEqual(minter.convert("c", "owner")["app-id"], "5")
            made["permissions"] = {**made["permissions"], "secrets": "write"}
            with self.assertRaisesRegex(ValueError, "nothing was stored"):
                minter.convert("c", "owner")
            made["permissions"], made["owner"] = minter.PERMISSIONS, {"login": "someone"}
            with self.assertRaisesRegex(ValueError, "belongs to someone"):
                minter.convert("c", "owner")

    def test_bad_conversion_stores_nothing(self):
        made = {"id": 5, "slug": "x", "pem": "KEY", "html_url": "https://github.test/apps/x",
                "owner": {"login": "someone"}, "permissions": dict(minter.PERMISSIONS)}
        with mock.patch.object(minter, "api", return_value=made), mock.patch.object(minter, "store_secret") as store:
            with self.assertRaisesRegex(githubops.GitHubError, "nothing was stored"):
                self.minter.setup("c")
        store.assert_not_called()
        self.assertIsNone(self.minter.status())


if __name__ == "__main__":
    unittest.main()
