# API второго этапа

База /api/v1, JSON UTF-8; OpenAPI /openapi.json, Swagger /docs. Реальные ID — UUID.

| Метод и маршрут | Результат |
|---|---|
| GET /health/live | 200, status=alive, без обращения к хранилищам |
| GET /health/ready | 200 или 503; ровно postgres/temporal/chroma |
| GET /api/v1/demo/program | Искусственная карточка, is_demo=true |
| POST /api/v1/programs | name → 201, id/name/current_version_id/created_at; пустая V1 атомарно |
| GET /api/v1/programs | Список программ |
| GET /api/v1/programs/{program_id} | Карточка и текущая версия |
| POST /api/v1/programs/{program_id}/versions | Полный снимок → 201, новая версия |
| GET /api/v1/programs/{program_id}/versions | История, от новых к старым |
| GET /api/v1/programs/{program_id}/versions/{version_id} | Полный снимок версии |
| POST /api/v1/programs/{program_id}/versions/{version_id}/restore | 201, новая версия из старого состояния |
| POST /api/v1/programs/{program_id}/documents | multipart file → 201, метаданные |
| GET /api/v1/programs/{program_id}/documents | Исходники программы |
| GET /api/v1/programs/{program_id}/documents/{document_id}/download | Исходные байты, attachment |

Списки: limit 1–100 (по умолчанию 50), offset от 0.

## Сохранение и восстановление

Тело сохранения: expected_version_id и snapshot. Формат снимка описан в data-model.md.
Пропущенные коллекции становятся пустыми, неизвестные значения остаются null.
Пример: ../examples/snapshot-v1.json. Проверяется структура, не нормативный смысл.

Ответ версии: id, program_id, number, parent_version_id, restored_from_version_id,
origin (initial/save/restore), created_at, snapshot.
Восстановление принимает только expected_version_id: копирует указанный снимок
в новую версию; родитель — текущая версия перед операцией.

Для save и restore обязателен Idempotency-Key: 1–200 символов A–Z/a–z/0–9/._:-.
Одинаковый нормализованный запрос с тем же ключом возвращает прежнюю версию (201),
даже если ожидаемая версия устарела. Другое тело/операция с тем же ключом — 409.
Ключ относится к программе. После проверки повтора проверяется актуальность версии.
Две одновременные записи блокируют строку программы: одна успешна, вторая получает 409.

## Исходники и ошибки

Метаданные документа: id, program_id, original_name, format, media_type, size_bytes,
sha256, created_at, parsing_status=not_parsed. Внутренний ключ/путь не раскрывается.
Один непустой DOCX/XLSX/PDF до 50 МиБ, без разбора. Загрузка не создаёт новую версию.

| Код | Причина |
|---|---|
| 404 | Объект отсутствует в указанной программе |
| 409 | Конфликт текущей версии или ключа |
| 422 | Некорректная структура/ссылка, пустой файл, отсутствующий ключ |
| 413 | Превышение размера |
| 415 | Неподдерживаемое расширение |
| 503 | Недоступное хранилище или отсутствующий оригинал |

Чужой документ в snapshot даёт 422 без изменения текущего состояния;
чужой документ при скачивании — 404. Технические ошибки не выдают подключений/путей.
Нулевой символ U+0000 в строках запроса, включая вложенные значения и ключи JSON,
отклоняется с 422 до записи в PostgreSQL. JSON null и обычный Unicode допустимы.
Остановка общего PostgreSQL может также сделать Temporal недоступным.

## Пока отсутствует

Импорты, jobs/SSE, diff, конфликты/решения, confirm, чат, операции агента, проверки,
экспорт. В дальнейшем долгая обработка вернёт 202 + job_id со связью на Workflow.
