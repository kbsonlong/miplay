"""RTSP message formatting and incremental parser."""
from datetime import datetime, timezone

def date_header():
    return datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")

def rtsp(start, headers=(), body=b""):
    hs=list(headers)
    if not any(k.lower()=="content-length" for k,v in hs):
        hs.append(("Content-Length",str(len(body))))
    text=start+"\r\n"+"".join(f"{k}: {v}\r\n" for k,v in hs)+"\r\n"
    return text.encode("ascii")+body

def parse_rtsp(buf):
    marker=buf.find(b"\r\n\r\n")
    if marker<0: return None
    head=buf[:marker].decode("latin1")
    lines=head.split("\r\n")
    length=0
    for line in lines[1:]:
        if ":" in line:
            k,v=line.split(":",1)
            if k.lower()=="content-length":
                try: length=max(0,min(65536,int(v.strip())))
                except ValueError: return ("BAD",{},b"",marker+4)
    total=marker+4+length
    if len(buf)<total: return None
    headers={}
    for line in lines[1:]:
        if ":" in line:
            k,v=line.split(":",1); headers[k.strip().lower()]=v.strip()
    return (lines[0],headers,buf[marker+4:total],total)

