"""HTTP wrapper around Camoufox for n8n / any HTTP client.

Endpoints:
    GET  /health          -> {"ok": true}
    POST /scrape          -> {"ok": true, "final_url", "title", "html", "screenshot_b64"}
    POST /download        -> binary file fetched by URL through the browser context
    POST /download-click  -> binary file captured after clicking an element

Auth: if API_TOKEN env var is set, requests must send
      Authorization: Bearer <API_TOKEN>
"""

import base64
import os
import re
import time
from typing import Literal
from urllib.parse import unquote, urlparse

from fastapi import FastAPI, Header, HTTPException, Response
from pydantic import BaseModel

from camoufox.sync_api import Camoufox, NewContext

API_TOKEN = os.environ.get("API_TOKEN", "")

_raw_headless = os.environ.get("CAMOUFOX_HEADLESS", "virtual").strip().lower()
HEADLESS = True if _raw_headless == "true" else False if _raw_headless == "false" else _raw_headless

# предохранитель: не тянуть в память файл больше лимита
MAX_BYTES = int(os.environ.get("MAX_DOWNLOAD_MB", "200")) * 1024 * 1024

# --- распознавание проверки браузера -----------------------------------------
# Основной признак — структура DOM: она одинакова для всех языков.
# Заголовок локализуется под гео IP прокси, поэтому он только запасной вариант.
CHALLENGE_SELECTORS: tuple[tuple[str, str], ...] = (
    ("stage", "#challenge-stage"),
    ("body_text", "#challenge-body-text"),
    ("error_text", "#challenge-error-text"),
    ("cf_running", "#cf-challenge-running"),
    ("cf_script", 'script[src*="challenge-platform"]'),
    ("turnstile", 'iframe[src*="challenges.cloudflare.com"]'),
)

CHALLENGE_TITLE_RE = re.compile(
    r"just a moment|checking your browser|attention required|verify you are human"
    r"|ddos-guard|access denied|security check",
    re.IGNORECASE,
)

app = FastAPI(title="camoufox-service")


class Job(BaseModel):
    url: str
    # smart: ждать, пока снимется челлендж Cloudflare/Turnstile (до ready_timeout_ms)
    # fixed: не проверять челлендж, просто выждать wait_ms
    wait_mode: Literal["smart", "fixed"] = "smart"
    wait_ms: int = 2000
    ready_timeout_ms: int = 60000
    wait_for: str | None = None
    screenshot: bool = False
    html: bool = True
    proxy: str | None = None


class DownloadJob(BaseModel):
    url: str
    timeout_ms: int = 60000
    proxy: str | None = None
    b64: bool = False


class ClickJob(BaseModel):
    url: str
    selector: str
    wait_ms: int = 2000
    timeout_ms: int = 60000
    proxy: str | None = None
    b64: bool = False


def check_auth(authorization: str | None) -> None:
    if API_TOKEN and authorization != f"Bearer {API_TOKEN}":
        raise HTTPException(status_code=401, detail="bad token")


def launch_kwargs(proxy: str | None) -> dict:
    kwargs: dict = {"headless": HEADLESS, "geoip": True}
    if proxy:
        kwargs["proxy"] = {"server": proxy}
    return kwargs


def challenge_marker(page) -> str | None:
    """Возвращает маркер незавершённой проверки браузера, либо None если страница готова."""
    for name, selector in CHALLENGE_SELECTORS:
        try:
            if page.query_selector(selector) is not None:
                return name
        except Exception:
            return "error"  # страница в момент навигации — считаем, что ещё не готово

    try:
        title = page.title() or ""
    except Exception:
        return "error"

    if CHALLENGE_TITLE_RE.search(title):
        return "title"
    return None


def wait_until_ready(page, timeout_ms: int) -> tuple[bool, int]:
    """Ждёт снятия проверки. Возвращает (снялась_ли, сколько_ждали_мс)."""
    started = time.monotonic()
    marker = challenge_marker(page)
    while marker is not None and (time.monotonic() - started) * 1000 < timeout_ms:
        page.wait_for_timeout(500)
        marker = challenge_marker(page)
    return marker is None, int((time.monotonic() - started) * 1000)


def filename_from(headers: dict, url: str) -> str:
    disposition = headers.get("content-disposition", "") or ""
    match = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)\"?", disposition, re.IGNORECASE)
    if match:
        return unquote(match.group(1))
    name = os.path.basename(urlparse(url).path)
    return unquote(name) or "download.bin"


def file_response(data: bytes, filename: str, content_type: str, as_b64: bool):
    if as_b64:
        return {
            "filename": filename,
            "content_type": content_type,
            "size": len(data),
            "b64": base64.b64encode(data).decode(),
        }
    return Response(
        content=data,
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.post("/scrape")
def scrape(job: Job, authorization: str | None = Header(default=None)) -> dict:
    check_auth(authorization)

    with Camoufox(**launch_kwargs(job.proxy)) as browser:
        page = browser.new_page()
        page.goto(job.url, wait_until="domcontentloaded", timeout=job.ready_timeout_ms)

        waited_ms = 0
        if job.wait_mode == "smart":
            _, waited_ms = wait_until_ready(page, job.ready_timeout_ms)
            if job.wait_for:
                try:
                    page.wait_for_selector(job.wait_for, timeout=job.ready_timeout_ms)
                except Exception:
                    pass  # селектор не появился — вернём что есть

        if job.wait_ms:
            page.wait_for_timeout(job.wait_ms)

        marker = challenge_marker(page)
        result: dict = {
            "ok": True,
            "final_url": page.url,
            "title": page.title(),
            "challenge_cleared": marker is None,
            "detected_by": marker,
            "waited_ms": waited_ms,
        }
        if job.html:
            result["html"] = page.content()
        if job.screenshot:
            result["screenshot_b64"] = base64.b64encode(
                page.screenshot(full_page=False)
            ).decode()
        return result


@app.post("/download")
def download(job: DownloadJob, authorization: str | None = Header(default=None)):
    """Забрать файл по прямой ссылке: куки контекста и прокси сохраняются."""
    check_auth(authorization)

    with Camoufox(**launch_kwargs(job.proxy)) as browser:
        context = NewContext(browser)
        response = context.request.get(job.url, timeout=job.timeout_ms)

        if not response.ok:
            raise HTTPException(status_code=502, detail=f"upstream HTTP {response.status}")

        declared = response.headers.get("content-length")
        if declared and int(declared) > MAX_BYTES:
            raise HTTPException(status_code=413, detail=f"file too large: {declared} bytes")

        data = response.body()
        if len(data) > MAX_BYTES:
            raise HTTPException(status_code=413, detail=f"file too large: {len(data)} bytes")

        content_type = (response.headers.get("content-type") or "application/octet-stream").split(";")[0]
        return file_response(
            data,
            filename_from(response.headers, job.url),
            content_type,
            job.b64,
        )


@app.post("/download-click")
def download_click(job: ClickJob, authorization: str | None = Header(default=None)):
    """Открыть страницу, нажать элемент и перехватить скачивание."""
    check_auth(authorization)

    with Camoufox(**launch_kwargs(job.proxy)) as browser:
        context = NewContext(browser, accept_downloads=True)
        page = context.new_page()
        page.goto(job.url, wait_until="domcontentloaded", timeout=job.timeout_ms)
        page.wait_for_timeout(job.wait_ms)

        try:
            with page.expect_download(timeout=job.timeout_ms) as incoming:
                page.click(job.selector, timeout=job.timeout_ms)
            download_event = incoming.value
        except Exception as exc:  # таймаут, неверный селектор, файл не начал качаться
            raise HTTPException(status_code=504, detail=f"download not triggered: {exc}") from exc

        local_path = download_event.path()
        if local_path is None:
            raise HTTPException(status_code=502, detail="download produced no file")

        with open(local_path, "rb") as handle:
            data = handle.read()

        if len(data) > MAX_BYTES:
            raise HTTPException(status_code=413, detail=f"file too large: {len(data)} bytes")

        return file_response(
            data,
            download_event.suggested_filename or "download.bin",
            "application/octet-stream",
            job.b64,
        )
