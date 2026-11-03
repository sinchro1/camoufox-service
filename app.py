"""HTTP wrapper around Camoufox for n8n / any HTTP client.

Endpoints:
    GET  /health          -> {"ok": true}
    POST /scrape          -> {"ok": true, "final_url", "title", "html", "screenshot_b64"}
    POST /proxy-check     -> batch reachability check for plain HTTP proxies
    POST /download        -> binary file fetched by URL through the browser context
    POST /download-click  -> binary file captured after clicking an element

Auth: if API_TOKEN env var is set, requests must send
      Authorization: Bearer <API_TOKEN>
"""

import base64
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Literal
from urllib.parse import unquote, urlparse

import requests
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from camoufox.sync_api import Camoufox, NewContext

API_TOKEN = os.environ.get("API_TOKEN", "")

_raw_headless = os.environ.get("CAMOUFOX_HEADLESS", "virtual").strip().lower()
HEADLESS = True if _raw_headless == "true" else False if _raw_headless == "false" else _raw_headless

# предохранитель: не тянуть в память файл больше лимита
MAX_BYTES = int(os.environ.get("MAX_DOWNLOAD_MB", "200")) * 1024 * 1024

# ограничения массовой проверки прокси
MAX_PROXIES_PER_CHECK = 500
MAX_CONCURRENCY = 50

# --- распознавание проверки браузера -----------------------------------------
# Основной признак — структура DOM: она одинакова для всех языков.
# Заголовок локализуется под гео IP прокси, поэтому он только запасной вариант.
CHALLENGE_SELECTORS: tuple[tuple[str, str], ...] = (
    # turnstile первым: наличие виджета означает, что нужен клик/взаимодействие,
    # и это важнее для диагностики, чем сам факт cf-скрипта на странице
    ("turnstile", 'iframe[src*="challenges.cloudflare.com"]'),
    ("stage", "#challenge-stage"),
    ("body_text", "#challenge-body-text"),
    ("error_text", "#challenge-error-text"),
    ("cf_running", "#cf-challenge-running"),
    ("cf_script", 'script[src*="challenge-platform"]'),
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
    goto_timeout_ms: int | None = None  # по умолчанию = ready_timeout_ms
    wait_for: str | None = None
    screenshot: bool = False
    html: bool = True
    proxy: str | None = None
    disable_coop: bool = False
    humanize: bool = False


class ProxyCheckJob(BaseModel):
    proxies: list[str]
    timeout_ms: int = 8000
    concurrency: int = 20
    test_url: str = "http://api.ipify.org"
    fallback_url: str = "http://icanhazip.com"


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


def normalize_proxy(value: str | None) -> str | None:
    """'1.2.3.4:8080' -> 'http://1.2.3.4:8080'. Схема сохраняется, если указана."""
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    return value if "://" in value else f"http://{value}"


def launch_kwargs(
    proxy: str | None,
    disable_coop: bool = False,
    humanize: bool = False,
) -> dict:
    kwargs: dict = {"headless": HEADLESS, "geoip": True}
    if proxy:
        kwargs["proxy"] = {"server": proxy}
    if disable_coop:
        # нужно, чтобы можно было кликнуть по Turnstile в cross-origin iframe
        kwargs["disable_coop"] = True
    if humanize:
        kwargs["humanize"] = True
    return kwargs


def error_response(status_code: int, code: str, detail: str, proxy: str | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "ok": False,
            "error": code,
            "detail": detail[:1000],
            "proxy": proxy,
        },
    )


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


def extract_exit_ip(body: str) -> str | None:
    text = (body or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            for key in ("ip", "origin", "query", "address"):
                value = data.get(key)
                if isinstance(value, str) and value:
                    return value.split(",")[0].strip()[:64]
    except Exception:
        pass
    match = re.search(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text)
    if match:
        return match.group(0)
    match = re.search(r"\b[0-9a-f:]{6,45}\b", text, re.IGNORECASE)
    return match.group(0) if match else None


def classify_proxy_error(exc: Exception) -> str:
    text = str(exc).lower()
    name = type(exc).__name__.lower()
    if "connecttimeout" in name or "connect timeout" in text:
        return "connect_timeout"
    if "readtimeout" in name or "read timed out" in text:
        return "read_timeout"
    if "refused" in text:
        return "refused"
    if "reset" in text:
        return "reset"
    if "resolve" in text or "name or service not known" in text or "nodename nor servname" in text:
        return "dns"
    if "ssl" in text or "certificate" in text:
        return "tls"
    if "timeout" in text:
        return "timeout"
    return "other"


def check_single_proxy(
    raw: str,
    test_url: str,
    timeout_s: float,
    fallback_url: str | None = None,
) -> dict:
    """Проверка прокси обычным HTTP-запросом, без запуска браузера.

    ok        — подтверждён выходной IP, прокси реально проксирует
    reachable — прокси ответил любым HTTP-статусом (жив на транспортном уровне)
    """
    proxy = normalize_proxy(raw)
    result: dict = {
        "proxy": proxy,
        "ok": False,
        "reachable": False,
        "http_status": None,
        "ms": None,
        "exit_ip": None,
        "error_code": None,
        "error": None,
    }
    if not proxy:
        result["error"] = "empty proxy"
        result["error_code"] = "other"
        return result

    urls = [test_url]
    if fallback_url and fallback_url != test_url:
        urls.append(fallback_url)

    started = time.monotonic()
    for url in urls:
        try:
            response = requests.get(
                url,
                proxies={"http": proxy, "https": proxy},
                timeout=timeout_s,
                headers={"User-Agent": "camoufox-service/proxy-check"},
            )
        except Exception as exc:
            result["ms"] = int((time.monotonic() - started) * 1000)
            result["error"] = (str(exc) or type(exc).__name__)[:200]
            result["error_code"] = classify_proxy_error(exc)
            return result

        result["ms"] = int((time.monotonic() - started) * 1000)
        result["http_status"] = response.status_code
        result["reachable"] = True

        if response.status_code == 200:
            ip = extract_exit_ip(response.text)
            if ip:
                result.update(exit_ip=ip, ok=True, error=None, error_code=None)
                return result
            result["error"] = "no ip in response"
            result["error_code"] = "other"
            return result

        # 403/429/503 — это лимит тестового сервиса, а не отказ прокси.
        # Пробуем запасной адрес, прежде чем считать прокси плохим.
        if response.status_code in (403, 429, 503) and url != urls[-1]:
            continue

        result["error"] = f"http {response.status_code}"
        result["error_code"] = "blocked" if response.status_code in (403, 407) else "http_error"
        return result

    return result


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.post("/scrape")
def scrape(job: Job, authorization: str | None = Header(default=None)):
    check_auth(authorization)

    proxy = normalize_proxy(job.proxy)
    goto_timeout = job.goto_timeout_ms or job.ready_timeout_ms

    try:
        with Camoufox(**launch_kwargs(proxy, job.disable_coop, job.humanize)) as browser:
            page = browser.new_page()

            try:
                navigation = page.goto(job.url, wait_until="domcontentloaded", timeout=goto_timeout)
            except Exception as exc:
                text = str(exc).lower()
                # отказ именно прокси, а не сайта: такую попытку надо учитывать
                # как fail_conn, иначе пул деградирует незаметно
                proxy_markers = (
                    "proxy_connection_refused",
                    "err_proxy",
                    "proxy connect",
                    "proxy server",
                    "err_tunnel_connection_failed",
                    "proxy authentication",
                )
                if proxy and any(marker in text for marker in proxy_markers):
                    return error_response(502, "proxy_unreachable", str(exc), proxy)
                is_timeout = "timeout" in text
                return error_response(
                    504 if is_timeout else 502,
                    "timeout" if is_timeout else "navigation_failed",
                    str(exc),
                    proxy,
                )

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
                "http_status": getattr(navigation, "status", None),
                "challenge_cleared": marker is None,
                "detected_by": marker,
                "waited_ms": waited_ms,
                "proxy": proxy,
            }
            if job.html:
                result["html"] = page.content()
            if job.screenshot:
                result["screenshot_b64"] = base64.b64encode(
                    page.screenshot(full_page=False)
                ).decode()
            return result
    except Exception as exc:
        # сюда попадают ошибки запуска браузера и работы через прокси
        return error_response(
            502,
            "proxy_unreachable" if proxy else "launch_failed",
            str(exc),
            proxy,
        )


@app.post("/proxy-check")
def proxy_check(job: ProxyCheckJob, authorization: str | None = Header(default=None)) -> dict:
    check_auth(authorization)

    items = job.proxies[:MAX_PROXIES_PER_CHECK]
    workers = max(1, min(job.concurrency, MAX_CONCURRENCY, len(items) or 1))
    timeout_s = max(1, job.timeout_ms) / 1000

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(
            pool.map(
                lambda raw: check_single_proxy(raw, job.test_url, timeout_s, job.fallback_url),
                items,
            )
        )

    return {
        "ok": True,
        "checked": len(results),
        "alive": sum(1 for item in results if item["ok"]),
        "reachable": sum(1 for item in results if item["reachable"]),
        "results": results,
    }


@app.post("/download")
def download(job: DownloadJob, authorization: str | None = Header(default=None)):
    """Забрать файл по прямой ссылке: куки контекста и прокси сохраняются."""
    check_auth(authorization)

    proxy = normalize_proxy(job.proxy)
    try:
        with Camoufox(**launch_kwargs(proxy)) as browser:
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
    except HTTPException:
        raise
    except Exception as exc:
        return error_response(
            502,
            "proxy_unreachable" if proxy else "launch_failed",
            str(exc),
            proxy,
        )


@app.post("/download-click")
def download_click(job: ClickJob, authorization: str | None = Header(default=None)):
    """Открыть страницу, нажать элемент и перехватить скачивание."""
    check_auth(authorization)

    proxy = normalize_proxy(job.proxy)
    try:
        with Camoufox(**launch_kwargs(proxy)) as browser:
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
    except HTTPException:
        raise
    except Exception as exc:
        return error_response(
            502,
            "proxy_unreachable" if proxy else "launch_failed",
            str(exc),
            proxy,
        )
