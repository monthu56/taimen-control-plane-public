# ADR-0074: Движок процессов в ядре — чистая функция шага, намерения, журнал экземпляра, симуляция и план по хэшу

Статус: Accepted (2026-09-27), фича `process-packages`, задача P002
(TASK-000718). Spec/plan/tasks — `specs/process-packages/` суперпроекта
(FR-003, FR-016…FR-020, FR-024, FR-027, FR-028; конституция ст. II, V, VI);
дизайн — TASK-000715 (редакция 2, ворота плана одобрены владельцем
2026-09-27, отступление по ст. VIII принято), разбиение — TASK-000716.
Контракт (маршруты, тела, права, события) опубликован этим шагом (п.16);
реализация — P004, P006–P009, P013–P015 (таблица п.16); MCP-инструменты
автора — P016 (п.17). Амендмент 2026-09-29 (TASK-000903): план и применение
по хэшу — для всех видов каталога ядра (п.11, после уточнения P015).
Амендмент 2026-09-29 (TASK-000904): привязка объекта каталога к пакету —
`package` в ответах, `?package=`, `POST /packages:record` (Е1–Е5).

Контекст: TAI-ADR-0054 «Пакеты процессов с памятью» (решения Р1–Р20 plan;
виды каталога `Process` и `Calendar`, схема `packages/schema/v1` — P001,
TASK-000717); [ADR-0075](0075-cel-expression-profile.md) (язык выражений);
[ADR-0076](0076-processes-and-memory.md) (процессы и память); ADR-0003
(состояние + события); ADR-0023 (порядок журнала `(tx_id, sequence)`);
ADR-0056 (вызов скиллов); ADR-0061 (исходы approval); ADR-0063 (правила
вывода работы, курсор журнала, личность правила Г1); ADR-0067 (стадия
проверки); ADR-0068 (каталог событий); ADR-0073 (реестр агентов, `agent:<key>`).

## Контекст

Процесс организации нельзя описать данными: нет состояния дела, таймеров от
дат, кворума, отмены с компенсацией, приостановки, и ничего нельзя проверить
без стенда (TAI-ADR-0054, «Контекст»). BPMN-движок `process-runtime` жил
отдельно от ядра со своим состоянием, и его задачи не были задачами ядра.

Владелец решил (2026-09-27): процессы исполняет ядро. Отдельный сервис дал бы
второй источник состояния, а процесс как набор правил без экземпляра не
выражает таймеры, кворум и компенсацию (plan, «Отступления»).

Ограничения:

- исполнение детерминировано: при тех же событиях, времени и ответах решения
  совпадают (FR-017), иначе нет ни тестов без стенда, ни replay;
- каждое решение оставляет след с причиной и автором (FR-018);
- действия процесса выполняются от личности пакета, а не применившего
  (FR-020);
- staging — 2 vCPU и 5,8 ГиБ: без нового процесса-воркера;
- ядро нейтрально к предметной области (ст. II).

## Решение

### 1. Процесс и календарь — объекты ядра с неизменяемыми версиями

Процесс — запись tenant'а с ключом `key` (`^[a-z0-9][a-z0-9_-]*$`, до 63
символов). Тело `POST /api/v1/process-definitions` — объект каталога без
конверта: `{key, spec}`, где `spec` — ровно `spec` файла пакета вида `Process`
(`$defs.processSpec`). Установщик пакетов подставляет переменные установки
`${…}` (например `spec.workspaceId`) и больше ничего не меняет.

- Версия — `spec.version`. Пара `(key, version)` неизменяема; у версии есть
  `definitionHash` — `sha256:<hex>` канонического JSON `spec` (ключи
  отсортированы, без пробелов — та же функция, что у ревизии агента,
  ADR-0073 п.2).
- Повтор той же версии с тем же хэшем — `200` без записи. Та же версия с другим
  содержимым — `409 process_version_conflict`. Версия не больше последней —
  тоже `409 process_version_conflict` (`details.latestVersion`).
- Процесс уровня workspace (`spec.workspaceId`) виден и правится по правам
  на этом workspace, без него — на tenant, как правило (ADR-0063).
- Владелец процесса — `spec.owner`, цепочка назначения (`assignChain`, как у
  `human.assign`; амендмент TAI-ADR-0054 2026-09-27): ему адресуются задачи о
  самом процессе — расхождение с регламентом, ошибки экземпляров. Схема
  каталога его не требует; версия хранит его в `spec` и отдаёт полем `owner`
  `ProcessDefinitionOut`, без владельца проверка даёт предупреждение
  `process_owner_missing`.
- Адрес — `key` (последняя версия) или `key@version`:
  `GET /process-definitions/{ref}`, список версий —
  `GET /process-definitions/{key}/versions`.

Календарь (п.9) устроен так же: `POST /api/v1/calendars {key, spec}`, версия
выдаётся ядром при отличии хэша канонического JSON `spec`.

### 2. Проверка определения: форма по схеме каталога, затем язык

Публикация проходит две ступени (реализация — P006):

1. **Форма** — JSON Schema вида из схемы каталога суперпроекта
   (`$defs.processSpec`). Ядро держит её копию; contract-тест держит копию
   равной суперпроекту (`tests/fixtures/superproject/object.schema.json`).
   Язык описывается одной схемой, и ядро не дублирует её моделью pydantic:
   тело маршрута в OpenAPI — объект, а его форма — схема каталога.
2. **Язык**:
   - типы всех выражений CEL (ADR-0075), включая `memory`, `recall` и
     `context` (ADR-0076);
   - неизвестные поля данных; `input`/`output`/`export` против схемы данных;
   - уникальность и стабильность id элементов; достижимость шагов и стадий,
     тупики;
   - ссылки на таблицы решений, типы задач, скиллы (вход и выход — по схемам
     скилла из каталога), календарь, агента личности (п.14);
   - пробелы и перекрытия таблиц решений.

Найденное — список `problems` в одной форме для всех маршрутов и
MCP-инструментов (`ProcessProblemOut`):

```json
{"code": "unknown_data_field", "severity": "error",
 "path": "/spec/stages/1/steps/0/output/as/decison",
 "file": "processes/tender.yaml", "line": 42,
 "message": "data has no field decison", "hint": "did you mean decision?"}
```

`file` и `line` есть, когда объект пришёл файлом пакета (п.10); у
`POST /process-definitions` они `null`. Хоть одна ошибка —
`422 invalid_process` с `details.problems`; предупреждения (например,
документ `governedBy`, которого нет в памяти, ADR-0076 п.6) не мешают
публикации и возвращаются в `warnings` версии.

### 3. Экземпляр закреплён за версией

Экземпляр — запись `process_instances`: `definition_key`,
`definition_version`, `instance_key`, `workspace_id`, `status`
(`running | suspended | completed | failed | cancelled`), `outcome`, данные
(`data`, JSON по схеме процесса) и состояние движка (стадии, открытые
элементы, стек областей, счётчики повторов).

- **Старт** — по `start.on` (событие журнала или наблюдение) и `start.key`
  (CEL от события). Уникальный индекс `(tenant_id, definition_key,
  instance_key)` даёт ровно один экземпляр на ключ (FR-013). Событие старта
  с ключом существующего экземпляра становится входом этого экземпляра и
  событием `process.correlated`, а не вторым экземпляром.
- **Явный старт** (TAI-ADR-0055, решение владельца 2026-09-28: цели —
  процессами) — `POST /api/v1/process-instances {process, key, data?,
  workspaceId?}` с правом `processes.operate` (на workspace экземпляра):
  экземпляр без внешнего события, ключ и начальные данные заданы (`data`
  проверяется по схеме данных процесса — иначе `422 invalid_process_data`;
  `start.set` не исполняется). Повтор с тем же ключом — `409
  process_instance_exists` с `details.instanceId` существующего экземпляра,
  не второй экземпляр. `process.started` несёт `triggerType: command` и
  `triggerEventId: null`. Так заводятся постоянные цели — процессы-сверки
  без `complete`, чья веха достигается и снимается вслед за данными
  (`process.milestone_lost`, п.13). Задачам экземпляра `goalId` не
  выставляется: цель выводится из ядра.
- **Версия** — последняя опубликованная на момент старта; экземпляр остаётся
  на ней до конца или до миграции (п.11, FR-019).
- Workspace экземпляра — workspace процесса; задачи и approvals экземпляра
  заводятся в нём.

### 4. Чистая функция шага

Модуль домена `domain/process_engine.py`:

```python
def step(definition, state, input) -> tuple[state, list[Decision], list[Intent]]
```

- Без ввода-вывода: ни базы, ни HTTP, ни часов, ни случайности. Время движка —
  время входа (`input.at`): время события журнала или `due_at` сработавшего
  таймера. Идентификаторы, которые движок порождает (таймер, намерение,
  recall), выводятся из `(instance_id, seq, index)` детерминированно (UUIDv5).
- `Decision` — запись журнала экземпляра (п.5): переход, вход и выход
  стадии, веха, установка таймера, голос, компенсация, ошибка и её
  обработчик, с причиной (`reason`: какое условие, какая строка таблицы,
  какой вход).
- `Intent` — что должно случиться вне состояния экземпляра (п.6). Движок не
  знает, как намерение исполняется.
- Ошибки — RFC 7807 (`type`, `status`, `detail`); поднимаются до ближайшего
  `try`, иначе экземпляр `failed` и событие `process.failed`.

Живой прогон, тесты пакета и replay зовут одну и ту же функцию (FR-025): у
них разный только исполнитель намерений.

### 5. Входы и журнал экземпляра

Вход движка — одно из:

| Вход | Откуда |
|---|---|
| событие старта или `correlate` | событие журнала или наблюдение (`observation.recorded`), совпавшее с `on` и ключом |
| завершение, отмена задачи шага | `task.completed` / `task.updated` задачи с внешней ссылкой `process/<instance>/<element>` (ADR-0047) |
| решение согласования | `approval.approved`, `approval.rejected`, `approval.cancelled` approvals, заведённых экземпляром |
| результат скилла | `skill.invocation_succeeded` / `skill.invocation_failed` вызова, заведённого экземпляром |
| срабатывание таймера | строка `process_timers`, у которой наступил `due_at` |
| ответ памяти | ответ `recall` или его таймаут (ADR-0076 п.4) |
| команда оператора | `:suspend`, `:resume`, `:cancel` |

**Журнал экземпляра** — таблица `process_instance_events` (только
добавление): `seq` (по экземпляру), `at` (время движка), `kind` входа,
нормализованный вход (`event_id` источника, тип, нужные движку поля), решения и
намерения шага, `actor_id` (кто стоит за входом). Это журнал решений FR-018 —
`GET /process-instances/{id}/journal`. Replay и объяснение экземпляра читают
только его: вход записан целиком, поэтому повтор не зависит ни от журнала
ядра, ни от памяти, ни от текущих версий каталога. Уникальность
`(instance_id, source_ref)` делает повторную доставку того же входа пустой.

**Подача входов.** Воркер читает журнал ядра своим курсором `processes` в
`event_consumer_cursors` — тот же механизм, что у правил (ADR-0063): порядок
`(tx_id, sequence)`, стабильный горизонт, пакет по tenant'у в одной
транзакции под `FOR UPDATE SKIP LOCKED`, backoff при ошибке. Цикл
`process_events()` сопоставляет событие экземплярам (по внешней ссылке задачи,
id approval и вызова скилла, по `start`/`correlate`) и для каждого делает шаг
в транзакции пакета. Таймеры — отдельный цикл `process_timers()` (п.8). Новый
процесс в контейнере не появляется: оба цикла живут в `control-plane-worker`.

### 6. Намерения и их исполнение

| Намерение | Чем исполняется |
|---|---|
| `create_task`, `cancel_task` | команды задач: тип, форма, назначение, срок, внешняя ссылка `process/<instance>/<element>`, профиль контекста (ADR-0076 п.5) |
| `request_approvals`, `close_approvals` | команды approvals: по approval на согласующего, `excludedPrincipals` (п.7) |
| `invoke_skill` | долговечный `skill_invocation` (ADR-0056); агент — задачей на `agent:<key>` |
| `recall` | запрос к памяти вне транзакции (ADR-0076 п.4) |
| `remember` | наблюдение ядра (ADR-0076 п.5) |
| `set_timer`, `cancel_timer` | строки `process_timers` |
| `emit_event` | событие `process.*` журнала ядра |
| `start_child` | экземпляр вложенного процесса с родителем |
| `complete` | статус и исход экземпляра, `process.completed` |

Намерения исполняет прикладной слой (`commands/process_instances.py`)
имеющимися командами ядра **в той же транзакции**, что сохраняет `state'` и
запись журнала: либо записано всё, либо ничего. Обычные проверки команд
(права, eligibility, схемы) проходят от личности процесса (п.14). Отказ
команды — ошибка шага (`type: intent_failed`, `detail` — код команды); её
ловит `try` процесса, иначе экземпляр `failed`. Только `recall` уходит
наружу, и его ответ — новый вход (ADR-0076 п.4); вызов скилла ждёт своего
события, как у правил.

### 7. Люди и согласования

- **Шаг `human`** — задача ядра: `taskType`, форма (JSON Schema и uischema у
  типа задачи, шаг может сузить), `assign` — цепочка кандидатов (`principal`,
  `role`, `agent:<key>` по ADR-0073 А1, CEL), `due`, эскалации. Результат —
  `customFields` задачи при завершении, проверенные по форме; в данные — через
  `output.as`. Эскалации — таймеры шага; уровень даёт `process.escalated`,
  уведомления — обычные правила уведомлений по этому событию.
- **Шаг `approve`** — по approval ядра на каждого согласующего; `mode`
  `parallel | sequential`, кворум `all | any | {atLeast} | {percent}`,
  досрочное решение. Кворум считает чистая функция
  `domain/approval_quorum.py`; лишние approvals закрываются (`:cancel`),
  уход согласующего (`approval.cancelled`) пересчитывает кворум.
  Правила функции (P008): `tally(approvers, votes, quorum, mode,
  earlyDecision)` отвечает исходом `pending | approved | rejected`, причиной
  и списком `active` — согласующие, чей approval должен быть открыт сейчас;
  движок запрашивает недостающие и закрывает открытые вне списка (после
  решения список пуст). Нужно одобрений: `all` — все оставшиеся, `any` — 1,
  `{atLeast: n}` — n, `{percent: p}` — ⌈p·N/100⌉, не меньше 1, где N —
  оставшиеся (доля считается точно: `66.7` — это 667/10, а не двоичная дробь). Ушедший
  согласующий не считается: `all` перестаёт его ждать, `percent` берёт долю
  от меньшего числа, `atLeast`, недостижимый оставшимися, — отказ
  (`quorum_unreachable`); ушли все — отказ (`no_approvers`: пустое «все» не
  согласие). С досрочным решением шаг решается, как только кворум набран или
  недостижим; без него — когда проголосовали все оставшиеся (`all_voted`).
  `sequential` держит открытым один approval — первого в порядке, кто ещё не
  голосовал; `parallel` — всех.
- **Разделение обязанностей проверяет ядро, а не движок.** У approval
  появляется поле `excludedPrincipals` (запрос `POST /approvals`, ответ
  `ApprovalOut`, колонка `approvals.excluded_principals`). Решение principal'а
  из списка команда решения отвергает `403 separation_of_duties_violation`
  при любом пути — движок, консоль, канал, MCP. Движок заполняет список из
  `separationOfDuties` шага. Реализовано в P008: колонка — JSON-массив id
  principal'ов (`[]` — никто не исключён, как у approval до ревизии);
  запрос принимает до 100 principal'ов tenant'а (повторы схлопываются,
  неизвестный — `404`, исключённый `assignedPrincipalId` — `422
  invalid_approval`: решать было бы некому). Проверка стоит в общей проверке
  eligibility команды решения, поэтому действует и на `:reject`, и на отмену
  чужого gate (`:cancel` требует полномочий решающего), и при любой роли
  исключённого. Список «Важное» (ADR-0071) исключённому такой approval не
  показывает. `approval.requested` v3 несёт `excludedPrincipals` (только
  добавление).

### 8. Таймеры

- Строка `process_timers(instance_id, element, due_at, expr_hash,
  reads, state, remaining_seconds, provisional)`; `state` —
  `pending | frozen | fired | cancelled`.
- `due` задаётся длительностью, датой из данных или сдвигом по календарю
  (CEL, ADR-0075). `reads` — поля данных, которые выражение читает (разбор
  CEL).
- Цикл `process_timers()` берёт наступившие строки под `FOR UPDATE SKIP
  LOCKED` и делает вход «таймер» в той же транзакции; событие
  `process.timer_fired`. Сработавший таймер не откатывается.
- **Пересчёт.** Шаг, изменивший данные, пересчитывает `due` несработавших
  таймеров, чьи `reads` пересекаются с изменёнными полями, — событие
  `process.timer_rescheduled` со старым и новым временем. Процесс может
  реагировать на перенос срока (`on: data.changed`).
- **Приостановка** замораживает таймеры: хранится остаток, `due_at` — `null`.
  При возобновлении `due_at` = время возобновления + остаток. Таймер от даты в
  данных остатка не хранит и пересчитывается от данных.

### 9. Календарь

Вид каталога `Calendar` (`$defs.calendarSpec`): часовой пояс, выходные дни
недели, по годам — праздники, перенесённые рабочие дни, сокращённые дни,
`provisional` и источник. Таблица `calendars (key, version, calendar_hash,
spec)`; версия — по хэшу, как у процесса (п.1). Чтение
`GET /calendars[/{key}[@version]]` — любому аутентифицированному: календарь
не секрет и не персональные данные. Запись — `calendars.write`.

- `cal.*` (ADR-0075) на предварительном годе помечает результат; таймер и
  экземпляр показывают `provisional: true` («срок предварительный»).
- Каждое вычисление записывает в журнал экземпляра версию календаря, на
  которой оно сделано: replay берёт её, а не текущую.
- Новая версия календаря пересчитывает несработавшие таймеры экземпляров,
  чьи выражения зовут `cal.*` с этим ключом (`process.timer_rescheduled`,
  `cause: calendar_changed`) — так подтверждённый год снимает пометку.

Уточнение (P004, 2026-09-27). Арифметика — `domain/calendar.py`, чистые
функции над версией календаря:

- день опубликованного года рабочий, если он в `workdays`; иначе — если он
  не в `holidays` и не выходной день недели (`weekend`, по умолчанию `[6, 7]`);
  день вне опубликованных годов знает только выходные дни недели;
- `addWorkdays(ts, n)` — `n`-й рабочий день после дня `ts` (при `n < 0` — до
  него), сам день `ts` не считается, `n = 0` — тот же момент; время суток
  сохраняется в часовом поясе календаря;
- `workdaysBetween(a, b)` — число рабочих дней в `(a, b]`, при `b < a` —
  со знаком минус: для рабочего дня `b` после `a`
  `addWorkdays(a, workdaysBetween(a, b)) = b`;
- «предварительно» — если вычисление посмотрело хоть один день года с
  `provisional: true` или года, которого в календаре нет;
- шаг ограничен 50 годами просмотра: календарь без рабочих дней даёт ошибку,
  а не бесконечный цикл.

Хэш версии считается от `spec`, где годы упорядочены по номеру, даты и
`weekend` — по возрастанию: пакет, переставивший строки, новой версии не
даёт. Сверх схемы ядро проверяет часовой пояс (IANA), один элемент на год,
принадлежность дат своему году и что день не одновременно праздник и
рабочий — иначе `422 invalid_calendar` с `details.code` и `details.path`.
Строки `calendars` неизменяемы (триггер). Объект пакета вида `Calendar`
(`{apiVersion, kind: Calendar, key, spec}`, конверт уже сверен со схемой
каталога проверкой пакета) ставит
`api/v1/calendars.install_calendar` — та же публикация под `calendars.write`
применяющего; `packages:apply` (P015) публикует календарь той же командой
`publish_calendar`.

### 10. Симуляция в трёх режимах — в ядре

Все три режима исполняет ядро тем же `step` (FR-024, FR-025).

**Тело — пакет файлами.** `PackageSource {files: [{path, content}]}`: пути
внутри пакета (без `..`, пустых частей и абсолютных путей), содержимое — текст
YAML 1.2 (TAI-ADR-0054 п.11: `on`, `yes`, `no` — строки). Ядро само разбирает
файлы, поэтому находка называет файл и строку. До 1000 файлов, до 1 000 000
символов в файле.

1. **Тест по сценарию — `POST /packages:test`** (`packages.test`).
   - Ядро собирает определения пакета в памяти, не записывая их в каталог, и
     прогоняет тесты `tests/*.test.yaml` (контракт —
     `packages/schema/v1/test.schema.json`; фильтр `tests` — пути файлов).
   - Намерения исполняет **песочница**: задачи, approvals и таймеры — объекты
     в памяти; скиллы, агенты и `recall` — заглушки теста (`mocks`), чей выход
     проверяется по схеме скилла из каталога (не по схеме — провал теста);
     время виртуальное (`advance: P3D`).
   - Транзакция базы — только чтение (`SET TRANSACTION READ ONLY`): каталог,
     роли, календари. Исходящих вызовов нет: у песочницы нет клиентов HTTP,
     памяти и хранилища содержимого. `noSideEffects` в тесте — счётчик
     записей песочницы.
   - Ответ `PackageTestOut`: `status` (`passed | failed | invalid`),
     `problems` (проверка п.2 по всем объектам пакета), результат по тестам с
     провалами по шагам, покрытие по процессам — элементы, переходы, строки
     таблиц, обработчики ошибок, с перечнем непройденного.
   - `?checkOnly=true` — только проверка п.2, без тестов. Её зовёт
     `cp_packages check`, когда ядро доступно.
2. **Replay — `POST /process-definitions/{key}:replay`** (`packages.test`).
   Тело — кандидат `spec`. Ядро берёт журналы экземпляров (`instanceIds` или
   последние `limit` ≤ 200 текущей версии, только из workspace'ов, где у
   вызывающего есть `processes.read`), подаёт их входы кандидату и сравнивает
   решения и намерения с записанными. Ответ — расхождения по экземплярам:
   первая запись журнала, где пути расходятся, элемент, записанное и
   полученное. Ответы `recall` берутся из журнала (ADR-0076 п.4). Ничего не
   пишется.
3. **Пробный прогон на стенде** — тот же `packages:test` со сценарием
   `given.fromInstance`: начальное состояние копируется из живого экземпляра
   (нужно `processes.read` на его workspace). Записи — как у теста: никаких.

### 11. План и применение по хэшу

**`POST /packages:plan`** (`packages.plan`) — ответ `PackagePlanOut`:

- `changes` — структурный diff по объектам и полям: `create | update | rename
  | retire | unchanged`, у поля — `before`, `after` и владелец (`package` или
  `console`: поле правил человек после последнего применения; такое поле не
  перетирается без флага, TAI-ADR-0044) и `applies`;
- `processes` — у изменённого процесса поведенческий diff (replay п.10 на
  `replayLimit` экземплярах) и судьба открытых экземпляров по версиям: `pin`,
  `migrate` или `unaffected`, `migrationRequired`;
- `regulationCoverage` — покрытие разделов регламентов элементами по
  `governedBy` (ADR-0076 п.6);
- `problems` — проверка п.2;
- `catalogEtag` — `sha256` канонического списка `(kind, key, версия или
  хэш)` объектов, которые пакет затрагивает, и отметок владения их полей;
- `planHash` — `sha256` канонического JSON `{package: хэш файлов,
  catalogEtag, changes, processes}`.

**`POST /packages:apply`** `{package, planHash}` применяет ровно этот план
(FR-028). Ядро строит план заново из тех же файлов и сравнивает: другой
`catalogEtag` или `planHash` — `409 plan_stale` (стенд изменился после
показа). Применение — одна транзакция; каждое изменение проходит право своего
вида (`processes.write`, `calendars.write`, `task_types.manage`…), право
`packages.plan` нужно на сам маршрут.

- **Переименования** объектов — `package.yaml → renames: [{kind, from, to}]`
  (как `moved` в Terraform): план переносит объект, а не удаляет и создаёт.
- **Миграции экземпляров** — `migrations: [{from, to, policy, map}]` процесса.
  `pin` — экземпляры дорабатывают на старой версии; `migrate` — состояние
  переносится по карте `старый элемент → новый`, событие `process.migrated` и
  запись журнала у каждого экземпляра. Элемент, на котором стоят открытые
  экземпляры, исчез без покрывающей миграции — ошибка плана
  `migration_required`; `apply` такого плана отказывает `422
  migration_required`.

### 12. Права

| Право | Ресурс | Что даёт |
|---|---|---|
| `processes.read` | workspace процесса (без него — tenant) | определения, экземпляры, журнал |
| `processes.write` | workspace процесса | публикация версии процесса |
| `processes.operate` | workspace процесса | `:suspend`, `:resume`, `:cancel` экземпляра |
| `packages.test` | tenant | `packages:test`, `:replay` |
| `packages.plan` | tenant | `packages:plan`, `packages:apply` (плюс права видов) |
| `calendars.write` | tenant | публикация календаря |

Автор процесса и оператор дела — разные роли: остановить или отменить чужое
дело не следует из права описать процесс. Тест и план ничего не пишут и
потому отделены от применения. Имена — в `Permission` и `authz/catalog.yaml`.

### 13. События

`entityType` — `process_instance` (id экземпляра), у публикации —
`process_definition`, у календаря — `calendar`. Общие поля экземпляра:
`instanceId, definitionKey, version, instanceKey`.

| Тип | Когда | Payload сверх общих полей |
|---|---|---|
| `process.definition_published` | новая версия процесса | `key, version, definitionHash, previousVersion, workspaceId, identityAgent, elements` |
| `process.started` | старт экземпляра | `triggerEventId, triggerType, memory` |
| `process.correlated` | событие попало в существующий экземпляр | `triggerEventId, triggerType, changedFields` |
| `process.data_changed` | изменились данные | `changedFields, element, memory` |
| `process.stage_entered`, `process.stage_exited` | вход, выход стадии | `stage` |
| `process.milestone_reached` | веха | `milestone, stage` |
| `process.milestone_lost` | достигнутая веха перестала выполняться (TAI-ADR-0055) | `milestone, stage` |
| `process.timer_fired` | таймер сработал | `timerId, element, dueAt` |
| `process.timer_rescheduled` | срок сдвинулся (п.8, п.9) | `timerId, element, previousDueAt, dueAt, provisional, cause, changedFields` |
| `process.escalated` | уровень эскалации | `element, level, action, taskId, to` |
| `process.suspended`, `process.resumed` | приостановка, возобновление | `cause, reason` / `cause` |
| `process.compensated` | компенсации выполнены | `scope, steps` |
| `process.recall_completed`, `process.recall_timed_out` | ответ памяти или его отсутствие (ADR-0076 п.4) | `step, recallId, asOf, nodeCount, edgeCount, truncated, resultHash` / `step, recallId, reason` |
| `process.migrated` | экземпляр перенесён (п.11) | `fromVersion, map, policy` |
| `process.completed` | исход | `outcome, memory` |
| `process.cancelled` | отмена оператором | `reason, compensated` |
| `process.failed` | ошибка без обработчика | `error, element` |
| `calendar.published` | новая версия календаря | `key, version, calendarHash, previousVersion, years, provisionalYears` |

`memory` — вычисленная проекция дела (ADR-0076 п.2). Решения движка целиком
живут в журнале экземпляра (п.5); событие журнала ядра говорит остальной
платформе, что с делом случилось. Каталог (`domain/event_catalog.py`,
`docs/events/`) публикует схемы этим шагом.

### 14. Личность процесса

`spec.identity: {agent: <key>}` — ключ описания агента (CP-ADR-0073), как у
правила (ADR-0063 Г1): при публикации агент есть и не выведен из оборота
(иначе `422 unknown_agent`), `identity.kind` — `agent` или `service`, права
агента ⊆ права публикующего (`403 permission_escalation`). Процесс без
личности не публикуется — `422 process_identity_required` (FR-020): схема
каталога поле не требует, это проверка языка. Все намерения экземпляра
исполняются полномочиями principal'а агента; он автор задач, approvals,
наблюдений и событий `process.*` (`actorId`). Агент без principal или
выведенный из оборота — ошибка шага `credential_inactive`, экземпляр ждёт
её обработчика, как правило ждёт связки.

### 15. Нейтральность

В `domain/process_*`, `domain/cel_profile.py` и схеме вида `Process` нет
понятий предметных областей; страж-тест ищет в них слова доменов (список в
тесте, P009). Процесс оплаты счёта — второй потребитель того же языка.

### 16. Контракт до реализации

Маршруты объявлены в `api/v1/processes.py`, `calendars.py`, `packages.py` и
опубликованы в OpenAPI. До своего шага каждый проверяет право и отвечает
`501 not_implemented` с `details: {adr: CP-ADR-0074, implementedBy:
"process-packages <шаг>"}`:

| Маршрут | Шаг |
|---|---|
| `POST/GET /calendars`, `GET /calendars/{ref}` | P004 (реализован) |
| `POST/GET /process-definitions`, `GET …/{ref}`, `GET …/{key}/versions` | P006 (реализован) |
| `POST /approvals` с непустым `excludedPrincipals` | P008 (реализован) |
| `GET /process-instances[/{id}[/journal]]`, `:suspend`, `:resume`, `:cancel`, `POST /process-instances` | P009 (реализован) |
| `POST /packages:test` | P013 (реализован) |
| `POST /process-definitions/{key}:replay` | P014 (реализован) |
| `POST /packages:plan`, `POST /packages:apply` | P015 (реализован) |

Шаг заменяет тело обработчика и не меняет подпись. Таблицы
(`process_definitions`, `process_instances`, `process_timers`,
`process_instance_events`, `calendars`) вводит ревизия alembic фичи
`b8e3f1c6d2a9`, колонку `approvals.excluded_principals` — следующая за ней
`e3b7c1d9a4f2` (P008); существующие таблицы, кроме approvals, не меняются.

Уточнение (P006, 2026-09-27). Проверка определения —
`domain/process_definition.py`, таблицы решений — `domain/decision_table.py`,
публикация и чтение — `application/commands/process_definitions.py`:

- **Схема вида** — `domain/process_spec.schema.json`: `$defs.processSpec`
  схемы каталога со всеми `$defs`, до которых он дотягивается
  (`process_schema_from_catalog()`); тест держит её равной закреплённой
  копии схемы каталога. `spec.workspaceId` сверяется отдельно: в каталоге это
  переменная установки `${…}`, ядро получает id workspace; неподставленная
  переменная — `unresolved_install_variable`. Нарушение схемы — находка
  `schema_violation` с указателем на самое глубокое место, а не `400` маршрута:
  тело маршрута — объект.
- **Хэш.** `spec` нормализуется (строки NFC, целые числа, записанные дробью, —
  целыми) и хэшируется канонически, как ревизия агента, но с двумя отличиями:
  дробные числа разрешены (кворум `percent`, числа таблиц решений), глубина —
  до 64 уровней, строка — до 20 000 символов. Порядок ключей версии не даёт.
- **Личность** проверяется в два шага: отсутствие (`process_identity_required`)
  и неизвестный или выведенный агент (`unknown_agent`) — находки проверки,
  поэтому их видит и проверка пакета; затем, для прошедшей проверку версии,
  вид агента и права — как у правила: `422 invalid_process` для вида не
  `agent`/`service`, `403 permission_escalation`. Поэтому
  `process_identity_required` и `unknown_agent` п.14 приходят не кодом ошибки,
  а находками в `details.problems` ответа `422 invalid_process`.
- **Находки** (`severity`): `schema_violation`, `invalid_document`,
  `unresolved_install_variable`, `invalid_data_schema`, `unresolved_data_ref`,
  `invalid_form_schema`; выражения — коды ADR-0075 (`expression_syntax_error`,
  `expression_type_error`, `expression_too_complex`), в том числе «ожидается
  bool / string / timestamp или duration / list» по месту выражения; запись в
  данные — `unknown_data_field`, `data_type_mismatch`; вход скилла —
  `unknown_skill_input`, `skill_input_type_mismatch`, `skill_input_missing`;
  ссылки — `unknown_skill`, `unknown_task_type`, `unknown_agent`,
  `unknown_calendar`, `unknown_event_type`, `unknown_decision_table`,
  `unknown_table_input`, `unknown_compensation_target`,
  `memory_case_undeclared`, `invalid_error_binding`, `invalid_escalation`,
  `invalid_governed_by`; id — `duplicate_element_id`, `element_kind_changed`,
  `invalid_migration`, `unknown_element`; достижимость — `unreachable_step`,
  `unreachable_stage`, `dead_end`; таблицы — `invalid_table_cell`,
  `duplicate_table_input|output`, `unknown_table_output`,
  `table_output_missing`, `table_output_type_mismatch`, `table_overlap`.
  Предупреждения: `process_owner_missing`, `element_removed`,
  `unwritten_data_field`, `unreachable_milestone`, `unused_decision_table`,
  `nothing_to_compensate`, `duplicate_governed_by`, `unknown_process`
  (вложенный процесс ещё не опубликован), `unknown_artifact_type`,
  `unknown_skill` у `retrospective` без явного скилла, `table_gap`,
  `table_rule_unreachable`, `table_check_truncated`; проверка пакета в ядре
  добавляет `governed_by_unknown_document` и `governed_by_unchecked`
  (CP-ADR-0076 п.7).
- **Типы переменных по месту.** `event` — payload события из каталога событий
  для триггера `event:` (`start`, `correlate`, `onEvent`, `listen.any[].on`
  и их блоков), иначе `map(string, dyn)`; `step.result` в `output.as` и
  `export.as` — выход скилла по его схеме, форма шага `human` (иначе
  `fieldSchema` типа задачи), выходы таблицы у `decide` (у `collect` —
  `{items: [выходы строки]}`), `{nodes, edges, truncated}` у `recall`;
  `task.customFields` — `fieldSchema` типа задачи шага. Сверх профиля
  выражения видят `milestone` (`map(string, bool)`, вехи стадий), имя
  ошибки `try.catch[].as` в её обработчиках и `compensated` в блоке
  `onCompensate` (компенсируемый шаг, типизирован по его выходу; уточнение
  P027) — это привязки окружения (`bindings`), не новые переменные профиля.
- **Достижимость.** Шаг после безусловного `complete` или `raise` того же
  блока недостижим. Сторож, который читает только поля данных, которые ничто
  не пишет (`start.set`, `correlate[].set`, `set`, `output.as`, `export.as`),
  или постоянно ложен, никогда не станет истинным: у `exit` — тупик
  (`dead_end`), у `entry` — стадия недостижима, у вехи — предупреждение.
  Стадия достижима без `entry` или если её `entry` читает событие, экземпляр,
  записываемые данные или состояние другой достижимой стадии; взаимно
  ждущие стадии недостижимы обе.
- **Стабильность id.** Id элементов (стадии, шаги, вехи, таймеры, ветви,
  таблицы) уникальны в процессе. Против последней опубликованной версии:
  id не меняет вид (`element_kind_changed`), исчезнувший элемент без карты
  `migrations` из этой версии — предупреждение (экземпляры остаются на своей
  версии; обязательность миграции решает план, п.11). Карта миграции
  называет элементы своих версий, `to` не больше публикуемой версии.
- **Таблицы решений.** Ячейка — `-`, литерал, список `a,b`, диапазон
  `[a..b)` (открытый конец пустой: `[10..)`) или сравнение `<`, `<=`, `>`,
  `>=`; диапазоны — у `number`, `date` (целые дни), `timestamp`. Тип входа без
  `type` — по типу его выражения, иначе по ячейкам. Перекрытие строк у
  `unique` — ошибка; строка `first`, которую покрывают предыдущие, —
  предупреждение; пробел у `first` и `unique` — предупреждение с примером
  входа (вход может быть ограничен раньше по процессу), у `collect` пробела
  нет — пустой список и есть ответ. Поиск идёт по ячейкам, которые различают
  условия строк, с бюджетом 50 000 шагов; сверх — `table_check_truncated`.
  Там же — вычисление таблицы для движка (`evaluate`): `first` без совпадения
  и `unique` без ровно одного — `decision_no_match`, `decision_ambiguous`.
- **Версии и чтение.** Версия процесса workspace требует `processes.write` на
  новом workspace и на workspace последней версии. `GET …/{key}/versions`
  отдаёт `ProcessVersionOut` (без `spec`). `?governedBy=` ищет документ среди
  `governedBy` процесса, стадий, шагов, таблиц и их строк последней версии
  (колонка `governed_by`, индекс GIN). В `elements` события
  `process.definition_published` — стадии, шаги, вехи и таблицы; таймеры и
  ветви `fork` — нет. Объект пакета вида `Process` ставит
  `api/v1/processes.install_process` — та же публикация; `packages:apply`
  (P015) зовёт `publish_process_definition` напрямую.

Уточнение (P007, 2026-09-27). Чистая функция шага —
`domain/process_engine.py`:

- **Определение для движка** — `Definition.build(key, spec, catalog)`:
  проверка P006 (`check_process`), при ошибках — `DefinitionError`; движок
  исполняет только прошедшие проверку версии. Выражения компилируются один
  раз и типизированы так же, как их типизировала проверка (`CheckedProcess`
  отдаёт `programs` по JSON-указателю и разобранные таблицы `tables`). Блоки
  адресуются ключами из id элементов (`stage:<id>/steps`, `step:<id>/try`,
  `branch:<id>`, `timer:<id>`…), поэтому позиция потока называет элементы, а
  не места в файле. Прикладному слою — `start_key(definition, event)` и
  `correlation_keys(definition, event)` для поиска экземпляра по ключу.
- **Вход** — `Input(kind, at, body, actor_id, calendars)`; виды: `start`,
  `event` (событие или наблюдение: `observation` и `source` рядом с полями
  события), `task`, `approval`, `skill`, `child` (вложенный процесс
  закончился), `recall`, `timer`, `intent_failed`, `calendar` (новая версия
  календаря), `command` (`suspend`, `resume`, `cancel`,
  `start_discretionary`). Ответы адресуются `activityId` — детерминированным
  id ожидания, который несёт намерение; ответ на то, чего уже не ждут, —
  решение `ignored` (`stale`). `calendars` — версии календарей, на которых
  вычисляется вход (их пишет журнал, replay подаёт те же). У `approval` —
  `total`: сколько approvals активности открыто или решено (не отменено);
  кворум считается от него.
- **Состояние** — JSON (`jsonb`), от порядка ключей не зависит: данные,
  стадии и вехи, потоки со стеком кадров (последовательность с позицией,
  открытый `try`, `fork`, идущая компенсация), ожидания (`activities`),
  таймеры, завершённые шаги с `onCompensate`, отложенные входы, `seq`.
  Id, которые делает движок, — UUIDv5 от `(instance, seq, index)`.
- **Намерения** — как в п.6, плюс `reassign_task` (эскалация `reassign`) и
  `cancel_child` (область отмены закрыла вложенный процесс); `create_task`
  несёт форму, назначение (выражения вычислены: `agent:<key>`,
  `role:<slug>`, иначе principal), срок, план эскалаций, профиль `context` с
  вычисленными якорями (`{case: true}` — ключ дела) и внешнюю ссылку;
  `request_approvals` — `excludedPrincipals` из `separationOfDuties`;
  каждое событие `process.*` — намерение `emit_event` с общими полями
  экземпляра; `complete` — статус и исход (`completed`, `failed`,
  `cancelled`).
- **Кейс.** Стадия без `entry` входит при старте, с `entry` — когда сторож
  истинен. Стадия с `exit` выходит, когда он истинен, и завершает свою
  открытую работу (отмена задач, approvals, таймеров); без `exit` — когда
  закончились её потоки. `repeatable` стадия с `entry` входит снова на
  следующем входе после выхода, не на том же. Веха достигается один раз, пока
  стадия активна. Необязательная работа запускается командой
  `start_discretionary`. Экземпляр завершается шагом `complete` или сам с
  исходом `completed`, когда все стадии завершены и потоков нет.
- **Блоки.** `listen`: первое подошедшее событие по порядку `any`;
  `step.result` — `{option, event}`, `event` в следующих шагах потока —
  пришедшее событие. Таймаут `listen` и `recall` без `onTimeout` ведёт поток
  дальше без результата (`output` не применяется). `fork` `compete`
  завершается первой закончившейся ветвью, остальные закрываются. `retry`:
  `limit` повторов, пауза `delay`, при `exponential` — удвоение, потолок
  `maxDelay`; затем `catch`. `recall` без `timeout` ждёт 10 минут
  (`DEFAULT_RECALL_TIMEOUT`, ADR-0076 п.4: не бесконечно). Результат задачи
  проверяется по `form` шага (`form_invalid`).
- **Ошибки движка** (RFC 7807, `type`): коды выражений ADR-0075
  (`expression_error`, `expression_cost_exceeded`), `decision_no_match`,
  `decision_ambiguous`, `form_invalid`, `task_cancelled`, `timeout` (408,
  `call.timeout`), `intent_failed`, `child_failed`, код ошибки скилла,
  `step_limit_exceeded` (больше 10 000 действий на один вход). Ошибка
  сторожа стадии или вехи не имеет потока и сразу переводит экземпляр в
  `failed`; ошибка вычисления проекции памяти не останавливает процесс —
  поле проекции пустое, решение `projection_incomplete`.
- **Приостановка.** Таймеры замораживаются (п.8); ответы на работу стадий
  откладываются и подаются после возобновления по порядку; события
  (`correlate`, `onEvent`) исполняются, поэтому `resume` из `onEvent`
  работает.
- **Отмена оператором**: открытая работа закрывается, компенсации
  сделанных шагов идут в обратном порядке, затем `cancelled`. Ошибка
  компенсации (здесь или в шаге `compensate`) — `failed` с
  `attention: compensation_failed`, а не отмена.
- **Разбор дела** (`retrospective`, ADR-0076 п.8): после `completed` —
  `invoke_skill` (вход — по контракту скилла, журнал прикладывает
  исполнитель, `attach`; уточнение P027 и ADR-0076 п.6), затем задача
  `taskType` с предложенными уроками во входе; результат задачи —
  `customFields.lessons: [{key, text, appliesTo: [{kind, key}], evidence,
  decision: confirm | edit | reject, editedText}]`; `remember` — только
  `confirm` и `edit`, узел `lesson` со связями `learned_from` (дело) и
  `applies_to`.
- **Кворум** до P008 считает сам движок (`all`, `any`, `atLeast`,
  `percent`, досрочно при `earlyDecision`); P008 выносит расчёт в
  `domain/approval_quorum.py`, форма входа `approval` не меняется.

Уточнение (P009, 2026-09-27). Экземпляры, таймеры, воркер, API —
`application/commands/process_instances.py`, циклы `process_events` и
`process_timers` воркера, маршруты `/process-instances`:

- **Таблицы** — в той же ревизии фичи `b8e3f1c6d2a9`, что `calendars` и
  `process_definitions`: `process_instances` (уникальность `(tenant_id,
  definition_key, instance_key)`; `state` — состояние движка, `data` — его
  копия для чтения; `refs` — маршрутизация фактов к ожиданиям, п.5),
  `process_timers` (`id` — id таймера движка; `timer_kind` вместо `expr_hash`
  п.8: рецепт срока живёт в состоянии экземпляра; частичный индекс по
  `due_at` ожидающих), `process_instance_events` (первичный ключ `(instance_id,
  seq)`, уникальность `(instance_id, source_ref)`, только добавление —
  триггер). Колонку `approvals.excluded_principals` вводит P008 своей
  ревизией после этой.
- **Шаг** (`take`): вход берётся один раз (`source_ref`: `event:<id>`,
  `timer:<id>`, `command:<uuid>`, `start`); `step` движка; намерения
  исполняются командами ядра от личности процесса (п.14), каждое в
  savepoint; запись журнала (вход целиком, решения, намерения с полем
  `executed` — что из намерения вышло: `{ok, taskId | approvalIds |
  invocationId | instanceId}` или `{ok: false, code}`) и новое состояние —
  в одной транзакции. `executed` — след исполнения, не решение движка:
  replay (P014) его не сравнивает.
- **Отказ команды.** Отказ намерения, открывающего работу (`create_task`,
  `request_approvals`, `invoke_skill`, `start_child`), — следующий вход
  `intent_failed` (`detail` — код отказа) в той же транзакции; не больше 20
  таких входов на один вход. Отказ закрыть работу (`cancel_task` задачи под
  чужим claim, `close_approvals`, `reassign_task`, `cancel_child`) только
  записывается: движок её уже не ждёт.
- **Личность.** Контекст — principal и IAM-привязка агента `spec.identity`
  на момент шага (та же функция, что у правила, ADR-0063 Г1), с проверкой, что
  привязка активна. Нет личности — каждое намерение, которому она нужна,
  отказывает `credential_inactive`. `actorId` событий `process.*` — principal
  агента, `correlationId` всего, что пишет экземпляр, — `process:<id>`.
- **Задача шага** — `create_task` в workspace экземпляра: тип `taskType`
  (у `call.agent` — системный тип), заголовок, описание со входом шага, срок,
  исполнитель — первое разрешимое звено цепочки `assign` (principal,
  `agent:<key>`; `role` — требование роли без исполнителя), `origin: {kind:
  process, ref: process/<instance>/<element>}`, `goalId` не выставляется.
  Внешняя ссылка ADR-0047 — `external_references(system control-plane, type
  process_step, id process/<instance>/<element>)` с `metadata {instanceId,
  activityId}`; повтор элемента (повторная стадия, `retry`) переносит ссылку
  на новую задачу. Отмена — переход в первый статус категории
  `terminal_cancelled`, доступный из текущего, от личности процесса.
- **Согласования** — по approval на согласующего (`assignedPrincipalId` или
  `requiredRoleId` роли по slug — workspace экземпляра прежде tenant'а), без
  задачи, в workspace экземпляра. `sequential` просит следующего после
  голоса, пока ожидание открыто. `total` входа `approval` — число
  согласующих минус отменённые не самим экземпляром. Непустой
  `excludedPrincipals` до P008 — отказ `not_implemented` (п.7: запрет не
  пропадает молча), то есть `intent_failed`.
- **Скилл** — `invoke_skill` с ключом идемпотентности
  `process:<instance>:<activity>`. **Вложенный процесс** — явный старт
  последней версии с ключом `<parent>/<activity>` и родителем; его
  `process.completed | failed | cancelled` — вход `child` родителя.
  **`remember`** — наблюдение ядра от личности процесса в той же
  транзакции, **`recall`** — строка очереди `process_recalls`, которую
  воркер исполняет после транзакции шага (ADR-0076 п.4–5, P010).
- **Подача входов** (`process_events`): курсор `processes` создаётся при
  публикации первой версии процесса tenant'а (как у правил — в настоящем).
  Событие журнала — вход экземпляров, чьи `refs` называют его сущность
  (`task.completed`, `task.updated` в категорию `terminal_cancelled`,
  `approval.*`, конец вызова скилла, конец вложенного экземпляра), затем
  `calendar.published` — вход `calendar` экземплярам процессов, которые
  зовут этот календарь, затем `start`/`correlate` последних версий всех
  процессов (событие раньше первой версии процесса и событие чужого
  workspace процесса не доходят). Событие, которое экземпляр записал сам
  (`correlationId = process:<id>`), к нему не возвращается. Отказ
  (определение, которое больше не проходит проверку) записывается в лог и
  пропускается; прочая ошибка откатывает пакет tenant'а, курсор ждёт с
  backoff. `onEvent` и `listen` получают события, которые дошли до
  экземпляра по `correlate`.
- **Таймеры** (`process_timers`): наступившие строки по `due_at`; строка
  экземпляра берётся `FOR UPDATE SKIP LOCKED` (занятый экземпляр ждёт
  следующего цикла), таймер перечитывается; время входа — `due_at`, но не
  раньше часов экземпляра. Взятый движком таймер — `fired`, что бы тот ни
  решил (отложенный при приостановке срабатывает из состояния).
- **Время** события — `occurred_at`, команды — момент команды; время входа
  не меньше часов экземпляра (`state.clock`).
- **Маршруты.** `GET /process-instances` — новые первыми, фильтры п.16,
  видимость по `processes.read` на workspace. `ProcessInstanceOut` —
  стадии, открытые элементы (элемент, вид шага, с какого момента, задача и
  approvals ожидания), ожидающие и замороженные таймеры. `GET …/journal` —
  записи `ProcessJournalEntryOut` по шагам: вход (`kind: input`,
  `eventId`, `actorId`), каждое решение (вид — по решению: `stage`,
  `milestone`, `timer` вместе с эскалацией, `vote`, `recall`,
  `compensation`, `migration`, `error`, прочее — `transition`) и каждое
  намерение (`intent`); курсор — `(seq, номер записи)`. `:suspend`,
  `:resume`, `:cancel` — `409 invalid_process_instance_state`, если статус
  не тот (приостановить можно `running`, возобновить — `suspended`,
  отменить — не закрытый и не отменяемый); `:cancel {compensate: false}` —
  без компенсаций. `actorId` входа-команды — оператор.
- **Вехи** (TAI-ADR-0055): веха следует своему сторожу — достигается, когда
  он истинен, и снимается (`process.milestone_lost`, решение
  `milestone_lost`), когда перестаёт быть истинным, пока стадия активна;
  одна веха меняется не больше раза на вход, поэтому сторож, читающий свою
  же веху, не раскачивается. Это заменяет «веха достигается один раз» из
  уточнения P007.
- **Нейтральность** (п.15): `tests/unit/test_process_neutrality.py` ищет
  слова доменов (закупки, счета и оплата, соседние) в `domain/process_*`,
  `domain/cel_profile.py` и `process_spec.schema.json`.

Уточнение (P013, 2026-09-27). Тесты пакета — `domain/package_source.py`
(разбор файлов), `domain/process_sandbox.py` (песочница и покрытие),
`application/commands/package_test.py` (транзакция, каталог, проверка),
маршрут `POST /packages:test`:

- **Файлы пакета.** YAML 1.2 (`on`, `yes`, `no` — строки) с картой «JSON
  pointer → строка»: находка без своей строки берёт строку ближайшего
  родителя. Конверт `{apiVersion, kind, key, spec}`: ядро проверяет, что
  `apiVersion` называет версию `v1` формата каталога, а само имя каталога —
  дело `cp_packages check`. `package.yaml` — объект `Package`; объекты
  каталога — прочие `*.yaml` вне `tests/`, `schemas/`, `.layout/`; тесты —
  `tests/*.test.yaml`, сверенные со схемой `test.schema.json` (копия ядра —
  `domain/package_test.schema.json`, тест держит её равной закреплённой).
  `data: {$ref: <файл>}` процесса ядро раскрывает само (путь от файла
  процесса, не выходя из пакета; JSON или YAML), как `cp_packages`. Находки
  разбора: `invalid_yaml`, `invalid_document`, `unknown_kind`,
  `duplicate_object`, `unresolved_data_ref`, `invalid_test`,
  `unknown_test_process`, `unknown_test` (фильтр `tests` называет не тест),
  `test_version_mismatch`.
- **Определения в памяти.** Каждый процесс пакета проверяется (п.2) против
  каталога tenant'а, поверх которого лежат объекты самого пакета: его
  `TaskType` (`fieldSchema`), `Skill` (`inputSchema`/`outputSchema` или
  `contract.inputs`/`outputs`), `Agent`, `ArtifactType`, `Calendar`,
  `Process` известны его процессам до применения. Прошлая версия для
  стабильности id — последняя опубликованная ниже версии пакета.
  `workspaceId: ${…}` (переменная установки, `cp_packages test` её не
  подставляет) в тесте — workspace запроса или его отсутствие. `Calendar`
  проверяется как `POST /calendars` (`invalid_calendar` и коды п.9). Прочие
  виды здесь не сверяются по форме — это дело `cp_packages check` и плана.
- **`governedBy`** — единственный исходящий вызов маршрута: после закрытия
  транзакции ядро спрашивает память о документах (ADR-0076 п.7),
  предупреждения `governed_by_unknown_document` / `governed_by_unchecked`
  идут в `problems` в обоих режимах. Песочница клиентов не имеет: страж-тест
  проверяет, что `domain/process_sandbox.py` импортирует только домен.
- **Только чтение.** Первая команда транзакции — `SET TRANSACTION READ ONLY`;
  на сессию на время прогона вешается счётчик записей: оператор `INSERT`,
  `UPDATE`, `DELETE`; сырой SQL (`text(...)`), чьё первое ключевое слово —
  `INSERT`, `UPDATE`, `DELETE`, `MERGE`, `COPY`, `TRUNCATE`, или `WITH`, в
  теле которого есть такой оператор (кроме блокировки `FOR [NO KEY] UPDATE`);
  объект, который flush вставил бы, изменил или удалил. Чтение — в том числе
  сырое (`text("SELECT …")`, `WITH RECURSIVE … SELECT`, например предки
  рабочего пространства для `governedBy`) — не запись. Тесты идут внутри
  транзакции; `expect: {noSideEffects: true}` — этот счётчик равен нулю.
  Интеграционный тест сверяет все таблицы базы построчно до и после прогона.
- **Песочница.** Тот же `process_engine.step`, состояние между входами
  проходит через JSON, как `jsonb`. Намерения:
  `create_task` — задача песочницы, исполнитель — первое известное звено
  цепочки (principal теста, `agent:<key>` активного или пакетного агента,
  `role:<slug>` роли каталога, пакета или `given.principals`), иначе
  `intent_failed` (`unknown_role`, `unknown_agent`); `request_approvals` —
  approval на согласующего (`sequential` — следующий после голоса);
  `invoke_skill` — скилл каталога или пакета, вход по его `inputSchema`
  (иначе `intent_failed invalid_skill_inputs`), ответ — заглушка
  `mocks.skills["name@version"]`: `output` сверяется с `outputSchema` —
  **не по схеме — провал теста**, `error` — ответ `failed` с `{code: type,
  status, message: detail}`, `timeout` — ответа нет; `recall` —
  `mocks.recall` (`process_replay.mock_recall`, ADR-0076: ответ не в форме
  ответа памяти — провал теста, `timeout` и `error` — таймаут шага с
  причиной `timeout` или `error.type`, без заглушки — ответа нет, шаг ждёт
  своего таймаута); `call.agent` — `mocks.agents[key]` завершает задачу агента;
  `remember` и события `process.*` — записи для `expect`; `start_child` —
  вложенный экземпляр в той же песочнице (процесс пакета или последняя
  версия каталога), его исход — вход `child` родителя. Вызов без заглушки
  остаётся без ответа, как скилл, который ещё не ответил. Ответ заглушки
  приходит следующим входом, после текущего. Выбор ответа: подходящие по
  `step` и `when` (CEL над `input` вызова) — по порядку вызовов, после
  последнего повторяется последний.
- **Шаги теста.** `emit` — событие (`observation.recorded` с `observation`
  и `source` или тип `event`) идёт как в живом цикле: старт или повтор
  старта процесса теста, затем `correlate` открытых экземпляров;
  `advance` — сдвиг на длительность фиксированной длины или `until:<id или
  элемент таймера>`: ожидающие таймеры срабатывают по порядку `dueAt`, каждый
  в свой момент; `complete` — открытая задача шага (`by` — исполнитель или
  держатель роли по `given.principals`; `output` сверяется с `fieldSchema`
  типа задачи, форма шага — движком, `form_invalid`; `cancel: true` — отмена);
  `approve` — ожидающий approval шага: исключённый principal —
  `separation_of_duties_violation`, не согласующий — `not_eligible`
  (`expectRefused` ждёт именно этот код, неожиданный отказ останавливает
  тест); `expect` — стадии (`open`, `completed`, `not_started`, `skipped` —
  не начатая у закрытого экземпляра), вехи, задачи (`assignee` — исполнитель
  или держатель его роли), таймеры, данные (путь `a.b` или `/a/b`), события
  с прошлого `expect`, `memory.recalled` / `remembered` (частичное
  совпадение), `status`, `outcome`, `error` (тип ошибки), `noSideEffects`.
  Невыполнимый шаг останавливает тест; несбывшееся ожидание — провал шага,
  тест идёт дальше. Время без `given.clock` — `2026-01-05T09:00:00Z`, чтобы
  тест давал один ответ. `given.data` — явный старт с ключом `test`;
  `given.calendar` подменяет календарь процесса; `given.stage` движок не
  умеет (провал теста), `given.fromInstance` — пробный прогон P014.
  Ошибка движка — тест `error`.
- **Покрытие** — по всем тестам процесса вместе, у каждого счётчика —
  `missing`: элементы — стадии (вход), шаги (исполнение; `do` и `try` — когда
  исполнился шаг внутри), вехи, таймеры стадий и процесса; переходы —
  `<стадия>:entry`, `<стадия>:exit`, `<шаг>:when` / `<шаг>:skip`,
  `<шаг>:any/<i>` и `<шаг>:timeout` у `listen`, `<шаг>:answered` /
  `<шаг>:timeout` у `recall`, `<шаг>:approved` / `<шаг>:rejected`, ветви
  `fork`, `correlate/<i>`, `onEvent/<i>`; строки таблиц — `<таблица>/<строка>`;
  обработчики — `<шаг>/catch/<i>`, `<шаг>/retry`, `<шаг>/onTimeout`,
  `<шаг>/onCompensate`, `<шаг>/escalations/<i>`, `<шаг>/onDue`.
  `coverage.minimum` теста — порог доли элементов процесса этим тестом.
- **Ответ** — `200` всегда, когда пакет удалось принять телом: `status
  invalid` (есть находка-ошибка, тесты не идут), `failed`, `passed`;
  `checkOnly` — только проверка, `tests` и `coverage` пусты. `workspaceId`
  запроса требует ещё `processes.read` на него; его роли читаются вместе с
  ролями tenant'а.
- **PyYAML** становится зависимостью ядра (раньше приходил транзитивно через
  `uvicorn[standard]`).

Уточнение (P014, 2026-09-27). Replay и пробный прогон —
`application/commands/process_replays.py` поверх `domain/process_replay.py`,
пробный прогон — `Sandbox.start_from` в `domain/process_sandbox.py`.

- **Кандидат** проверяется, как при публикации (п.2): каталог tenant'а,
  прошлая версия для стабильности id — последняя опубликованная ниже версии
  кандидата. Находки — `problems` ответа; с ошибкой экземпляры не
  прогоняются (`replayed: 0`), ответ всё равно `200`, как у `packages:test`.
  Нечитаемый `spec` — находка `invalid_document`. Процесса с ключом нет —
  `404`.
- **Экземпляры.** `instanceIds` — ровно эти (повторы схлопываются), каждый
  читается как `GET /process-instances/{id}`: без `processes.read` на его
  workspace — `403`, неизвестный id или экземпляр другого процесса — `404`. Без
  `instanceIds` — последние `limit` (по умолчанию 50, не больше 200)
  экземпляров текущей (последней) версии по `started_at`, только из
  workspace'ов, где у вызывающего `processes.read`. Закрытые экземпляры
  участвуют наравне с открытыми.
- **Номер версии — не поведение.** Движок пишет номер версии в состояние и
  в каждое событие `process.*`; кандидат под своим номером расходился бы с
  журналом на каждом `emit_event`. Поэтому кандидат идёт под номером версии
  экземпляра (`process_replay.as_version`): проверка и программы — его,
  номер — экземпляра. Выражение, читающее `instance.version`, видит номер
  экземпляра.
- **Сравнение.** Входы журнала по `seq`, календари — версий, записанных у
  входа; решения и намерения шага (намерения без `executed`) против
  записанных. Прогон экземпляра останавливается на первом расходящемся
  шаге: дальше пути разошлись, и каждое следующее сравнение шло бы с другой
  историей. Если все шаги совпали, итоговое состояние сверяется с
  сохранённым: `set`, вычисливший другое значение, своего решения не
  пишет — его видно только в данных.
- **Расхождение** — у экземпляра не больше одного, первое: `journalSeq`
  (запись журнала; первая — старт, `0`), `kind` — `decision` или `intent`
  (первое отличающееся из списка шага, с его `element`), `input` (кандидат
  отказал записанному входу, `replayed` — текст отказа), `data`, `timer`,
  `state` (шаги совпали, отличается итоговое состояние; `journalSeq` —
  последняя запись), `recorded` и `replayed` — отличающиеся элементы.
  Изменённая строка таблицы решений даёт расхождение `decision` с
  `element` шага `decide` ровно у тех экземпляров, чьи входы она решает
  иначе (SC-005).
- **Ничего не пишется**: транзакция `READ ONLY`; память не зовётся — ответы
  `recall` — входы журнала (ADR-0076 п.4).
- **Пробный прогон** — `given.fromInstance: <id экземпляра>` теста
  `packages:test`. Экземпляр читается в той же транзакции только на чтение,
  как `GET /process-instances/{id}` (без `processes.read` на его workspace
  — `403`, неизвестный или не-UUID id — `404` всего запроса), вместе с его ожидающими
  approvals (согласующий — principal или `role:<slug>`, исключённые),
  оставшимися согласующими последовательного шага и их числом. Песочница
  продолжает **копию** его состояния на версии процесса из пакета (экземпляр
  другого процесса — провал теста): открытые задачи шагов (`human` и
  `call.agent`) становятся задачами песочницы с исполнителем по цепочке
  назначения, ожидающие approvals — её approvals; вызов скилла, `recall` и
  вложенный процесс, которых экземпляр ждёт, остаются без ответа, как вызов
  без заглушки (их таймауты — таймеры состояния). Часы — `given.clock` или
  время последнего входа экземпляра. `given.data` и `given.stage` с
  `fromInstance` несовместимы (провал теста). Живой экземпляр не меняется.
  Состояние, которое версия пакета не может продолжить (элемент исчез),
  — ошибка движка, тест `error`; перенос по карте — `migrations` P015.

Уточнение (P015, 2026-09-28). План и применение —
`application/commands/package_plan.py` поверх `domain/package_plan.py`
(поля, владельцы, переименования, хэши) и `domain/process_migration.py`
(перенос экземпляра по карте), маршруты `POST /packages:plan` и
`POST /packages:apply`:

- **Виды.** Ядро планирует и применяет `Calendar` и `Process` (в этом порядке:
  процесс называет календарь); прочие виды пакета ставит установщик
  (`cp_packages`, `PLAN_KINDS`), в `changes` их нет (*заменено амендментом
  2026-09-29 ниже: ядро планирует и типы задач, агентов, правила*). Пакет без `package.yaml`
  — ошибка плана `package_manifest_missing`: применение записывает объекты под
  ключом пакета. `workspaceId` запроса подставляется в `spec.workspaceId`
  вида `${…}` (иначе `unresolved_install_variable`) и требует
  `processes.read` на него.
- **Поле** — член верхнего уровня `spec` (`/spec/stages`,
  `/spec/displayName`…). Что последнее применение хотело от объекта, хранит
  таблица `package_objects` (ревизия `d7f2a9c4e1b8`): `(tenant, kind, key)`,
  ключ пакета, `spec` пакета, его хэш, версия, `planHash`, кто и когда
  применил, `retired_at`. Поле, чьё значение в последней версии отличается от
  хотевшегося, правил человек после применения: владелец `console`, такое поле
  не перетирается (`applies: false`) — публикуемая версия берёт его последнее
  значение, — пока план не построен с `overwriteConsole: true`. Запись хранит
  то, что хотел пакет, а не опубликованное: сохранённое поле консоли остаётся
  полем консоли и в следующем плане; человек вернул значение пакета — поле
  снова пакета. `version` процесса — номер, не поле человека. Объект, который
  пакет ещё не применял, — все поля пакета. Флаг — часть плана (входит в
  хэш): `apply` передаёт тот же флаг, с другим — `plan_stale`.
- **Действия.** `create` — ключа нет; `update` — публикуемая спецификация
  отличается от последней версии; `unchanged` — совпадает (с учётом
  сохранённых полей консоли); `rename` — ниже. Процесс `update`/`rename` с
  номером версии не выше последней — ошибка `process_version_conflict` в
  плане. Каждый публикуемый процесс проходит проверку п.2 (как
  `packages:test`, поверх каталога — объекты пакета); ошибки — в `problems`.
  `retire` в плане ядра не возникает: ключ, переименованный прочь, выводится
  вместе с `rename`.
- **Переименование** `renames: [{kind, from, to}]` видов ядра: `to` — объект
  пакета, `from` — нет, ключ переименовывается один раз (иначе
  `invalid_rename`). Действует, когда `from` есть в каталоге и не выведен, а
  `to` нет: объект `to` публикуется версией пакета, его прошлая версия для
  стабильности id и сторона `from` миграций — версии старого ключа
  (`publish_process_definition(renamed_from=…)`), номер — выше последнего
  номера старого ключа; запись владения переезжает с объектом. Старый ключ
  выводится (`package_objects.retired_at`): выведенный процесс не заводит
  новых экземпляров ни событием старта, ни `POST /process-instances`
  (`409 process_retired`), а его открытые экземпляры — если миграция их не
  перенесла — идут дальше: старт-событие существующего ключа и `correlate` до
  них доходят. Публикация версии выведенного ключа в обход пакета возвращает
  его. Обе стороны в каталоге и не выведены — ошибка `rename_target_exists`;
  переименование уже применено (старый выведен или его нет) — план о нём
  молчит, повтор того же пакета — `unchanged`.
- **Экземпляры.** У процесса `update`/`rename` открытые экземпляры (`running`,
  `suspended`) ключа, который он продолжает, группируются по версиям. Миграция
  версии — та из `migrations` публикуемой спецификации, у которой `from` —
  версия группы, а `to` — публикуемая (прочие — история прошлых версий):
  `pin` — остаются, `migrate` — переносятся; без миграции — `unaffected`,
  остаются на своей версии. Экземпляр **стоит** на элементе
  (`process_migration.standing`): активная стадия, элемент активности
  (задачи, голосования, вызова, ожидания), блок и позиция потока, `try` и
  `fork` со своими ветвями, шаги незавершённой компенсации, элемент и стадия
  ждущего таймера, шаг с `onCompensate`, чья компенсация ещё не исполнена.
  `migrationRequired` — у `migrate` перенос хоть одного экземпляра не
  удаётся, у `unaffected` — хоть один стоит на элементе, которого после
  карты нет в новой версии или который сменил вид (вид шага — его вид шага);
  с этим план несёт ошибку `migration_required` (файл и строка процесса, путь
  `/spec/migrations/<i>` или `/spec`, подсказка с формой миграции), а
  `apply` отказывает `422 migration_required`. `pin` не проверяется.
- **Перенос по карте** (`migrate_state`, чистая функция): каждый id —
  по карте (не названный остаётся), позиция потока — «после того же
  элемента» в блоке с переименованным именем, а не индекс списка (вставленный
  до шага элемент не исполняется); указатели выражений таймеров переезжают со
  своим элементом (`/spec/stages/0/steps/1/human/due/at` →
  `/spec/stages/0/steps/2/…`); новые стадии — `available`, вехи — только
  известные новой версии. Не переносится — `MigrationError` с кодом
  (`migration_required`, `element_moved` — элемент ушёл из блока,
  `block_gone`, `expression_gone`, `element_gone`) и элементами. Следующий вход
  — обычный шаг новой версии: движок миграций не знает.
- **Применение** одной транзакцией: блокировка применений tenant'а
  (`pg_advisory_xact_lock`), записей владения и открытых экземпляров (`FOR
  UPDATE`), план строится заново и сравнивается по `planHash`: другой —
  `409 plan_stale` (`details.planHash`, `currentPlanHash`, `catalogEtag`);
  затем `migration_required` — `422 migration_required`, прочие ошибки —
  `422 invalid_package` (оба с `details.problems`). Публикация — обычными
  командами (`publish_calendar`, `publish_process_definition`) под правом
  вида применяющего; перенос экземпляра: `definition_id`, `definition_key`,
  `definition_version`, состояние, элементы его таймеров
  (`process_timers.element`), ожидающих `recall` и ссылок `refs`; запись
  журнала `kind: migrate` (`source_ref` `migration:<id версии>`, вход
  `{fromKey, fromVersion, toVersion, policy, map, planHash, state}` — с
  перенесённым состоянием, решение `migrated`) и событие `process.migrated`
  (`actorId` — применивший: перенос решил он, не процесс; корреляция
  `process:<id экземпляра>`). Внешние ссылки открытых задач
  (`process/<экземпляр>/<элемент>`) не переписываются — это история. Ответ —
  `applied[kind, key, action, version]` и `catalogEtag` после применения.
  Маршрут идёт через ключ идемпотентности, как прочие записи.
- **Replay после миграции.** Записи журнала до переноса исполнялись другой
  версией: `process_replay.replay` начинает с состояния последней записи
  `migrate`. Так replay экземпляра на своей версии и `:replay` кандидата
  сравнивают только то, что шло на текущей версии. Кандидат переименованного
  процесса в `behaviour` плана идёт под старым ключом (ключ — не поведение,
  как номер версии).
- **Хэши.** `catalogEtag` — `sha256` канонического списка `{kind, key,
  version, hash, applied, retired}` объектов пакета и переименованных прочь
  ключей: последняя версия, хэш записи владения, выведен ли. `planHash` —
  `sha256` канонического `{package: хэш файлов, catalogEtag, changes,
  processes, overwriteConsole}`, где от `processes` берутся номера версий и
  по группам экземпляров `version`, `fate`, `migrationRequired`: число
  открытых экземпляров и `behaviour` (выборка replay) — отчёт, их меняет
  каждый вход живого экземпляра, а применение от них не зависит. Появилась
  или исчезла группа, судьба или нужда в миграции — `plan_stale`.
- **Покрытие регламентов** (FR-058) — только в плане, после закрытия
  транзакции, как `governedBy` у `packages:test`. Разделы документа — узлы,
  указывающие на него связью `section_of` (онтология `process-knowledge`,
  P019): один типизированный запрос на документ (якорь и `traverse
  [{relation: section_of, direction: in, depth: 1, limit: 200}]`, без поиска
  по смыслу) в namespace процесса. Имя раздела — `attributes.section` узла,
  иначе его ключ без ключа документа и разделителя (`doc#4.2` → `4.2`).
  `regulationCoverage[]`: `found` — документ разрешён точно, `covered` —
  раздел → элементы (`<процесс>/<id элемента>`, у ссылки процесса в целом —
  ключ процесса; ссылки без `section` раздел не покрывают), `uncovered` —
  разделы памяти без элементов. Ссылка на раздел, которого у документа в
  памяти нет, — предупреждение `governed_by_unknown_section`. Память не
  настроена или не ответила — покрытие пусто и одно предупреждение
  `governed_by_unchecked`; план от памяти не зависит.

Амендмент 2026-09-29 (TASK-000903; решение владельца по вопросу R012,
TASK-000824). План и применение по хэшу охватывают все виды каталога, которые
держит ядро: консоль показывает и применяет пакет целиком, а не только
процессы и календари. Механизм прежний — изменения по объектам и полям с
владельцем поля, находки, `catalogEtag`, `planHash`, `409 plan_stale`,
`overwriteConsole`; добавлены виды и то, как каждый сравнивается и
публикуется (`application/commands/package_catalog.py`):

- **Виды и порядок.** `PLANNED_KINDS = (TaskType, Agent, Calendar, Process,
  WorkRule)` — порядок установщика: агент называет типы задач, процесс —
  типы задач, агента и календари, правило — типы задач и действует как агент.
  В этом порядке идут `changes`, `applied` и публикация. `PlanChangeOut.kind`
  — перечисление этих видов.
- **Форма объекта.** Поле — член верхнего уровня формы: того, что каталог
  держит об объекте, в именах пакета. Формы повторяют то, что сравнивает
  `cp_packages apply --install`, поэтому ключ, поставленный установщиком тем
  же файлом, план показывает `unchanged` (приёмка: оба пути дают один
  каталог).
  - `TaskType` — поля `POST /task-types` (`displayName`, `description`,
    `fieldSchema`, `lifecycleSchema`, `execution`, `approvalSchema`,
    `contextSchema`, `instructions`, `completionSchema`, `artifactSchema`,
    `acceptance`), нормализованные, как их хранит ядро. Последняя версия —
    старшая **активная**. Поля, которые установщик сравнивает, только если
    они есть в файле (`lifecycleSchema`, `contextSchema`, `instructions`,
    `completionSchema`, `artifactSchema`, `acceptance`), без них в файле
    берут значение последней версии — и при сравнении, и в публикуемой
    версии. Это осознанное расхождение с установщиком: `apply --install`,
    заводя новую версию из-за другого поля, шлёт только файл, и такие поля
    новой версии получают умолчания ядра (инструкции, критерии приёмки
    последней версии теряются). План их переносит: поле, которого пакет не
    задаёт, — не его, и молча стирать заданное в консоли он не должен. На
    `unchanged` расхождение не влияет (оба пути сравнивают одинаково), а
    новая версия по плану несёт эти поля от прошлой, по установщику — нет;
    после перевода установщика на plan → apply путь один. Версия неизменяема: `update` —
    следующая версия (номер — старший номер ключа + 1); все прочие активные
    версии ключа выводятся — новое поле изменения `deprecates: [версии]`,
    входит в хэш; у `unchanged` в нём — активные версии, кроме последней
    (так делает установщик).
  - `Agent` — тело ревизии (каноническое, CP-ADR-0073 §2) плюс желаемое
    состояние: `state` и `placement.replicas`. Изменилось тело — новая
    ревизия, только состояние — ревизия прежняя (`version` в плане и
    `applied` — номер ревизии). Выведенный ключ — ошибка плана
    `agent_retired`: пакет его не возвращает.
  - `WorkRule` — `description`, `trigger`, `condition`, `interpretation`,
    `action` (нормализованные, как хранит ядро), `identity`, `status`,
    `workspaceId` живого (не архивного) правила ключа. Первые шесть —
    `PATCH` (новая версия правила), `status` — `:enable`/`:disable` без
    новой версии (`version` в плане и `applied` у изменения только статуса —
    прежний номер, как у `set_rule_status`); другой `workspaceId` — ошибка `rule_workspace_immutable`
    (правило выводится в установке и заводится заново). `workspaceId: ${…}`
    подставляется из `workspaceId` запроса, как у процесса; без него —
    `unresolved_install_variable`.
- **Форма запроса.** Файл проверяется моделью запроса маршрута своего вида
  (`TaskTypeCreateRequest`, `AgentPublishRequest`, `RuleCreateRequest`; у
  правила без `goalId`) — находки `invalid_task_type`, `invalid_agent`,
  `invalid_rule` с путём в файле.
- **Проба команд в плане.** То, на что объект ссылается (скиллы, типы
  задач, агенты, роли, права, которые пишущий одалживает агенту), проверяет
  команда вида. План больше не `READ ONLY`: он строится в транзакции,
  которую всегда откатывает, и прогоняет команды типов задач, агентов и
  правил на том, что опубликовало бы применение, — каждую в своей точке
  сохранения, в порядке применения (правило видит тип задачи и агента из
  того же пакета). Отказ команды — находка объекта (код и сообщение
  команды, путь из `details`). Нет права вида — предупреждение
  `permission_required` на объекте, и проба останавливается: применению
  право нужно всё равно, а дальше пошли бы ложные находки. Объект со своей
  ошибкой плана не пробуется. Ничего из пробы не остаётся (тест: план не
  создаёт тип задачи).
- **Применение** — одна транзакция, как прежде: план заново под
  блокировкой, сравнение хэша (`409 plan_stale` для изменения любого вида:
  новая версия типа задачи, ревизия или состояние агента, версия или статус
  правила — всё это в `catalogEtag` через номер и хэш формы), ошибки плана —
  `422 invalid_package`. До построения плана применение берёт блокировки,
  которые берут команды видов: ключа типа задачи, агента и правила
  (advisory-блокировки `task_types`/`agents`/`work_rules`) и строки их
  объектов: агента — `FOR UPDATE`, типа задачи и правила — `FOR NO KEY
  UPDATE`. На строки типа задачи и правила ссылаются внешние ключи задач и
  оценок правил, их вставка берёт `FOR KEY SHARE`; движок процессов
  вставляет задачу, держа экземпляр, который применение блокирует следом, —
  `FOR UPDATE` на типе дал бы взаимоблокировку (тест: экземпляр под
  блокировкой и вставка задачи того же типа при ждущем применении).
  Порядок один у применения и у пробы плана: виды — в порядке
  `PLANNED_KINDS`, ключи внутри вида — по возрастанию; проба берёт все ключи
  до первой команды (отпущенная точка сохранения блокировки не отпускает,
  и по одной на команду они пришли бы в другом порядке). Публикация того же ключа, открытая в момент
  применения, либо фиксируется до плана (и хэш устарел — `plan_stale`), либо
  ждёт применения; между планом и командой она не встаёт (иначе применение
  вывело бы виденные версии типа и оставило чужую активной рядом со своей;
  тест `tests/concurrency/test_package_apply_races.py`). Затем каждый объект публикуется обычной командой
  своего вида под его правом (`task_types.manage`, `agents.manage`,
  `calendars.write`, `processes.write`, `rules.write`). Отказ команды при
  применении — её ошибка (`403`, `422 …`), и откатывается всё применение:
  частично применённого пакета не бывает. Ревизия агента записывает
  источник `package` с ключом и версией пакета из `package.yaml` (CP-ADR-0073,
  история ревизий) — как у установщика, а не `manual`. IAM-личности, чью привязку
  изменила ревизия агента, маршрут сбрасывает из кэша после фиксации.
- **Владение полями** — та же таблица `package_objects`: её `kind` допускает
  новые виды (ревизия `b5d1e7a3c9f4` амендмента TASK-000904, Е4); `spec` —
  форма, которую хотел пакет. Строка без `spec` (связь, записанная
  установщиком через `packages:record`, Е6) — как строки нет: все поля
  `package`.
  Поле, изменённое в консоли (новая версия типа задачи, `PATCH` правила,
  `:disable`, ревизия или состояние агента), — `console` и сохраняется без
  `overwriteConsole`.
- **Переименования** по-прежнему только у `Calendar` и `Process`: версии
  других видов не переезжают на новый ключ. `renames` другого
  планируемого вида — предупреждение `rename_not_planned` (старый ключ
  остаётся, новый применяется сам по себе).
- **Граница.** Остальные виды пакета план не применяет и перечисляет в новом
  поле `outside: [{kind, key, appliedBy}]`: `NotificationRule` —
  `notification-service` (правила уведомлений живут в сервисе уведомлений,
  ADR-0005 notification-service, у ядра их нет), `WorkspaceType`, `Role`,
  `Capability`, `Skill`, `ArtifactType`, `ProjectTemplate` — `installer`
  (`cp_packages apply`). Онтологии и доменные пакеты знаний — не объекты
  каталога пакета (ADR-0060): плана у них нет. Вывод ключей из оборота
  (`Installation.retire`) — тоже установщика: это объект установки, а не
  пакета.
- **Установщик.** Перевод `tools/cp_packages.py` на один путь plan → apply
  для этих видов (`PLAN_KINDS` = виды ядра, `apply --install` — через
  `packages:plan`/`packages:apply`, остальное — как прежде) — отдельная
  задача суперпроекта после вливания этой ветки. До перевода порядок
  такой: `cp_packages apply --install` ставит пакет (включая виды вне
  ядра), затем `packages:plan` того же пакета показывает `unchanged` и
  дальше пакет может применяться через консоль; записи владения
  (`package_objects`) у ключей, поставленных только установщиком, нет — все
  их поля считаются полями пакета до первого `packages:apply`. Применять
  один пакет попеременно обоими путями не следует: новая версия типа задачи
  у них различается полями, которых нет в файле (см. выше).

Уточнение (TASK-000969, 2026-09-29; ревью TASK-000903):

- **План берёт вызывающего первым.** Проба команд в плане — пишущая
  транзакция, хотя и откатываемая, поэтому `packages:plan` первым оператором
  берёт principal вызывающего (CP-ADR-0077 п.3, правило 1): `:disable`,
  зафиксированный, пока план ждал, — `403 principal_not_active`.
- **Объект другого пакета.** Применение пишет связь каждого объекта плана со
  своим пакетом (Е2) и так переводит объект, поставленный другим пакетом, на
  себя. План говорит об этом предупреждением `package_owner_changed` на
  файле объекта (`«вид/ключ» belongs to package A: the apply moves it to
  package B`); отказа нет — переименованный пакет законно забирает свои
  объекты. Пакет-владелец входит в `catalogEtag` (поле `package` записи
  каждого объекта): смена владельца между планом и применением — `409
  plan_stale`.
- **`packages:record` и `packages:apply` не блокируют друг друга.**
  Применение держит строки `package_objects` своего плана `FOR UPDATE`
  (одним запросом, по `kind, key`) и вставляет недостающие в конце; запись
  установщика апсертит те же строки по одной в порядке видов каталога.
  Встретившись, они ждали бы друг друга (`40P01`, `500`). Поэтому
  `packages:record` до первой строки берёт ту же advisory-блокировку
  применений tenant'а (`package_links.lock_applies`) и ждёт применения
  целиком (тест `tests/concurrency/test_package_apply_races.py`).

Уточнение (P027, 2026-09-28). Компенсация и разбор дела — ошибки, которые
нашёл пакет `tenders` (P023) в песочнице:

- **`step` в `onCompensate`** — текущий шаг блока, как в любом блоке: после
  шага блока `step` — его результат, и `output.as` шага компенсации читает
  свой результат (`step.result`). Компенсируемый шаг — отдельная привязка
  **`compensated`** (`{id, status, result}`), видна во всём блоке
  `onCompensate`; проверка типизирует её по выходу компенсируемого шага
  (скилл, форма, таблица, `recall` — как `step.result` в его `output.as`), вне
  блока её нет (`expression_type_error`). `try.catch[].as: compensated`
  внутри `onCompensate` — `invalid_error_binding`. Кадр компенсации несёт
  `bindings: {compensated}`; кадр прежнего движка (`stepVar`) читается как
  `compensated`, поэтому экземпляр посреди компенсации доходит её по новой
  семантике.
- **Разбор дела** — вход `process.retrospective@1` по его контракту
  (`packages/process-knowledge/skills/process.retrospective.yaml`
  суперпроекта): `case` — узел дела `{kind, key, title}` из проекции памяти,
  `definitionKey`, `version`, `instanceId`, `outcome`, `data`, `entities`
  проекции (до 200), `appliesToKinds` — `retrospective.appliesTo` процесса;
  `journal` прикладывает исполнитель (`attach: [journal]`). Процесс без ключа
  дела разбор не заводит: `retrospective_skipped` с `error.type:
  case_unknown`. Подробности — ADR-0076 п.6.

### 17. MCP-инструменты автора процессов

Уточнение (P016, 2026-09-27; FR-031, FR-033, FR-034). MCP-сервер
оператора (`control_plane_mcp/server.py`) даёт автору процесса шесть
инструментов — тонкие адаптеры маршрутов ядра; правил и состояния у них
нет, ядро проверяет права и инварианты само:

| Инструмент | Маршрут | Пишет |
|---|---|---|
| `cp_pkg_check(path)` | `POST /packages:test?checkOnly=true` → `{status, problems}` | нет |
| `cp_pkg_test(path, tests?)` | `POST /packages:test` (`PackageTestOut`) | нет |
| `cp_pkg_plan(path, workspaceId?, replayLimit?, overwriteConsole?)` | `POST /packages:plan` (`PackagePlanOut` с `planHash`) | нет |
| `cp_pkg_apply(path, planHash, …)` | `POST /packages:apply` | да |
| `cp_process_get(ref)` | `GET /process-definitions/{ref}` и первая страница `…/{key}/versions` | нет |
| `cp_process_explain(instanceId)` | экземпляр, его журнал и версия, на которой он идёт | нет |

- **Пакет — каталог на диске** (`path`): все файлы `*.yaml`/`*.yml` под
  ним, путь относительно каталога через `/`; скрытые файлы и каталоги
  (`.layout`, `.git`) в пакет не входят. Ограничения числа и размера файлов
  проверяет ядро (п.10).
- **Применение — только по хэшу плана.** `planHash` у `cp_pkg_apply`
  обязателен; пустой или не `sha256:<64 hex>` — отказ
  `plan_hash_required` без обращения к ядру. Старый хэш (каталог, открытые
  экземпляры или файлы изменились после плана) — `409 plan_stale` ядра
  (п.11) с подсказкой построить план заново. `cp_pkg_apply` — единственный
  пишущий инструмент автора: он не read-only и не доказательство работы,
  поэтому вложенному исполнителю не выдаётся (`withheld_tool_names`,
  ADR-0046); проверка, тест, план и чтение — read-only.
- **Ошибки — в форме находки п.2** (`ProcessProblemOut`, P006):
  `{error, message, details?, hint?, problems: [{code, severity, path,
  file, line, message, hint}]}`. Находки отказа ядра берутся из
  `details.problems` (`invalid_package`, `migration_required`); ошибка
  тела запроса — по находке на `details.errors`; любой другой отказ (право,
  `not_found`, `plan_stale`) и ошибки самого адаптера (`package_not_found`,
  `package_empty`, `package_file_unreadable`, `plan_hash_required`) —
  одна находка с `severity: error`. Отчёты проверки, теста и плана
  возвращаются как есть: их `problems` уже в этой форме.
- **Объяснение экземпляра** читает то же, что replay (п.5): экземпляр,
  журнал решений (до 1000 записей; дальше — `journalCursor`) и определение
  версии экземпляра (`key@version`). Ответ: `instance`; `process` — ключ,
  версия, хэш и `governedBy` процесса; `steps` — по записи журнала: вход
  (что пришло, `actorId`, `eventId`, тело входа), решения и намерения с
  `reason` и `data`; у каждого решения и намерения `governedBy` — документы
  его элемента и объемлющих элементов, ближайший первым, затем процесса,
  каждый с `element`, где он объявлен (`null` — процесс; элемент
  `<шаг>:<часть>` — таймер или ветка шага — отвечает регламентам шага);
  `memory` — ответы памяти на шаги `recall` (вход `recall` журнала:
  `status`, `result` или `reason`, шаг).
- Клиент (`control_plane_client`) получает методы `get_process_definition`,
  `list_process_versions`, `get_process_instance`, `list_process_journal`,
  `test_package`, `plan_package`, `apply_package`.

## Границы

- Движок не исполняет код: только объявленные блоки. Всё остальное —
  имеющиеся задачи, approvals, скиллы, наблюдения и правила.
- Нет BPMN XML и импорта из внешних редакторов; нет `goto`.
- Нет локальной библиотеки движка у `cp_packages`: без ядра работает только
  статическая проверка схемой.
- Визуальный редактор в решение не входит; раскладка — файлы
  `.layout/<process>.json` пакета, ядро их не читает.

## Последствия

- Ядро получает подсистему процессов: определения, экземпляры, таймеры,
  календари, песочницу тестов, replay и план (отступление по ст. VIII принято
  владельцем).
- `process-runtime` выводится из суперпроекта (TAI-ADR-0032 → Superseded by
  TAI-ADR-0054).
- Задачи и approvals экземпляров — обычные объекты ядра: консоль, «Важное»,
  MCP и исполнители видят их без доработки.
- `control-plane-worker` получает два цикла (`process_events`,
  `process_timers`) и третий курсор журнала.

## Не принято

- Компиляция процесса в набор правил (`WorkRule`): у правила нет состояния
  экземпляра — таймеров, кворума и компенсаций не выразить.
- Отдельный сервис процессов или процесс-воркер: второй источник состояния,
  лишняя память на staging.
- Кворум внутри одного approval ядра: пришлось бы менять модель approval для
  всех потребителей.
- Разделение обязанностей в движке: голос в обход движка его бы не заметил.
- Синхронная память в транзакции движка (ADR-0076).

## Амендмент 2026-09-29 (TASK-000904): привязка объекта каталога к пакету

Решение владельца 2026-09-29 по вопросу из R012 (TASK-000824): ни один ответ
ядра не говорил, каким пакетом поставлен объект каталога, и консоль
группировала установленное по виду, а не по пакетам. `package_objects`
(п.11) знала пакет только у видов, которые планирует ядро (`Process`,
`Calendar`); остальные виды установщик (`cp_packages --install`) применяет
обычными маршрутами, и ядро пакета не видело.

### Е1. Связь объекта с пакетом

Объект каталога — `(kind, key)` tenant'а: все версии типа задачи, процесса,
календаря, все ревизии агента — один объект. `package_objects` становится
связью объекта с пакетом, который его поставил, для всех видов каталога,
которые держит ядро: `ArtifactType`, `TaskType`, `ProjectTemplate`,
`WorkspaceType`, `Role` (роль tenant'а; роль workspace пакету не
принадлежит), `Capability`, `Skill`, `WorkRule`, `Agent`, `Process`,
`Calendar`. `NotificationRule` живёт в сервисе уведомлений, `Package` и
`Installation` — не объекты каталога tenant'а. Нет строки — объект создан
вручную.

Связь принадлежит объекту, а не версии: версия, опубликованная человеком
позже, остаётся в пакете своего ключа (кто правил поле — вопрос плана,
`owner` в п.11). История источника по ревизиям у агента остаётся своей
(CP-ADR-0073 Г2).

Строка хранит ключ пакета, **версию пакета** (`package.yaml → spec.version`)
и **хэш установки**: `planHash` применённого плана — для `packages:apply`,
`installHash`, названный установщиком, — для `packages:record`. Поля
«что хотело применение» (`version`, `spec`, `spec_hash`) обязательны только
у планируемых видов (проверка `ck_package_objects_planned_spec`).

### Е2. Кто пишет связь

- **`POST /packages:apply`** пишет связь каждого объекта плана, включая
  `unchanged`: применение следующей версии пакета переводит связь на неё,
  даже если объект не изменился; переименованный прочь ключ сохраняет
  связь с пометкой `retired_at` (как прежде).
- **`POST /packages:record`** `{package: {key, version}, installHash?,
  objects: [{kind, key}]}` — маршрут установщика. После применения
  непланируемых видов установщик называет **все** объекты пакета, которые он
  применил, в том числе без изменений: связь переходит на эту версию пакета.
  Виды — только непланируемые (перечисление в схеме запроса; `Process`,
  `Calendar` и чужие виды — `400 invalid_request`); объект, которого нет в
  каталоге, — `422 unknown_object` со списком, и ничего не пишется (запрос
  целиком или никак). Права: `packages.plan` и право записи каждого названного
  вида (`task_types.manage`, `artifact_types.manage`,
  `project_templates.manage`, `workspaces.manage`, `org.manage` для ролей,
  возможностей и скиллов, `agents.manage`, `rules.write` на workspace
  каждого живого правила): связь говорит, чей объект, и пишет её тот, кто
  может писать сам объект. Ответ — `PackageRecordOut {package, installHash,
  recorded}` в порядке видов каталога. Событий нет: связь — не изменение
  объекта. Запись идёт под блокировкой применений tenant'а (уточнение
  TASK-000969 в п.11): применение и запись связей одного tenant'а не
  пересекаются.
- **`POST /agents`** с `package` (CP-ADR-0073 Г2) пишет и связь агента — с
  ревизией и без неё; `installHash` у такой связи пуст, пока установщик не
  назовёт агента в `packages:record`.

Связь — заявление применяющего с правом вида, как источник ревизии агента:
ядро не сверяет ключ и версию с файлами пакета.

### Е3. `package` в ответах и `?package=`

Списки и карточки всех видов Е1 (`GET /task-types`, `/task-types/{id}`,
`/artifact-types[/{ref}]`, `/project-templates[/{id}]`,
`/workspace-types[/{id}]`, `/roles[/{id}]`, `/capabilities[/{id}]`,
`/skills[/{ref}]`, `/rules[/{id}]`, `/agents[/{ref}]`, `/agents/me`,
`/process-definitions[/{ref}]`, `/calendars[/{ref}]`), ответы их записей и
вложенные объекты назначений принципала отдают поле
`package: PackageLinkOut | null` — `{key, version, installHash,
installedAt}`; `version` пуст у строк, применённых до амендмента. Поле
добавлено, прежние поля не менялись: ответ обратно совместим.

Списки этих видов принимают `?package=<key>` — только объекты, которые
поставил пакет с этим ключом (`EXISTS` по `package_objects`: страница,
порядок и курсор — прежние). Консоль группирует установленное по пакетам
теми же запросами списков, что и по видам: поле `package.key` у каждого
элемента или фильтр по ключу пакета.

### Е4. Миграция

Ревизия `b5d1e7a3c9f4`: проверка вида расширена до видов Е1; колонка
`package_version`; `plan_hash`, `version`, `spec`, `spec_hash` — nullable
с проверкой `ck_package_objects_planned_spec`; индекс
`ix_package_objects_tenant_package (tenant_id, package_key, kind)` под
фильтр. Агенты, чьи ревизии называют пакет, получают связь по новейшей такой
ревизии. Откат удаляет связи непланируемых видов и версии пакетов.

### Е5. Что осталось установщику

`cp_packages` суперпроекта должен после применения каждого пакета вызвать
`POST /packages:record` со всеми применёнными объектами непланируемых видов
(ядро их не видит), передавать `package` в `POST /agents` и `installHash`
(например, хэш файлов пакета). Без этого связь есть только у процессов,
календарей и агентов, опубликованных с `package`.

### Е6. Согласование с планом всех видов (TASK-000903)

Амендмент п.11 от 2026-09-29 (TASK-000903) делает `TaskType`, `Agent` и
`WorkRule` планируемыми видами, а установщик применяет их обычными
маршрутами, пока не перейдёт на plan → apply. Поэтому:

- `packages:record` принимает все виды Е1, кроме видов движка (`Process`,
  `Calendar`): `TaskType`, `Agent`, `WorkRule` — тоже (переходный путь
  установщика). Запись установщика очищает у строки «что хотело применение»
  (`version`, `spec`, `spec_hash`): объект перезаписан маршрутом вида, и
  следующий план считает все поля `package`. Так же пишет связь
  `POST /agents` с `package`.
- `packages:apply` пишет строку этих видов с `version`, `spec`, `spec_hash`
  сам; `POST /agents`, вызванный изнутри применения, связь не пишет.
- Проверка `ck_package_objects_planned_spec` остаётся на видах движка:
  у `TaskType`, `Agent`, `WorkRule` строка без `spec` законна (связь
  установщика или миграции Е4).
- Отдельной миграции у TASK-000903 нет: проверку вида расширила ревизия
  `b5d1e7a3c9f4`.

## Conformance

- `tests/unit/test_process_contract.py`: маршруты и тела в OpenAPI, `501` у
  каждого ждущего маршрута, форма находки, план с хэшем и этагом,
  `excludedPrincipals` у approval, права в `Permission` и
  `authz/catalog.yaml`, события `process.*`, `calendar.published`,
  `knowledge.changed`; копии `object.schema.json`, `test.schema.json` и
  примеров равны суперпроекту; примеры процесса, календаря и теста проходят
  схему каталога и модели ядра; принятое схемой каталога для календаря
  принимает ядро, отвергнутое — отвергает.
- `tests/integration/test_processes_contract.py`: каждый маршрут проверяет
  своё право и отвечает `501` с шагом; путь вне пакета — `400`; approval
  с пустым `excludedPrincipals` — как прежде.
- `tests/unit/test_approval_quorum.py` (P008): `all`, `any`, `atLeast`,
  `percent`, «двое из трёх» — досрочные одобрение и отказ, без досрочного
  решения, `parallel`/`sequential`, уход согласующего.
- `tests/integration/test_approval_separation_of_duties.py` (P008): голос
  исключённого principal'а (`:approve`, `:reject`, отмена чужого gate)
  отвергается `403 separation_of_duties_violation` обычным маршрутом решения,
  approval остаётся `pending`, решает другой держатель роли; поле в ответе и
  в `approval.requested`; проверки запроса; «Важное».
- `tests/unit/test_event_catalog.py`: `docs/events/` совпадает с кодом,
  `process_instance`, `process_definition`, `calendar` — сущности ядра.
- `tests/unit/test_process_definition.py` (P006): негативные фикстуры
  `tests/fixtures/processes/invalid/` по классам находок дают свой код и
  путь; файл и строка находки из файла пакета; стабильность id против прошлой
  версии; хэш не зависит от порядка ключей; копия схемы вида равна схеме
  каталога. `tests/unit/test_decision_table.py`: ячейки, политики,
  перекрытия, пробелы, вычисление.
- `tests/integration/test_process_definitions.py` (P006): версия
  публикуется один раз, другое содержимое той же версии и версия не выше
  последней — `409`; `422 invalid_process` с находками; предупреждения в
  версии; личность и эскалация прав; чтение по ключу и версии, список,
  фильтры `governedBy` и `workspaceId`, версии без `spec`; строки неизменяемы.
- `tests/unit/test_process_engine.py` (P007): каждый блок и вид шага,
  стадии, вехи и сторожа, необязательная работа, таймеры (установка,
  пересчёт по изменённым полям и по новой версии календаря, сработавший не
  откатывается, заморозка и сдвиг на длительность приостановки),
  эскалации, кворум, `recall` (ответ, таймаут, опоздавший ответ),
  `remember`, компенсации и отмена, ошибка компенсации, подъём ошибки до
  `try` через `fork`, лимит стоимости выражения, разбор дела (вход
  сверяется с закреплённым контрактом скилла, P027), `step` и `compensated`
  в `onCompensate` и их типы (P027), пример
  процесса схемы каталога от извещения до закрытия; каждое событие
  `process.*` сверяется со схемой каталога событий; один журнал, поданный
  дважды и повторно (replay), даёт те же решения, намерения и состояние.
- `tests/integration/test_process_instances.py` (P009): старт наблюдением →
  задача ядра от личности процесса с внешней ссылкой → завершение задачи →
  таймеры шага → изменение данных пересчитывает их → срабатывание,
  эскалация, закрытие; повтор события старта — `process.correlated`, не
  второй экземпляр; повторная доставка пакета ничего не меняет; ответ задачи
  доходит только до своего экземпляра; `:suspend`/`:resume`/`:cancel` с
  правом и заморозкой таймеров; явный старт — один экземпляр на ключ;
  постоянная цель теряет и снова достигает веху; кворум `any` и
  `sequential`; `separationOfDuties` до P008 — `intent_failed`.
  `tests/unit/test_process_engine.py` дополнен явным стартом, отменой без
  компенсаций и потерей вехи; `tests/unit/test_process_neutrality.py` —
  страж нейтральности.
- `tests/unit/test_package_test.py` (P013): копия схемы тестов равна
  закреплённой; YAML 1.2 и строки по указателю; разбор пакета с находками и
  их файлом и строкой; `data.$ref` из пакета и отказ за его пределы; фильтр
  тестов; заглушка скилла по схеме отвечает, **не по схеме — тест падает**;
  вызов без заглушки ждёт, ошибка заглушки — ошибка шага; заглушка `recall`
  не в форме ответа памяти — провал; разделение обязанностей и
  `not_eligible` у `approve`; виртуальное время и `until:`; задача человеку
  (исполнитель, чужой, `fieldSchema`); несбывшиеся ожидания с `expected` и
  `actual`; `noSideEffects` по счётчику; пример теста суперпроекта проходит;
  покрытие перечисляет непройденные элементы, переходы, строки и
  обработчики, второй тест их закрывает; порог `coverage.minimum`; у
  песочницы нет клиентов.
- `tests/integration/test_package_test.py` (P013): пакет с тестом проходит,
  покрытие перечисляет непройденное, **все таблицы базы после прогона
  построчно те же**; `checkOnly`; неверный пакет — `invalid` с файлом и
  строкой, тесты не идут; заглушка не по схеме скилла каталога — `failed`;
  объекты пакета (тип задачи, скилл, агент, календарь) известны его процессу
  до применения; право `packages.test`.
- `tests/unit/test_process_replay.py` (P014): версия на своём журнале — ноль
  расхождений, в том числе как следующая версия; номер кандидата без
  `as_version` расходится на событиях; сдвинутая строка таблицы — ровно
  затронутые экземпляры, `decision` у шага `decide`; `set` с другим значением
  — `data`; отказ входа — `input`; итоговое состояние — `timer`, `state`.
  `tests/unit/test_package_test.py` (P014): пробный прогон с копии —
  задача и approval экземпляра, разделение обязанностей, живое состояние не
  меняется, часы экземпляра и `given.clock`, экземпляр другого процесса,
  `given.data` рядом, неизвестный экземпляр.
- `tests/integration/test_process_replay.py` (P014): на Postgres версия на
  журналах своих экземпляров — ноль расхождений, изменённая строка таблицы —
  ровно два затронутых из трёх, **все таблицы базы после replay построчно
  те же**; `instanceIds`, `limit`; кандидат с ошибкой — `problems` без
  прогона; право `packages.test`, неизвестный процесс и экземпляр — `404`;
  пробный прогон `given.fromInstance` проходит без записей, живой экземпляр
  не тронут, неизвестный — `404`.
- `tests/unit/test_process_migration.py` (P015): экземпляр стоит на стадии,
  шаге ожидания и таймере; переименованный с картой шаг — активность, позиция
  «после того же элемента» при вставленном до него шаге, таймер с
  переехавшим указателем, новая стадия, и следующий вход версии 2 доводит
  экземпляр до закрытия; без карты и со сменой вида — отказ с элементом;
  шаг, ушедший в другой блок, — `element_moved`; миграция версии — только в
  публикуемую; replay начинается с последней записи `migrate`.
- `tests/integration/test_package_plan.py` (P015): **переименование шага с
  картой при открытых экземплярах — все три на новой версии и на том же
  шаге**, с таймером, записью журнала и `process.migrated` у каждого, задача
  версии 1 завершает шаг версии 2; повтор пакета — `unchanged`; элемент исчез
  без миграции — `migration_required` в плане и `422`, `pin` — экземпляры
  остаются; **каталог изменился между планом и применением — `409
  plan_stale`**, другой пакет под тем же хэшем — тоже; план ничего не пишет;
  поле, правленное человеком, — `console` и сохраняется, `overwriteConsole`
  перетирает, флаг входит в хэш; переименование процесса переносит
  экземпляры, старый ключ не стартует (`409 process_retired`); **план
  перечисляет непокрытые разделы регламента** и раздел, которого нет в
  памяти; права `packages.plan` и права вида, пакет без манифеста.
- `tests/integration/test_package_plan_catalog.py` (амендмент 2026-09-29):
  **план пакета с типом задачи, агентом и правилом — три `create`, apply
  пишет их, тот же план повторно — `409 plan_stale`**, повтор пакета —
  `unchanged`, проба команд ничего не оставляет; по каждому виду `update`
  (новая версия типа и вывод старой, ревизия агента, `PATCH` и статус
  правила), состояние агента без новой ревизии, **изменение любого вида
  между планом и применением — `plan_stale`**; поле, правленное в консоли,
  у каждого вида сохраняется и перетирается `overwriteConsole`; **ключи,
  поставленные как `apply --install` (обычные маршруты с тем же spec), —
  `unchanged`**, поле, которого нет в файле, сохраняется; права каждого
  вида — предупреждение `permission_required` в плане, `403` и ничего не
  записано; отказ команды (неизвестный тип задачи правила) — находка с
  файлом и путём, форма — `invalid_task_type`; `outside` и
  `rename_not_planned`; выведенный агент и чужой workspace правила — ошибки
  плана и `422 invalid_package`; объект другого пакета — предупреждение
  `package_owner_changed`, применение переводит связь (TASK-000969).
- `tests/concurrency/test_package_apply_races.py` (TASK-000903,
  TASK-000969): публикация типа задачи, ревизия агента и `:disable` правила
  во время применения — `409 plan_stale`, чужая версия не перетёрта; проба
  плана берёт ключи в порядке применения; вставка задачи движком при
  ждущем применении; план ждёт `:disable` вызывающего и отвечает `403
  principal_not_active`; **`packages:record` во время применения того же
  пакета ждёт его на advisory-блокировке и оба отвечают `200`, без
  `40P01`**.
- `tests/client/test_mcp_process_tools.py` (P016): инструменты автора на
  фикстурном пакете-каталоге — проверка и тест с файлом и строкой находки,
  каталог не пакет — находка; `cp_pkg_apply` без хэша (или не хэш) отказывает
  `plan_hash_required`, со старым хэшем — `plan_stale`, отказ ядра —
  его находки; `cp_process_get` по ключу и версии; объяснение экземпляра —
  решения с причинами, ответ памяти на `recall`, регламенты элементов;
  вложенному исполнителю не выдаётся только `cp_pkg_apply`.
- `tests/unit/test_package_links_contract.py` (TASK-000904): виды связи
  совпадают в домене, схеме запроса, проверке таблицы и формате каталога;
  у списка и карточки каждого вида — `package` (`PackageLinkOut` или null) и
  `?package=`; маршрут `packages:record` с телами.
- `tests/integration/test_package_links.py` (TASK-000904): применение
  связывает календарь пакета, ручной — `null`, фильтр; следующая версия
  пакета переводит связь неизменившегося объекта; версия, опубликованная
  вручную, остаётся в пакете ключа; `packages:record` связывает тип задачи,
  правило, роль tenant'а (не роль workspace), возможность и скилл, фильтры
  списков, обновление версии; неизвестный объект — `422 unknown_object`,
  ничего не записано; `Process`/`Calendar`/чужой вид — `400`; права
  `packages.plan` и вида; агент с `package`; миграция вниз и вверх и связь
  агентов по ревизиям.
- Реализующие шаги дополняют раздел своими тестами.
