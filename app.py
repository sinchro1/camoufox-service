"""Minimal HTTP wrapper around Camoufox for n8n / any HTTP client.

Endpoints:
    GET  /health   -> {"ok": true}
    POST /scrape   -> {"ok": true, "final_url": ..., "title": ..., "html": [base64]}

Auth: if API_TOKEN env var is set, requests must send
      Authorization: Bearer <API_TOKEN>
"""

import base64
import os

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from camoufox.sync_api import Camoufox

API_TOKEN = os.environ.get("API_TOKEN", "")

_raw_headless = os.environ.get("CAMOUFOX_HEADLESS", "virtual").strip().lower()
HEADLESS = True if _raw_headless == "true" else False if _raw_headless == "false" else _raw_headless

app = FastAPI(title="camoufox-service")


class Job(BaseModel):
    url: str
    wait_ms: int = 2000
    screenshot: bool = False
    html: bool = True
    proxy: str | None = None


def check_auth(authorization: str | None) -> None:
    if API_TOKEN and authorization != f"Bearer {API_TOKEN}":
        raise HTTPException(status_code=401, detail="bad token")


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.post("/scrape")
def scrape(job: Job, authorization: str | None = Header(default=None)) -> dict:
    check_auth(authorization)

    kwargs: dict = {"headless": HEADLESS, "geoip": True}
    if job.proxy:
        kwargs["proxy"] = {"server": job.proxy}

    with Camoufox(**kwargs) as browser:
        page = browser.new_page()
        page.goto(job.url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(job.wait_ms)

        result: dict = {"ok": True, "final_url": page.url, "title": page.title()}
        if job.html:
            result["html"] = page.content()
        if job.screenshot:
            result["screenshot_b64"] = base64.b64encode(
                page.screenshot(full_page=False)
            ).decode()
        return result
