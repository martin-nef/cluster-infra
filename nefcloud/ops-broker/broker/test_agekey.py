"""Tests for agekey.py: age identities in pure Python. Stdlib only.

The vectors are throwaway keys made by age-keygen v1.2.1 for this test and
nothing else: an identity (32 bytes, bech32) and the public key age derives."""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agekey  # noqa: E402

VECTORS = [
    ('AGE-SECRET-KEY-1XYX89H353M2RQNP5DVQX82ZZ56FV6EMU3RW9LXQMX7CSAGEDYJKS22ECDC', 'age1079cvelc6wkjug43235nw9h7fzke37h3h9a8g4auda8gytevn5js6ye5rf'),
    ('AGE-SECRET-KEY-1RPV8WW54KN8CD695MFLU8KH9E2ZAZEJZN068P80AG58VYRVRMWQQ93TUHP', 'age13mj68xpsjspw8qmpjgra8n4ekgglhcf04c9kpjh3k5jdzxhvrujqrgcde3'),
    ('AGE-SECRET-KEY-1U08SCUMH00PA3GHKLTUKF323AX8LJKVHV50SSNGXX7E09N7XHNTSAFHFNE', 'age1nc55azeueat507884fne234qt25jnp7cpza2cqt482q3gw2vr52q454z6t'),
]


def decode(s):
    """bech32 data part back to bytes (no checksum check: test helper only)."""
    values = [agekey.CHARSET.index(c) for c in s.lower().rpartition("1")[2][:-6]]
    acc, bits, out = 0, 0, []
    for v in values:
        acc, bits = (acc << 5) | v, bits + 5
        if bits >= 8:
            bits -= 8
            out.append((acc >> bits) & 255)
    return bytes(out)


class AgeKeyTests(unittest.TestCase):
    def test_matches_age_keygen(self):
        for secret, public in VECTORS:
            with self.subTest(public):
                scalar = decode(secret)
                self.assertEqual(len(scalar), 32)
                self.assertEqual(agekey.identity(scalar), secret)
                self.assertEqual(agekey.recipient(scalar), public)

    def test_rfc7748_x25519(self):
        # RFC 7748 section 6.1: Alice's key pair.
        a = bytes.fromhex("77076d0a7318a57d3c16c17251b26645df4c2f87ebc0992ab177fba51db92c2a")
        self.assertEqual(agekey.x25519(a, (9).to_bytes(32, "little")).hex(),
                         "8520f0098930a754748b7ddcb43ef75a0dbf3a0d26381af4eba4a98eaa9b4e6a")

    def test_generate_writes_a_file_like_age_keygen(self):
        key_file, public = agekey.generate(now=0)
        lines = key_file.splitlines()
        self.assertEqual(lines[0], "# created: 1970-01-01T00:00:00Z")
        self.assertEqual(lines[1], f"# public key: {public}")
        self.assertRegex(lines[2], r"^AGE-SECRET-KEY-1[0-9A-Z]{58}$")
        self.assertRegex(public, r"^age1[0-9a-z]{58}$")
        self.assertEqual(agekey.recipient(decode(lines[2])), public)
        self.assertNotEqual(agekey.generate()[1], public)  # fresh randomness each time


if __name__ == "__main__":
    unittest.main()
