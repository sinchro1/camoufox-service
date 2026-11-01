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
from urllib.parse import unquote, urlparse

from fastapi import FastAPI, Header, HTTPException, Response
from pydantic import BaseModel

from camoufox.sync_api import Camoufox, NewContext

API_TOKEN = os.environ.get("API_TOKEN", "")

_raw_headless = os.environ.get("CAMOUFOX_HEADLESS", "virtual").strip().lower()
HEADLESS = True if _raw_headless == "true" else False if _raw_headless == "false" else _raw_headless

# предохранитель: не тянуть в память файл больше лимита
MAX_BYTES = int(os.environ.get("MAX_DOWNLOAD_MB", "200")) * 1024 * 1024

app = FastAPI(title="camoufox-service")


class Job(BaseModel):
    url: str
    wait_ms: int = 2000
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
