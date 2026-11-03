-- ============================================================================
-- camoufox-service — схема БД для пула прокси
-- Версия: 1.2.0  (под фактические типы таблицы proxy_lst)
-- MySQL 5.7+ / MariaDB 10.3+
--
-- ФАКТИЧЕСКАЯ СТРУКТУРА proxy_lst (по DESCRIBE):
--   id      int unsigned  PK auto_increment
--   ip      varchar(21)   UNIQUE
--   ms      varchar(255)  <-- конвертируется в INT (раздел 1)
--   notes   varchar(255)
--   datum   date          default current_date
--
-- ПОРЯДОК ВЫПОЛНЕНИЯ
--   Раздел 0 — осмотреться (обязательно)
--   Раздел 1 — ms: varchar -> INT              (обязательно)
--   Раздел 2 — дедупликация                    (необязательно, см. комментарий)
--   Раздел 3 — добавить колонки
--     ИЛИ
--   Раздел 3b — создать таблицу с нуля         (только если таблицы нет)
--   Раздел 4 — site_proxy_stats
--   Раздел 5 — proxy_log
--   Раздел 6 — проверка
-- ============================================================================


-- ======================================================== 0. ОСМОТРЕТЬСЯ ====

DESCRIBE proxy_lst;

SHOW INDEX FROM proxy_lst;

SELECT COUNT(*) AS total, COUNT(DISTINCT ip) AS distinct_ip FROM proxy_lst;


-- ============================================== 1. ms: varchar(255) -> INT ====
-- Обязательный шаг: пока ms строка, сравнение `ms <= 4000` и сортировка
-- работают лексикографически ("900" > "1500"), то есть неправильно.
--
-- 1a. Чистим всё, что не число — иначе конвертация в строгом режиме упадёт.

SELECT COUNT(*) AS not_numeric
FROM proxy_lst
WHERE ms IS NOT NULL AND ms NOT REGEXP '^[0-9]+$';

UPDATE proxy_lst SET ms = NULL
WHERE ms IS NOT NULL AND ms NOT REGEXP '^[0-9]+$';

-- 1b. Меняем тип. Пустые значения останутся NULL — это нормально.

ALTER TABLE proxy_lst
  MODIFY COLUMN ms INT NULL COMMENT 'задержка при проверке, мс';


-- ================================================== 2. ДУБЛИ (не нужен) ====
-- По ip уже стоит UNIQUE-индекс, поэтому дублей быть не может
-- (сейчас: 167 строк = 167 уникальных ip).
-- Оставлено для случая, когда индекс когда-нибудь снимут или перезальют таблицу.

-- DELETE p1 FROM proxy_lst p1
-- JOIN proxy_lst p2 ON p1.ip = p2.ip AND p1.id > p2.id;


-- Статусы (ослабленные пороги под нестабильный пул):
--   1  ALIVE        — последний запрос успешен; ЛЮБОЙ успех сбрасывает fail_conn в 0
--   9  NO_CONNECT   — 1..4 провала связи подряд, кулдаун 15 минут
--   10 DEAD         — 5 провалов подряд И ни одного успеха за сутки; только тогда не трогаем
--   0  NEW          — ни разу не проверялся
--
-- ================================================= 3. ДОБАВИТЬ КОЛОНКИ =====
-- UNIQUE по ip НЕ добавляем — индекс уже есть под именем `ip`.
--
-- ВНИМАНИЕ: если какая-то колонка уже существует, MySQL 8 не поддерживает
-- ADD COLUMN IF NOT EXISTS — убери её строку и выполни остальные.

ALTER TABLE proxy_lst
  ADD COLUMN status     TINYINT     NOT NULL DEFAULT 0
      COMMENT '0 NEW, 1 ALIVE, 9 NO_CONNECT, 10 DEAD',
  ADD COLUMN fail_conn  SMALLINT    NOT NULL DEFAULT 0
      COMMENT 'подряд неудачных подключений',
  ADD COLUMN ok_count   INT         NOT NULL DEFAULT 0
      COMMENT 'успешных использований всего',
  ADD COLUMN exit_ip    VARCHAR(45) NULL
      COMMENT 'внешний IP при проверке, из Code node n8n',
  ADD COLUMN leaked     TINYINT(1)  NOT NULL DEFAULT 0
      COMMENT 'прозрачный прокси: exit_ip совпал с IP сервера',
  ADD COLUMN checked_at DATETIME    NULL
      COMMENT 'последняя проверка связи',
  ADD COLUMN last_ok_at DATETIME    NULL
      COMMENT 'последний успешный запрос через прокси',
  ADD COLUMN last_error VARCHAR(40) NULL
      COMMENT 'короткий код ошибки, формирует n8n',
  ADD KEY idx_pick (status, ms);


-- ============================================ 3b. СОЗДАТЬ С НУЛЯ (альтернатива)
-- Только если таблицы прокси ещё нет вообще.
-- Не выполняй, если сделал раздел 3.

-- CREATE TABLE IF NOT EXISTS proxy_lst (
--   id         INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
--   ip         VARCHAR(21)  NOT NULL,
--   ms         INT          NULL COMMENT 'задержка при проверке, мс',
--   notes      VARCHAR(255) NULL,
--   datum      DATE         NOT NULL DEFAULT (CURRENT_DATE),
--   status     TINYINT      NOT NULL DEFAULT 0,
--   fail_conn  SMALLINT     NOT NULL DEFAULT 0,
--   ok_count   INT          NOT NULL DEFAULT 0,
--   exit_ip    VARCHAR(45)  NULL,
--   leaked     TINYINT(1)   NOT NULL DEFAULT 0,
--   checked_at DATETIME     NULL,
--   last_ok_at DATETIME     NULL,
--   last_error VARCHAR(40)  NULL,
--   UNIQUE KEY ip (ip),
--   KEY idx_pick (status, ms)
-- ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


-- ================================================== 4. СТАТИСТИКА ПО САЙТУ ====
-- Антибот блокирует на уровне зоны, поэтому «не пустили на neowin.net» —
-- это свойство пары «домен + прокси», а не прокси вообще.
-- proxy_id обязан быть INT UNSIGNED: proxy_lst.id объявлен как int unsigned,
-- иначе внешний ключ не создастся (errno 150).

CREATE TABLE IF NOT EXISTS site_proxy_stats (
  domain         VARCHAR(255) NOT NULL,
  proxy_id       INT UNSIGNED NOT NULL,
  ok_count       INT          NOT NULL DEFAULT 0,
  fail_count     INT          NOT NULL DEFAULT 0 COMMENT 'антибот не пустил на этом домене',
  last_ok_at     DATETIME     NULL,
  last_used_at   DATETIME     NULL,
  cooldown_until DATETIME     NULL COMMENT 'до этого времени не пробовать на домене',
  last_error     VARCHAR(40)  NULL,
  PRIMARY KEY (domain, proxy_id),
  KEY idx_domain (domain, ok_count, last_used_at),
  CONSTRAINT fk_sps_proxy FOREIGN KEY (proxy_id)
      REFERENCES proxy_lst(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


-- ======================================================= 5. ИСТОРИЯ ПОПЫТОК ====
-- Без внешнего ключа намеренно: удаление прокси из пула не должно стирать историю.

CREATE TABLE IF NOT EXISTS proxy_log (
  id          BIGINT AUTO_INCREMENT PRIMARY KEY,
  proxy_id    INT UNSIGNED  NOT NULL,
  domain      VARCHAR(255)  NULL,
  url         VARCHAR(1024) NOT NULL,
  result      ENUM('ok','challenge','proxy_error','target_error','timeout') NOT NULL,
  http_status SMALLINT      NULL,
  waited_ms   INT           NULL COMMENT 'сколько ждали снятия челленджа',
  detected_by VARCHAR(32)   NULL COMMENT 'чем распознан челлендж',
  ms          INT           NULL COMMENT 'задержка прокси в момент запроса',
  created_at  DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_proxy_time  (proxy_id, created_at),
  KEY idx_result_time (result, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


-- ============================================================= 6. ПРОВЕРКА ====

-- Типы после конвертации (ms должен стать int)
DESCRIBE proxy_lst;

-- Сколько прокси в каком состоянии (после раздела 3 все должны быть status=0)
SELECT status, COUNT(*) AS cnt
FROM proxy_lst
GROUP BY status
ORDER BY status;

-- Живые и быстрые, готовые к работе
SELECT COUNT(*) AS alive_fast
FROM proxy_lst
WHERE status = 1 AND ms <= 4000;

-- Структура новых таблиц
SHOW CREATE TABLE site_proxy_stats;
SHOW CREATE TABLE proxy_log;

-- Почему выбывают прокси за сутки (если n8n пишет last_error)
SELECT last_error, COUNT(*) AS cnt
FROM proxy_lst
WHERE checked_at > NOW() - INTERVAL 1 DAY
GROUP BY last_error
ORDER BY cnt DESC;
