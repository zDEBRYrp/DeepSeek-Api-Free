# DeepSeek Chat UI Bridge

Микросервис-обёртка над веб-интерфейсом [chat.deepseek.com](https://chat.deepseek.com),
предоставляющая **OpenAI-совместимый** эндпоинт `/v1/chat/completions`.

Позволяет управлять вашей личной учётной записью DeepSeek из любого
OpenAI-совместимого клиента: поддерживаются режимы **DeepThink**,
**Web-Search**, потоковая генерация (`stream`), `reasoning_content`,
`conversation_id`, история чатов, системный промпт, регенерация,
редактирование сообщений и веб-цитаты.

## Возможности

- OpenAI-совместимый `/v1/chat/completions` (stream + non-stream).
- Режимы DeepSeek через поле `model`:
  - `deepseek-chat` — обычный режим
  - `deepseek-think` — DeepThink (рассуждения в `reasoning_content`)
  - `deepseek-search` — Web-Search
  - `deepseek-think-search` — DeepThink + Search
  - либо явно флагами `deep_think` / `search`.
- `reasoning_content` — цепочка рассуждений DeepThink (в потоке и в обычном режиме).
- `conversation_id` — идентификатор чата DeepSeek для продолжения диалога.
- `citations` — список веб-цитат (при включённом Web-Search).
- История: `GET /v1/chat/list`, `POST /v1/chat/switch`, `GET /v1/chat/history`.
- Регенерация (`regenerate`), редактирование (`edit`), новый чат (`new_chat`).
- Системный промпт (`messages` с `role: system`).
- Остановка генерации: `POST /v1/chat/stop`.
- **Очередь запросов**: одновременно обрабатывается один запрос генерации;
  остальные получают `HTTP 429` (настраивается `REQUEST_QUEUE_TIMEOUT`).
- Сессия шифруется (Fernet) и сохраняется в `data/sessions.sqlite3`;
  восстанавливается автоматически при перезапуске.
- **Мультимодальный `content`**: помимо строки поддерживается массив
  content-частей OpenAI (`[{"type":"text","text":...}, {"type":"image_url",...}]`);
  text-части склеиваются, `image_url`/`image_base64` передаются как вложения.
  ⚠️ Сквозная загрузка **не проверена на живом DeepSeek и может не работать
  вообще** — подробности в §6.
- **Очистка ответов**: код-блоки возвращаются корректными markdown-заборами
  (```lang ... ```), без «шапки» и подписей кнопок «Копировать»/«Скачать».
- **Ошибки в формате OpenAI**: при сбоях отдаётся `{"error":{"message":...,"type":...}}`
  (а не «Type validation failed» у zod-клиентов вроде AI SDK).
- **Function calling (`tools` / `tool_calls`)**: мост переводит запросы DeepSeek в
  стандартные `tool_calls` OpenAI. Клиент (opencode / Kilo Code) сам исполняет
  команды и чтение файлов на вашей машине, получает результат и шлёт обратно
  `tool`-сообщение — мост подсовывает его DeepSeek. Подробнее ниже.
- **Режимы памяти `MEMORY_MODE`** (в `.env`):
  - `server` (по умолчанию) — контекст держит сам DeepSeek (его чат-тред),
    шлём только последнее сообщение.
  - `client` — клиент шлёт всю историю; мост «сплющивает» её в одно сообщение
    и каждый запрос начинает **новый** чат DeepSeek (полностью stateless,
    без утечки прошлых ответов). НУЖЕН для opencode / Kilo Code.
- **Пул профилей** (`profiles.json` / `DS_PROFILES`, пример: `profiles.json.example`):
  round-robin по нескольким аккаунтам DeepSeek, упавший профиль уходит
  в cooldown (`POOL_COOLDOWN_SECONDS`) и возвращается сам; статус — в `/healthz`.
  **Параллельность = размеру пула**: каждый запрос занимает свою сессию
  («много вкладок»); один профиль = строго один запрос за раз, остальные ждут
  `REQUEST_QUEUE_TIMEOUT` секунд и получают 429. Второй профиль = второй
  параллельный запрос (залогинь через `python login.py --profile acc2`).
- **Защита**: `API_KEY` (Bearer для всех `/v1/*`), per-IP rate limit
  (`RATE_LIMIT_PER_MINUTE`, `DISABLE_RATE_LIMIT`), CORS (`ALLOWED_ORIGINS`).
- **Суммаризатор длинного контекста**: при настроенном `SUMMARIZER_API_URL`
  (любой OpenAI-совместимый endpoint) середина истории длиннее
  `SUMMARIZE_THRESHOLD_CHARS` сжимается внешним LLM с цепочкой запасных
  моделей (`SUMMARIZER_MODEL` + `SUMMARIZER_MODEL_FALLBACKS`).
- **CLI входа**: `python login.py [--profile NAME] [--check|--list|--manual]`.
- **Встроенный Web UI** (`GET /ui`, один файл `frontend/index.html`): чат поверх
  нашего же `/v1/chat/completions` (stream), выбор режима из `/v1/models`,
  раскрывающаяся панель мышления (`reasoning_content`), веб-цитаты,
  чекбоксы think/search, поле API-ключа (помнится в localStorage).

## Установка

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
playwright install --with-deps chromium   # либо chromium только: playwright install chromium
cp .env.example .env
```

Отредактируйте `.env` при необходимости (см. описание переменных в `.env.example`).

## Первый запуск (ручной вход)

Браузер должен быть **видимым**, чтобы вы могли войти в аккаунт:

```bash
HEADLESS=false uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Откроется окно браузера — выполните вход вручную. После успешного входа
сессия (куки + localStorage) шифруется и сохраняется. Если аккаунта нет —
окно всё равно появится при следующем запросе к API (сервис дождётся входа).

## Последующие запуски (фоновый режим)

```bash
HEADLESS=true uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Сессия восстанавливается из `data/sessions.sqlite3` автоматически. При
истечении сессии сервис вернёт ошибку входа — перезапустите с `HEADLESS=false`
для повторного ручного входа.

## Docker

```bash
cp .env.example .env
docker compose up --build
```

Для первичного логина в контейнере используйте `HEADLESS=false` и проброс
X11 (см. закомментированные строки в `docker-compose.yml`), либо выполните
первичный вход локально и смонтируйте готовые `./data` и `./pw_profile`.

## Использование

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-think-search",
    "messages": [{"role": "user", "content": "Что такое квантовая запутанность?"}],
    "stream": true
  }'
```

Поля запроса:

- `model` — выбор режима (см. выше). Имеет приоритет над `deep_think`/`search`.
- `deep_think` / `search` (bool) — явное включение режимов.
- `messages` — история диалога (`system`/`user`/`assistant`). Сообщение с
  `role: "system"` вшивается в начало пользовательского запроса (UI DeepSeek
  не имеет отдельного поля system prompt), см. `SYSTEM_PROMPT_TEMPLATE` в `.env`.
- `stream` (bool) — потоковый SSE-ответ.
- `regenerate` (bool) — перегенерировать последний ответ.
- `edit: {"index": N, "content": "..."}` — отредактировать сообщение пользователя
  под индексом `N` (0-based среди `user`-сообщений) и пересчитать диалог.
- `new_chat` (bool) — начать новый чат перед отправкой.
- `conversation_id` — идентификатор чата для продолжения (из предыдущего ответа).
- `images` в `messages[]` — вложения (`image_base64` / `image_path`).
- `tools` — список инструментов в формате OpenAI
  (`[{"type":"function","function":{"name":...,"description":...,"parameters":...}}]`).
  Включает режим function calling (см. ниже).
- `messages[].role` также допускает `"tool"` (результат вызова инструмента) и
  `tool_calls` у сообщений `assistant`.

В ответе (кроме стандартных полей) возвращаются:

- `reasoning_content` — рассуждения DeepThink.
- `conversation_id` — id чата DeepSeek.
- `citations` — веб-цитаты (при Web-Search).
- `usage.ds_token_counter` — счётчик токенов UI DeepSeek (если доступен).

## Tool calling (function calling)

Мост делает DeepSeek совместимым с агентскими клиентами (opencode, Kilo Code,
Cursor и др.), которые сами исполняют инструменты. Реализован **шим** над
чат-UI DeepSeek, который не имеет нативного function calling:

1. Клиент шлёт запрос с `tools` (список инструментов OpenAI-формата).
2. Мост вшивает в промпт инструкцию: DeepSeek должен вернуть вызов инструмента
   одним fenced-JSON блоком:
   ```` ```json
   {"name": "<имя_инструмента>", "arguments": { ... }}
   ``` ````
3. Мост парсит этот блок и возвращает клиенту стандартный OpenAI `tool_calls`
   (поле `choices[].message.tool_calls`, `finish_reason: "tool_calls"`; в потоке —
   в финальном SSE-чанке).
4. Клиент **сам выполняет** команду / читает файл на вашей машине, получает
   результат и шлёт его обратно в `messages` с `role: "tool"`.
5. Мост подсовывает результат DeepSeek, и цикл повторяется, пока модель не
   вернёт финальный ответ.

Пример запроса:

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-chat",
    "tools": [
      {"type":"function","function":{"name":"read_file","description":"Прочитать файл",
       "parameters":{"type":"object","properties":{"path":{"type":"string"}}}}},
      {"type":"function","function":{"name":"run_command","description":"Выполнить команду",
       "parameters":{"type":"object","properties":{"command":{"type":"string"}}}}}
    ],
    "messages": [{"role":"user","content":"Прочитай README.md и скажи первую строку."}]
  }'
```

Пример ответа (DeepSeek решила вызвать инструмент):

```json
{
  "choices": [{
    "index": 0,
    "message": {
      "role": "assistant",
      "content": null,
      "tool_calls": [{
        "id": "call_abc123",
        "type": "function",
        "function": {"name": "read_file", "arguments": "{\"path\": \"README.md\"}"}
      }]
    },
    "finish_reason": "tool_calls"
  }]
}
```

Особенности:

- Работает в обоих режимах `MEMORY_MODE`. Для opencode / Kilo Code ставьте
  `MEMORY_MODE=client` (каждый запрос — новый чат, вся история в `messages`).
- Если DeepSeek отвечает обычным текстом вместо JSON-блока вызова — `tool_calls`
  не возвращаются, ответ идёт как есть (без краха).
- Потоковый режим: `tool_calls` отдаются в финальном SSE-чанке
  (`finish_reason: "tool_calls"`), контент до вызова не дублируется.

## Режимы памяти (MEMORY_MODE)

- `server` (по умолчанию в `.env.example`) — контекст хранит сам DeepSeek.
  Мост отправляет только последнее сообщение в текущий чат-тред; предыдущие
  реплики «помнит» DeepSeek. Хорошо для простых однопоточных клиентов.
- `client` — клиент сам управляет историей и шлёт её целиком в `messages`.
  Мост «сплющивает» историю в одно сообщение и стартует новый чат DeepSeek при
  каждом запросе. Это полностью stateless и исключает утечку текста прошлых
  ответов в новый — именно так ожидают вести себя opencode / Kilo Code.

## API-эндпоинты

| Метод | Путь | Назначение |
|-------|------|------------|
| POST | `/v1/chat/completions` | Основной эндпоинт генерации |
| GET  | `/v1/models` | Список доступных режимов (`deepseek-*`) |
| GET  | `/v1/chat/list` | Список чатов |
| POST | `/v1/chat/switch` | Переключиться на чат (`chat_id`) |
| GET  | `/v1/chat/history?chat_id=` | История сообщений чата |
| POST | `/v1/chat/new` | Новый чат (возвращает `chat_id`) |
| POST | `/v1/chat/stop` | Остановить текущую генерацию |
| GET  | `/healthz` | Проверка живости: `status`, `uptime`, `mode`, `pool` (профили), `version` |
| GET  | `/ui` | Встроенный Web UI (чат) |

## Важно

- Селекторы (`SEL_*` в `.env`) адаптированы под текущий UI `chat.deepseek.com`,
  но DeepSeek периодически меняет вёрстку — при поломках перепроверьте их
  (см. `app/config.py`).
- Все таймауты применяются с небольшой случайной погрешностью, чтобы
  поведение автоматизации не выглядело идентичным.
- Файлы `.env`, `data/`, `pw_profile/`, `cookies.txt` **исключены из git**
  (см. `.gitignore`) — они содержат ваши учётные данные и сессию.

## Отладка

Эндпоинты `/debug/dom`, `/debug/extract`, `/debug/type_test`, `/debug/dump`
полезны для проверки селекторов на живом UI (возвращают DOM-структуру).

## Roadmap / Future

План развития проекта. ✅ — реализовано, 🔜 — в планах, ❌ — отклонено (с причиной).

### 1. Анти-детект и браузерная инфраструктура

- ✅ **patchright** (`ANTIDETECT=patchright`) — работает, остаётся основным.
- ❌ **rebrowser-patches / Camoufox / botright / nodriver / CDP к чужому браузеру**
  Отложено: patchright закрывает задачу, зоопарк драйверов не нужен.
- ✅ **Пул сессий с параллельностью** (`app/session_pool.py`, `profiles.json`)
  Round-robin по профилям + cooldown после сбоев; вкладки shared-context
  (`TABS_PER_PROFILE`) — параллельные генерации в ОДНОМ Chromium;
  in-use трекинг, `busy` в `/healthz`. Глобальный семафор на 1 запрос удалён.
- 🔜 **Docker-образ с VNC/авто-логином**
  Готовый образ с предустановленным профилем и web-VNC для ручного входа
  без локального браузера.
- ✅ **Graceful shutdown** (пул закрывает вкладки раньше владельцев).
  🔜 **Метрики**: `/metrics` (Prometheus), счётчики запросов/429/cooldown.
- ✅ **Healthcheck + ротация логов + profiles-volume в compose**.

### 2. Надёжность и восстановление

- 🔜 **Авто-восстановление сессии при вылете**
  Детект разлогина/cloudflare-challenge и автоматический ре-логин (по
  возможности без окна), с уведомлением.
- 🔜 **Обработка challenge/captcha**
  Авто-ретрай генерации после прохождения проверки, экспоненциальный бэкофф.
- 🔜 **Детект разрыва соединения**
  Корректное завершение SSE-стрима при отключении клиента (`is_disconnected`).
- 🔜 **Тесты**
  pytest-набор: парсинг tool_calls, пул (round-robin/cooldown/in-use),
  лимитер, реестр режимов + e2e на заглушке UI без реального аккаунта.
  Сейчас покрытие — разовые скрипты в `%TEMP%/opencode`, в репозитории их нет.

### 3. Функции чат-моста

- ✅ **DeepThink / Web-Search / режимы через `model`** (реестр `app/model_registry.py`,
  чек-лист добавления — `docs/ADD_MODEL.md`).
- ✅ **`reasoning_content`, `conversation_id`, `citations`**.
- ✅ **Регенерация, редактирование, новый чат, история, системный промпт**.
- ✅ **Суммаризатор длинного контекста** (`SUMMARIZER_API_URL`, цепочка моделей).
- ✅ **Мини Web UI** (`GET /ui`): чат, режимы, панель мышления, цитаты.
  ❌ Полноценный клон навороченной студии (темы/анимации/история диалогов) — не нужен.
- 🔜 **Потоковые цитаты (citations on the fly)**
  Отдавать веб-цитаты по мере их появления в UI, а не только в финальном чанке.
- 🔜 **Продолжить ответ (continue generation)**
  Доотправка недописанного ответа при обрыве.
- 🔜 **Ветвление диалогов (fork/branch)**
  Копирование истории с указанного сообщения в новый чат.
- 🔜 **Экспорт/импорт диалогов**
  Выгрузка чата в Markdown/JSON и загрузка обратно (дешёво: поверх `get_history`).
- ❌ **Артефакты DeepSeek (код/таблицы) как отдельный API**
  Отклонено: код-блоки уже возвращаются корректным markdown (см. «Очистка ответов»),
  отдельное структурированное API никто не просил.
- ❌ **Локализация селекторов под языки UI**
  Отклонено: селекторы по классам/ролям, а не по тексту (текстовые метки — лишь
  запасной путь); чинить по факту поломки, а не впрок.

### 6. Прикрепление файлов и фото (multimodal)

> ⚠️ СТАТУС: **не проверено на живом DeepSeek — может не работать вообще.**
> Код приёма (`ChatMessage.images`, `_save_images_to_tmp`) и прикрепления
> (`set_input_files`) написан, но ни одна загрузка end-to-end не подтверждена:
> неизвестно, видит ли модель прикреплённое, доходит ли файл до чата,
> принимает ли DeepSeek не-картинки. Считайте фичу сломанной, пока проверка
> ниже не пройдена и статус не сменён на ✅.

- ✅ Входящие картинки принимаются в `ChatMessage.images` (`image_path`, `image_base64`,
  `image_url`) и в мультимодальном `content` (список частей с `image_url`); мост сохраняет
  их во временные файлы (`_save_images_to_tmp`) и прикрепляет к полю ввода DeepSeek через
  `set_input_files` (`browser_session._attach_files`). Лимит `MAX_FILES_PER_MESSAGE` (5).
  (Приём — код есть; сквозная работа — НЕ подтверждена, см. предупреждение выше.)
- 🔜 **Проверка: шлёт ли клиент картинки**. Убедиться, что opencode/Kilo Code отправляют
  фото в `/v1/chat/completions` (поле `images` / `content` с `image_url`).
- 🔜 **`image_url` с http/файловым путём**. Сейчас тип `image_url` (не `data:...`) падает в
  `_save_images_to_tmp` — нужно докачивать файл на стороне моста во временный `image_path`.
- 🔜 **Совпадение селектора ввода DeepSeek**. `input[type='file']` может быть скрыт; возможно
  нужна кнопка «скрепка/+». Если прикрепление молча не срабатывает — модель не видит фото.
- 🔜 **Типы файлов**. DeepSeek в чате, вероятно, принимает только изображения; документы/текст
  модель может не «прочитать» — проверить.
- 🔜 **Форматы/размер** (JPG/PNG/WebP) и **несколько файлов за раз** (`set_input_files([...])`).
- 🔜 **Надёжное прикрепление**: явный клик по скрепке, ожидание `input[type=file]`, ретрай.
- 🔜 **Проброс ошибки прикрепления клиенту** (из `_attach_files` в ответ OpenAI как `error`).
- 🔜 **base64 для произвольных типов файлов**, не только png; логирование входящих вложений.

### 4. API и совместимость

- ✅ **Аутентификация на стороне сервиса**
  `API_KEY` (Bearer для всех `/v1/*`; пусто = без проверки).
- ✅ **Per-IP rate limit + CORS + favicon + access-лог** (без slowapi).
- ✅ **Эмуляция tool_calls / function calling**
  Шим над чат-UI DeepSeek: инструкция модели → fenced-JSON вызов → стандартные
  `tool_calls` OpenAI-формата (клиент исполняет команды/чтение файлов сам).
- 🔜 **Квоты по ключам**
  Лимитер сейчас считает по IP; при публичном доступе с общим `API_KEY` нужен
  учёт токенов/запросов по отдельным ключам.
- ❌ **Webhook при завершении длинной генерации**
  Отклонено: клиенты (opencode/Kilo Code) держат SSE-соединение сами, callback
  просили ноль раз — YAGNI.
- ✅ **Реестр режимов** (ручное добавление за 2 минуты по `docs/ADD_MODEL.md`).
  🔜 **Авто-маппинг новых режимов DeepSeek** — только если DeepSeek выкатит
  новые переключатели в UI; пишется по факту, не впрок.

### 5. Удобство разработки

- 🔜 **Конфиг без перезапуска**
  Перезагрузка селекторов/таймаутов из `.env` без перезапуска.
- 🔜 **Расширенная отладка**
  `/debug/dom` с фильтрами, запись видео сессии при ошибках.
- ✅ **CLI-утилита** (частично): `python login.py` уже умеет
  `login | --check | --list | --manual` для профилей и не трогает `.env`.
  🔜 Остаток: `status` (сводка пула из `/healthz`) и `export` (выгрузка чата).
