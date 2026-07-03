"""Diagnostic microVM for PROGRAM message regression tests.

A minimal stand-in for aleph-vm's examples/example_fastapi: just enough
endpoints to prove the CRN's /vm/<hash>/ proxy plumbs paths, query strings,
request bodies, and PROGRAM-message env vars into the guest, plus the
upstream networking checks (/dns, /ip/4, /ip/6, /internet) ported to the
stdlib. Imports only FastAPI (bundled in the aleph-debian-12-python
runtime) and stdlib, so it runs unmodified in the runtime with no code
volume dependencies. Entrypoint: `main:app`.

The networking endpoints are sync `def`s: FastAPI runs them in its
threadpool, so the blocking socket/urllib calls don't stall the event loop.
"""
import os
import socket
import urllib.request

from fastapi import FastAPI
from fastapi.responses import JSONResponse

app = FastAPI()


@app.get("/")
async def index():
    return {"app": "diagnostic-vm", "status": "ok"}


@app.get("/echo")
async def echo_query(msg: str = ""):
    return {"msg": msg}


@app.post("/echo")
async def echo_body(body: dict):
    return {"body": body}


@app.get("/environ")
async def environ():
    return dict(os.environ)


NETWORK_TIMEOUT_SECONDS = 5

# Well-known anycast DNS services; a TCP connect to :443 proves L3 routing
# without depending on DNS or TLS. Same hosts as upstream example_fastapi.
IPV4_HOSTS = ["208.67.222.222", "9.9.9.9", "94.140.14.14"]
IPV6_HOSTS = ["2620:0:ccc::2", "2620:fe::fe", "2606:4700:4700::1111"]
# Full-stack check: DNS + routing + TLS + HTTP. Same URLs as upstream.
INTERNET_URLS = ["https://aleph.im/", "https://ethereum.org/en/", "https://ipfs.io/"]


@app.get("/dns")
def resolve_dns():
    hostname = "example.org"
    ipv4 = ipv6 = None
    try:
        info = socket.getaddrinfo(hostname, 80, proto=socket.IPPROTO_TCP)
    except OSError as e:
        return JSONResponse(
            status_code=503,
            content={"ipv4": None, "ipv6": None, "error": f"{type(e).__name__}: {e}"},
        )
    for family, _type, _proto, _canonname, sockaddr in info:
        if family == socket.AF_INET:
            ipv4 = sockaddr[0]
        elif family == socket.AF_INET6:
            ipv6 = sockaddr[0]
    status = 200 if (ipv4 or ipv6) else 503
    return JSONResponse(status_code=status, content={"ipv4": ipv4, "ipv6": ipv6})


def _tcp_connect_any(hosts: list[str]) -> JSONResponse:
    failures = []
    for host in hosts:
        try:
            with socket.create_connection((host, 443), timeout=NETWORK_TIMEOUT_SECONDS):
                return JSONResponse(content={"result": True, "host": host})
        except OSError as e:
            failures.append({"host": host, "reason": f"{type(e).__name__}: {e}"})
    return JSONResponse(status_code=503, content={"result": False, "failures": failures})


@app.get("/ip/4")
def connect_ipv4():
    return _tcp_connect_any(IPV4_HOSTS)


@app.get("/ip/6")
def connect_ipv6():
    return _tcp_connect_any(IPV6_HOSTS)


@app.get("/internet")
def check_internet():
    failures = []
    for url in INTERNET_URLS:
        try:
            with urllib.request.urlopen(url, timeout=NETWORK_TIMEOUT_SECONDS) as resp:
                if 200 <= resp.status < 400:
                    return JSONResponse(content={"result": True, "url": url, "status": resp.status})
                failures.append({"url": url, "reason": f"HTTP {resp.status}"})
        except Exception as e:  # HTTPError, URLError, ssl errors, timeouts
            failures.append({"url": url, "reason": f"{type(e).__name__}: {e}"})
    return JSONResponse(status_code=503, content={"result": False, "failures": failures})
