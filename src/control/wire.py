"""Legacy MiPlay TCP/8899 protocol frame and challenge codec."""
import struct
import hashlib
import hmac

MAGIC = b"$"

def frame(cmd, seq, payload=b""):
    return MAGIC + struct.pack(">HHI", cmd, seq, len(payload)) + payload

def recv_exact(sock, n):
    b=bytearray()
    while len(b)<n:
        part=sock.recv(n-len(b))
        if not part: raise EOFError("peer closed")
        b.extend(part)
    return bytes(b)

def recv_frame(sock):
    h=recv_exact(sock,9)
    if h[:1]!=MAGIC: raise ValueError(f"bad MiPlay magic: {h[:9].hex()}")
    cmd,seq,n=struct.unpack(">HHI",h[1:])
    if n>1024*1024: raise ValueError(f"frame too large: {n}")
    return cmd,seq,recv_exact(sock,n) if n else b""

def legacy_hmac(challenge):
    key=hashlib.md5(b"0.0.0.0").hexdigest().encode("ascii")
    return hmac.new(key,challenge,hashlib.sha1).hexdigest().encode("ascii")

