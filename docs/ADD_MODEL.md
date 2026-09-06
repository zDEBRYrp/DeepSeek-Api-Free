# Добавление режима («модели») — чек-лист

> Режим добавляется только в местах ниже. **Полнотекстовый поиск по коду не нужен.**
> Пример: `deepseek-fast` → DeepThink выкл, поиск выкл, подпись «DeepSeek Fast».

Нужно заранее решить: **имя режима** (то, что клиент шлёт в `model`),
**флаги** `(deep_think, search)`, **подпись** и **описание**.

---

## Обязательно (2 файла)

### 1. `app/model_registry.py` (источник правды)

В каждый из трёх словарей — по одной строке с тем же ключом:

| Словарь | Значение |
|---|---|
| `DEFAULT_TOGGLES` | кортеж флагов `(deep_think, search)` |
| `DISPLAY_NAMES` | короткая подпись |
| `DESCRIPTIONS` | одна строка описания |

`/v1/models` и маппинг в `chat_completions` читают реестр автоматически —
**не дублировать** список в `main.py`.

### 2. `README.md`

- Раздел «Возможности» → список режимов `deepseek-*`: добавить строку.
- Раздел «Использование» → поле `model`: добавить имя.

---

## По желанию

- `.env.example` → `MODE_TOGGLES`: расширить пример **только** если новый режим
  должен быть включён поставкой. Пользовательские оверрайды и так работают
  через эту переменную без правок кода.

## Не трогать

- `app/main.py` (`list_models`, маппинг `model_key` — читают реестр);
- `app/config.py` (парсинг `MODE_TOGGLES` — общий);
- `app/schemas.py`, `login.py`, compose-файлы.

## Проверка

```
[ ] model_registry.py: TOGGLES / DISPLAY_NAMES / DESCRIPTIONS
[ ] README.md: оба места
[ ] .\.venv\Scripts\python.exe -c "from app.model_registry import *; assert is_supported_model('…'); assert resolve_toggles('…')==…"
[ ] GET /v1/models содержит новый id
```
