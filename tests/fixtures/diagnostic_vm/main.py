"""Diagnostic microVM for PROGRAM message regression tests.

A minimal stand-in for aleph-vm's examples/example_fastapi: just enough
endpoints to prove the CRN's /vm/<hash>/ proxy plumbs paths, query strings,
request bodies, and PROGRAM-message env vars into the guest. Imports only
FastAPI (bundled in the aleph-debian-12-python runtime) and stdlib, so it
runs unmodified in the runtime with no code volume dependencies.
Entrypoint: `main:app`.
"""
import os

from fastapi import FastAPI

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
