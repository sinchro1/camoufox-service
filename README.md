# camoufox-service

HTTP-обёртка над [Camoufox](https://github.com/daijro/camoufox) (stealth Firefox) для вызова из n8n или любого HTTP-клиента.

## Как это устроено

Portainer **не собирает образы из Git-стека** (официально не поддерживается), поэтому схема такая:

```
GitHub repo --push--> GitHub Actions --build--> ghcr.io/<login>/camoufox-service:latest
                                                        |
                                                  Portainer stack (image:)
```

Dockerfile и код лежат в GitHub, сборка идёт в Actions, Portainer только тянет готовый образ.

## Состав

| Файл | Назначение |
|---|---|
| `Dockerfile` | сборка образа: Python 3.12 + Xvfb + бинарник Camoufox + FastAPI |
| `app.py` | HTTP-сервис: `/health`, `/scrape` |
| `requirements.txt` | зависимости Python |
| `docker-compose.yml` | стек для Portainer (только `image:`, без `build:`) |
| `.github/workflows/build.yml` | сборка и пуш в GHCR по push в `main` |
| `.dockerignore` | что не тащить в контекст сборки |

## Деплой по шагам

1. Запушьте репозиторий в GitHub. Имя репозитория — **строчными буквами** (GHCR не принимает заглавные в теге).
2. Вкладка **Actions** → workflow `build` соберёт образ. Первая сборка 5–10 минут (~1.5–2 ГБ).
3. GitHub → **Packages** → `camoufox-service` → Package settings:
   - **Public** — Portainer скачает без авторизации;
   - **Private** — добавьте реестр в Portainer: `Registries → Add registry → Custom registry`, URL `ghcr.io`, логин — GitHub-логин, пароль — Personal Access Token с правом `read:packages`.
4. В `docker-compose.yml` замените `your-github-login` на свой логин.
5. Portainer → **Stacks → Add stack → Repository** → укажите репозиторий, Compose path `docker-compose.yml`.
6. Добавьте переменную окружения `API_TOKEN` (случайная строка) и включите **Auto update**, чтобы новые сборки подхватывались автоматически.

## API

### `GET /health`

```json
{ "ok": true }
```

### `POST /scrape`

Заголовок (если задан `API_TOKEN`):

```
Authorization: Bearer <API_TOKEN>
```

Тело:

```json
{
  "url": "https://example.com",
  "wait_ms": 2000,
  "html": true,
  "screenshot": false,
  "proxy": "http://user:pass@host:port"
}
```

| Поле | Тип | По умолчанию | Описание |
|---|---|---|---|
| `url` | string | — | адрес страницы (обязательно) |
| `wait_ms` | int | `2000` | пауза после загрузки, мс |
| `html` | bool | `true` | вернуть `page.content()` |
| `screenshot` | bool | `false` | вернуть PNG в base64 |
| `proxy` | string | `null` | HTTP/SOCKS-прокси |

Ответ:

```json
{
  "ok": true,
  "final_url": "https://example.com/",
  "title": "Example Domain",
  "html": "<!DOCTYPE html>...",
  "screenshot_b64": "iVBORw0KGgo..."
}
```

### `POST /download` — файл по прямой ссылке

Забирает файл через контекст браузера: куки и прокси сохраняются, страница не рендерится.

```json
{
  "url": "https://example.com/report.pdf",
  "timeout_ms": 60000,
  "b64": false
}
```

Ответ по умолчанию — **бинарник** с заголовками `Content-Type` и `Content-Disposition: attachment; filename="..."`. `Content-Type` берётся из ответа сервера, имя файла — из `Content-Disposition`, иначе из пути URL.

### `POST /download-click` — файл после нажатия кнопки

Открывает страницу, кликает по элементу и перехватывает скачивание через `expect_download`.

```json
{
  "url": "https://example.com/exports",
  "selector": "text=Скачать",
  "wait_ms": 2000,
  "timeout_ms": 60000,
  "b64": false
}
```

`selector` — любой селектор Playwright: CSS, `text=Скачать`, `xpath=//button`, `role=button[name="Export"]`.

Если скачивание не началось (неверный селектор, таймаут) — вернётся `504` с текстом причины.

### JSON вместо бинарника

Оба эндпоинта принимают `"b64": true` — тогда ответ будет JSON:

```json
{ "filename": "report.pdf", "content_type": "application/pdf", "size": 84521, "b64": "JVBERi0..." }
```

Для больших файлов лучше бинарник: base64 раздувает объём на 33%.

### Приём файлов в n8n

Нода **HTTP Request** → **Response Format: File**. Файл окажется в бинарном свойстве `data`, дальше — «Convert to File», «Write Binary File», Telegram, почта, S3.

Лимиты на стороне n8n: `N8N_PAYLOAD_SIZE_MAX` (по умолчанию 16 МБ). На стороне сервиса — `MAX_DOWNLOAD_MB` (по умолчанию 200), при превышении вернётся `413`.

## Режимы браузера

Управляется переменной `CAMOUFOX_HEADLESS`:

| Значение | Что запускается |
|---|---|
| `virtual` (по умолчанию) | headed Firefox на приватном Xvfb-дисплее — лучший вариант для антибот-систем |
| `true` | обычный headless |
| `false` | headed, нужен реальный X-сервер |

При `virtual` обязателен `CAMOUFOX_VIRTUAL_DISPLAY_SIZE` (по умолчанию в образе `1920x1080x24`) — иначе дисплей будет 1×1 и это само по себе выдаст автоматизацию.

## Вызов из n8n

Нода **HTTP Request**:

- Method: `POST`
- URL: `http://camoufox-service:8000/scrape` (или `http://<host>:8000/scrape`)
- Authentication: `Generic Credential Type` → `Header Auth`, имя `Authorization`, значение `Bearer <API_TOKEN>`
- Body: JSON, например `{ "url": "https://example.com", "wait_ms": 3000 }`

Дальше `html` разбирается нодой **HTML Extract** либо Code-нодой.

## Замечания

- Каждый вызов поднимает свой экземпляр Firefox. Пять параллельных запросов ≈ 2 ГБ RAM. Для n8n обычно достаточно 1–2.
- Обновление бинарника Camoufox происходит только при пересборке образа: `camoufox fetch` выполняется на этапе сборки. Раз в месяц запускайте workflow вручную (`workflow_dispatch`).
- Открывать порт наружу без `API_TOKEN` нельзя: это готовый анонимный браузер для любого, кто до него дотянется.
