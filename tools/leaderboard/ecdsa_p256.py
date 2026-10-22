"""ECDSA P-256 (secp256r1) with SHA-256 in plain Python - verifying the game's signed leaderboard submissions
without installing anything on the GitHub runner. sign() is only for tests."""
import hashlib, secrets

P = 0xffffffff00000001000000000000000000000000ffffffffffffffffffffffff
A = P - 3
B = 0x5ac635d8aa3a93e7b3ebbd55769886bc651d06b0cc53b0f63bce3c3e27d2604b
N = 0xffffffff00000000ffffffffffffffffbce6faada7179e84f3b9cac2fc632551
G = (0x6b17d1f2e12c4247f8bce6e563a440f277037d812deb33a0f4a13945d898c296,
     0x4fe342e2fe1a7f9b8ee7eb4a7c0f9e162bce33576b315ececbb6406837bf51f5)


def on_curve(Q):
    x, y = Q
    return 0 <= x < P and 0 <= y < P and (y * y - (x * x * x + A * x + B)) % P == 0


def add(p1, p2):
    if p1 is None: return p2
    if p2 is None: return p1
    (x1, y1), (x2, y2) = p1, p2
    if x1 == x2 and (y1 + y2) % P == 0: return None
    if p1 == p2: m = (3 * x1 * x1 + A) * pow(2 * y1, -1, P) % P
    else: m = (y2 - y1) * pow(x2 - x1, -1, P) % P
    x3 = (m * m - x1 - x2) % P
    return x3, (m * (x1 - x3) - y1) % P


def mul(k, pt):
    r = None
    while k:
        if k & 1: r = add(r, pt)
        pt = add(pt, pt); k >>= 1
    return r


def verify(pub_hex, sig_hex, msg):
    """pub: hex X||Y (64 bytes), sig: hex r||s (64 bytes), msg: bytes. True if valid."""
    try:
        pub, sig = bytes.fromhex(pub_hex), bytes.fromhex(sig_hex)
    except ValueError:
        return False
    if len(pub) != 64 or len(sig) != 64: return False
    Q = (int.from_bytes(pub[:32], 'big'), int.from_bytes(pub[32:], 'big'))
    r, s = int.from_bytes(sig[:32], 'big'), int.from_bytes(sig[32:], 'big')
    if not on_curve(Q) or not (1 <= r < N and 1 <= s < N): return False
    z = int.from_bytes(hashlib.sha256(msg).digest(), 'big')
    w = pow(s, -1, N)
    X = add(mul(z * w % N, G), mul(r * w % N, Q))
    return X is not None and X[0] % N == r


def keypair():
    d = secrets.randbelow(N - 1) + 1
    Q = mul(d, G)
    return d, (Q[0].to_bytes(32, 'big') + Q[1].to_bytes(32, 'big')).hex()


def sign(d, msg):
    z = int.from_bytes(hashlib.sha256(msg).digest(), 'big')
    while True:
        k = secrets.randbelow(N - 1) + 1
        r = mul(k, G)[0] % N
        s = pow(k, -1, N) * (z + r * d) % N
        if r and s: return (r.to_bytes(32, 'big') + s.to_bytes(32, 'big')).hex()


if __name__ == '__main__':   # self-test
    d, pub = keypair()
    sig = sign(d, b'hello')
    assert verify(pub, sig, b'hello'), 'a good signature failed'
    assert not verify(pub, sig, b'hellp'), 'a changed message passed'
    d2, pub2 = keypair()
    assert not verify(pub2, sig, b'hello'), 'another key passed'
    print('ecdsa_p256 self-test ok')
