"""age X25519 identities in pure Python, for create-sops-age-key.

The stock python image has neither X25519 nor bech32, and an image with
`age` in it would mean a registry and a build workflow (README, "It lives in
cluster-infra"). An age identity is 32 random bytes; its recipient is the
X25519 public key of those bytes. Both are bech32 (BIP 173), with the
"AGE-SECRET-KEY-" and "age" prefixes. tests/test_agekey checks this against
keys from age-keygen.

X25519 is RFC 7748's Montgomery ladder. It isn't constant-time: it runs once
per approved request, in a pod nothing outside can time.
"""
import secrets
import time

P = 2 ** 255 - 19
A24 = 121665
CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def x25519(scalar, u):
    k = bytearray(scalar)
    k[0] &= 248
    k[31] &= 127
    k[31] |= 64
    k = int.from_bytes(k, "little")
    x1 = int.from_bytes(u, "little") & ((1 << 255) - 1)
    x2, z2, x3, z3, swap = 1, 0, x1, 1, 0
    for t in reversed(range(255)):
        bit = (k >> t) & 1
        swap ^= bit
        if swap:
            x2, x3, z2, z3 = x3, x2, z3, z2
        swap = bit
        a, b = (x2 + z2) % P, (x2 - z2) % P
        aa, bb = a * a % P, b * b % P
        e = (aa - bb) % P
        c, d = (x3 + z3) % P, (x3 - z3) % P
        da, cb = d * a % P, c * b % P
        x3, z3 = (da + cb) ** 2 % P, x1 * (da - cb) ** 2 % P
        x2, z2 = aa * bb % P, e * (aa + A24 * e) % P
    if swap:
        x2, z2 = x3, z3
    return (x2 * pow(z2, P - 2, P) % P).to_bytes(32, "little")


def _polymod(values):
    gen = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    chk = 1
    for v in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if (top >> i) & 1 else 0
    return chk


def _bits5(data):
    acc, bits, out = 0, 0, []
    for byte in data:
        acc, bits = (acc << 8) | byte, bits + 8
        while bits >= 5:
            bits -= 5
            out.append((acc >> bits) & 31)
    if bits:
        out.append((acc << (5 - bits)) & 31)
    return out


def bech32(hrp, data):
    values = _bits5(data)
    expanded = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]
    poly = _polymod(expanded + values + [0] * 6) ^ 1
    checksum = [(poly >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(CHARSET[v] for v in values + checksum)


def recipient(scalar):
    """The age1... public key of an identity's 32 bytes."""
    return bech32("age", x25519(scalar, (9).to_bytes(32, "little")))


def identity(scalar):
    return bech32("age-secret-key-", scalar).upper()


def generate(now=None):
    """(the age.agekey file, as age-keygen writes it, its public key)."""
    scalar = secrets.token_bytes(32)
    public = recipient(scalar)
    created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    return f"# created: {created}\n# public key: {public}\n{identity(scalar)}\n", public
