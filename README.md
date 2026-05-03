# FlowState

FlowState - локальная snapshot-ориентированная VCS с простыми правилами:

- Нет staging area.
- Каждая версия - полный снимок состояния проекта.
- Merge: `v1 + v2 = v3` (новая неизменяемая версия с двумя родителями).
- Local-first + content-addressable storage (SHA-256).

## 0. Quick start (5 минут)

Минимальные требования: Python 3.10+, опционально Node.js 18+ для VS Code расширения.

1. Создать виртуальное окружение и поставить зависимости:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

2. Инициализировать репозиторий и сделать первый snapshot:

```powershell
python cli.py init .
python cli.py save -m "initial"
python cli.py history
```

3. Получить машиночитаемый статус (используется VS Code расширением):

```powershell
python cli.py status --json
python cli.py history --json
python cli.py diff v1 v2 --json
```

4. (Опционально) Streamlit UI:

```powershell
streamlit run app.py
```

5. (Опционально) Сборка VS Code расширения:

```powershell
npm install
npm run compile
```

6. (Опционально) Dev-проверки:

```powershell
pip install -r requirements-dev.txt
ruff check .
pytest
npm run compile
```

Подробнее об архитектуре, командах и UI - в разделах ниже.

## 1. Что делает система

FlowState хранит историю версий в SQLite и контент файлов по хешу SHA-256:

- Метаданные версий в таблице `versions`.
- Контент файлов в таблице `objects` и на диске в `.flowstate/objects/`.
- Дерево каталогов по хешам в `tree_entries`.

Каждый `save` создает **новую** версию. Уже созданные версии не перезаписываются.

## 2. Структура проекта

- `database.py` - инициализация базы и подключение.
- `engine.py` - ядро VCS: snapshot, checkout, merge, history, diff.
- `cli.py` - консольный интерфейс (`flow init/save/merge/...`).
- `app.py` - Streamlit UI (таймлайн, save, merge, diff preview).
- `.flowstate/` - служебное хранилище (создается после init).

## 3. Схема данных (SQLite)

Используются три таблицы:

- `versions` - версии и связи родителей (`parent1_id`, `parent2_id`).
- `objects` - `hash -> content` (BLOB, сжатый zlib).
- `tree_entries` - структура дерева файлов/папок для конкретного `tree_hash`.

Ключевые гарантии:

- Дедупликация контента: одинаковые файлы хранятся один раз (`INSERT OR IGNORE` по hash).
- Невозможность потерять связь между версиями: у merge-версии всегда два родителя.
- Стабильный tree hash: дерево сериализуется детерминированно (отсортированные entries).

## 4. Как работает snapshot

`take_snapshot(message)`:

1. Рекурсивно сканирует рабочую директорию (кроме `.flowstate`).
   - учитывает правила из `.flowignore` и `.flowstateignore` (простые glob-шаблоны, например `*.pyc`, `__pycache__/`, `node_modules/`).
2. Для каждого файла считает SHA-256.
3. Сохраняет содержимое в CAS (`objects` + `.flowstate/objects/`), если такого хеша еще нет.
4. Строит хеши поддеревьев и корневой `root_tree_hash`.
5. Создает новую запись в `versions`:
   - первая версия -> `v1`
   - далее -> `vN+1`, где `N = COUNT(versions)`

## 5. Как работает checkout

`checkout(version_id)`:

1. Читает `root_tree_hash` выбранной версии.
2. Восстанавливает map `path -> object_hash` из `tree_entries`.
3. Полностью заменяет рабочую директорию (кроме `.flowstate`) на выбранный snapshot.

Важно: checkout перезаписывает рабочие файлы.

## 6. Как работает smart merge

`smart_merge(v_a_id, v_b_id)`:

1. Определяет общего предка (lowest common ancestor по графу родителей).
2. Сравнивает `base`, `A`, `B` по каждому пути файла.
3. Применяет трехстороннюю логику:
   - одинаково в A/B -> берем A;
   - изменено только в одной ветке -> берем измененную;
   - изменено в обеих -> конфликт.
4. При конфликте:
   - для текстов: вставляет conflict markers:
     - `<<<<<<< vA`
     - `=======`
     - `>>>>>>> vB`
   - для бинарных: выбирает более новую по id (текущая стратегия fallback).
5. Создает новую merge-версию `vN+1` с `parent1_id=A`, `parent2_id=B`.

## 7. CLI команды

Запуск через Python:

```powershell
python cli.py <command> [args]
```

Основные команды:

- Инициализация:
```powershell
python cli.py init .
```

- Новый snapshot:
```powershell
python cli.py save -m "Init project" --path .
```

- История версий:
```powershell
python cli.py history --path .
```

- Checkout версии:
```powershell
python cli.py checkout v2 --path .
```

- Merge двух версий:
```powershell
python cli.py merge v1 v2 -m "Merge v1 + v2" --path .
```

- Сравнение двух версий:
```powershell
python cli.py diff v1 v2 --path .
```

- Экспорт/импорт хранилища:
```powershell
python cli.py export flowstate-backup.zip --path .
python cli.py import flowstate-backup.zip --path ./restored-project
```

- Запуск watcher (background по умолчанию, каждые 15 минут):
```powershell
python cli.py watch --path .
```

- Запуск watcher в текущем процессе:
```powershell
python cli.py watch --path . --foreground
```

Поддерживаются ссылки на версию как по `id`, так и по `name` (`v1`, `v2`, ...).

### JSON API CLI

Любая команда поддерживает флаг `--json` для машиночитаемого вывода
(используется в том числе VS Code расширением):

```powershell
python cli.py status --json
python cli.py history --json
python cli.py save -m "initial" --json
python cli.py show v2 src/main.py --json
python cli.py diff v1 v2 --json
python cli.py export backup.zip --json
```

При ошибке возвращается `{"ok": false, "error": "..."}` и код возврата `1`.
Дополнительные служебные команды:

- `is-init --json` - проверка, что в каталоге проинициализирован FlowState и БД проходит схема-чек.
- `status --json` - текущая/последняя версия и dirty-флаг.
- `show <version> <file> --json` - содержимое файла из выбранной версии (utf-8 с заменой нечитаемых байтов).
- `diff <from> <to> --json` - списки `added`, `modified`, `removed` между двумя версиями.

## 8. UI (Streamlit)

Запуск:

```powershell
streamlit run app.py
```

Не запускайте UI через `py app.py` или `python app.py` для интерактивной работы - это bare mode.

Что есть в интерфейсе:

- `Timeline` - вертикальный список версий с родителями.
- `Action Bar` - кнопка сохранения новой версии.
- `Merge Tool` - выбор двух версий и создание merge-версии.
- `Difference Preview` - added/modified/removed между версиями.

## 9. Python API (engine.py)

Ключевые публичные методы `FlowStateEngine`:

- `take_snapshot(message: str = "") -> dict`
- `take_snapshot(message: str = "", auto: bool = False, only_if_changed: bool = False) -> dict | None`
- `checkout(version_id: int | str) -> dict`
- `smart_merge(v_a_id: int | str, v_b_id: int | str, message: str = "") -> dict`
- `get_history() -> list[dict]`
- `describe_diff(from_version_id: int | str, to_version_id: int | str) -> dict[str, list[str]]`
- `prune_auto_saves(keep_last: int = 5) -> int`

Минимальный пример:

```python
from engine import FlowStateEngine

engine = FlowStateEngine(".", auto_init=True)
v1 = engine.take_snapshot("initial")
v2 = engine.take_snapshot("update")
v3 = engine.smart_merge(v1["id"], v2["id"], "merge")
print(v3["name"])
```

## 10. Формат хранения на диске

После `init`:

```text
.flowstate/
  flowstate.db
  objects/
    ab/
      cdef...   # zlib-сжатый blob, имя = sha256
  logs/
    flowstate.log   # JSONL audit-log операций
```

При чтении объекта FlowState повторно проверяет SHA-256. Если объект поврежден,
операция завершается ошибкой вместо восстановления неверного содержимого.

## 11. Ограничения текущей версии

- Нет сетевого/удаленного репозитория (только local-first).
- Нет контроля прав доступа/подписей коммитов.
- Нет line-based merge-алгоритма уровня diff3 (используются простые conflict markers).

## 12. Диагностика и типовые проблемы

- Ошибка `Run 'flow init' first`:
  - Выполните `python cli.py init .`

- Warnings Streamlit (`missing ScriptRunContext`):
  - Запускайте UI только через `streamlit run app.py`

- После checkout "пропали" текущие незасейвленные файлы:
  - Это ожидаемо: checkout заменяет рабочее состояние на выбранный snapshot.

## 13. Рекомендуемый рабочий цикл

1. `python cli.py init .`
2. Внести изменения в проект.
3. `python cli.py save -m "описание изменений"`
4. Повторять шаги 2-3.
5. Для слияния: `python cli.py merge vA vB -m "merge message"`
6. Для отката/просмотра: `python cli.py checkout vN`

## 15. Advanced History Management

### clean

```powershell
python cli.py clean --path .
python cli.py clean --all --path .
```

- Default mode: deletes `temp-*` and `auto-*` versions older than 24h.
- `--all`: aggressive cleanup mode.
  - If `squash` root is set, keeps only that root and its descendants.
  - Otherwise removes temp/auto versions.
- Safety: protected `last_temp_save_id` is not deleted until a new snapshot is created after checkout.

### delete

```powershell
python cli.py delete 15 --path .
python cli.py delete v15 --force --path .
python cli.py delete v15 --recursive --path .
```

- Without flags: deletion is blocked when version has children.
- Error text сообщает, что версия является предком другой версии и ее нельзя удалить без `--force` или удаления потомков.
- `--force`: detaches children and deletes target.
- `--recursive`: deletes target plus removable ancestors.
- `gc()` runs automatically after deletion.

### gc

```powershell
python cli.py gc --path .
```

GC removes orphaned objects from DB and `.flowstate/objects/`.

### squash

```powershell
python cli.py squash v10 --path .
```

- Sets `parent1_id=NULL` and `parent2_id=NULL` for selected version.
- Marks version as new root for aggressive cleanup flow.

Typical flow:

```powershell
python cli.py squash v10 --path .
python cli.py clean --all --path .
```

## 16. Temp Snapshot Policy (Updated)

To reduce temp snapshot noise:

- `back` does not create another safety temp snapshot.
- During checkout, if the current dirty tree is identical to the already protected temp snapshot, FlowState reuses it instead of creating a new one.
- FlowState auto-prunes temp snapshots to keep the latest 3 (`temp-*`) while preserving the currently protected recovery snapshot.
- Protection is consumed after `back` and then `clean --all` can remove old temp snapshots.

## 17. Developer quality gates

Для локальной проверки перед коммитом:

```powershell
pip install -r requirements-dev.txt
ruff check .
pytest
npm ci
npm run compile
```

CI описан в `.github/workflows/ci.yml` и выполняет те же основные проверки:
Python lint, Python tests и TypeScript compile.
