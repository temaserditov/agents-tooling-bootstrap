# Agents tooling bootstrap

Публичный технический пакет дооснащения **уже установленной** команды Hermes.
Он не содержит навыков курса, промптов агентов, пользовательских данных,
памяти автора, ключей, browser profiles и Telegram-сессий.

Для ученика: скачайте файл **INSTALL-AGENTS-TOOLS.md** из Releases и передайте
его Codex вместе с просьбой выполнить задачу. Ссылки версии постоянные.
Никаких дополнительных кнопок платформы и GitHub-авторизации не требуется.

Codex получает пакет с закреплённым SHA-256, проверяет ОС/существующую установку,
подготавливает недостающие зависимости и собственные доступы пользователя,
выполняет bootstrap.py и проверяет результат от каждого агента.
Личные входы, 2FA и разрешение администратора пользователь проходит сам локально.
Это не обещание unattended-установки на любой неизвестной конфигурации.

## Состав

- inventory.py — обнаружение ОС, версии Hermes, команд и профилей, без чтения авторизации;
  на Mac — выбор базы знаний (`mac.kbBackend`); поиск самодельных баз знаний
  (`otherKnowledgeBases`, только имена и счётчики);
- bootstrap.py — автоматическая регистрация полученных скиллов/справок/контекста,
  добавление MCP и первое индексирование без замены других подключений;
- student_tools.py — локальные embeddings, PostgreSQL/pgvector (Ubuntu, Mac на macOS 15) или
  файл SQLite с bge-m3 через ONNX внутри процесса (backend `file`, без служб: Windows x64 и
  Mac Apple Silicon на macOS 14, 26, 27+), гибридный поиск, stdio MCP, индексатор/watch
  (с backend `file` — задача планировщика Windows или LaunchAgent Mac),
  offline Whisper consumer и Bitwarden consumer;
- schema.sql — основа новой собственной базы с vector(1024), не дамп чужой БД;
- settings.example.json (PostgreSQL), settings.windows.example.json (Windows x64) и
  settings.mac-file.example.json (Mac, backend `file`);
- requirements*.lock — закреплённые зависимости с хешами;
- browser-smoke.py — отдельный headless профиль и синтетическая страница: на Hermes 0.23+
  через собственный Chromium Hermes, на старых версиях через agent-browser;
- FOR-CODEX.md — полный порядок, безопасность и критерии приёмки.

Telegram Reader исключён и не переавторизуется. Пакет не повышает тариф,
не загружает чужие скиллы и не публикует студенческий индекс на сервере автора.
Приватные материалы индексируются только локально на устройстве пользователя.

## Проверки

В отдельном Python 3.11–3.13 установите requirements.lock с проверкой хешей.
`AGENTS_TOOLING_SOURCE=. python -B tests/test_tooling_addon.py` проверяет чистые
функции, настоящий stdio MCP, закреплённые модели (несовпадение SHA-256/digest —
жёсткий отказ), ограниченные повторы при 5xx/таймаутах, повторный bootstrap без
изменений (`changed: false`), что секреты и сессии не открываются и не копируются,
и что в публичном пакете нет личных данных. Флаг `--postgres` поднимает отдельный
временный PostgreSQL cluster с pgvector; рабочие БД не использует.
Windows x64 (backend `file`) проверяется workflow `windows-tooling.yml` на GitHub
`windows-2025`: это Windows Server, раннер-администратор, синтетические профили, Python 3.12,
папка ученика с пробелом и кириллицей. Не проверены: Windows 10/11 Home, настоящий профиль
ученика, Windows ARM.
`tests/test_file_backend.py` проверяет backend `file` с подставным эмбеддером; с
`AGENTS_TOOLING_ONNX_MODEL=<папка onnx/ закреплённой модели>` — ещё и с настоящей bge-m3
(русский поиск, MCP stdio).

Порядок для ученика: сначала патч агентов v2026.11.10.3 (или новее) и штатное обновление
(Hermes 0.23.0; 0.23.1 из патча v2026.11.10.4 код принимает так же), затем это дооснащение. Hermes 0.21–0.22 код понимает: в 0.23 нет
agent-browser, браузер — собственный Chromium Hermes (`hermes pm install chromium`),
постоянный профиль — `browser.user_data_dir`.

Закреплено: Systran/faster-whisper-small `536b0662…` (SHA-256 каждого файла),
Ollama bge-m3 digest `79076464…`, ONNX-экспорт BAAI/bge-m3 `5617a9f6…` (SHA-256 каждого
файла). Таблица поддержки ОС — в FOR-CODEX.md, раздел 0; Windows x64 — раздел 10,
Windows ARM64 не поддерживается. Релиз собирается `release/build.py` из
закоммиченного дерева (ZIP + manifest + SHA256SUMS + INSTALL-AGENTS-TOOLS.md).
