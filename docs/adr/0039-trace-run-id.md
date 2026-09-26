# ADR-0039: Сквозной trace-идентификатор `X-Run-Id`

Статус: Принято (v0.5)

## Контекст

Расследование инцидента «клиент → Control Plane → Memory» требует одного
идентификатора во всех логах и трассировках. У Control Plane уже есть
`X-Request-ID`, но он живёт ровно один HTTP-запрос и не переживает асинхронную
доставку через журнал.

Терминологическая ловушка: `Run` — доменная сущность исполнения (ADR-0012).
Трассировочный `X-Run-Id` — не она.

## Решение

Заголовок `X-Run-Id` принимается на всех запросах и валидируется по
`^[A-Za-z0-9._:\-]{1,128}$`. Невалидное или отсутствующее значение заменяется
серверным `run_<uuid4.hex>`; клиентскому значению доверяется только формат.
Значение эхом возвращается в ответе.

Разведение имён закреплено в коде и документации:

- доменная сущность — `Run`, поля `run_id`, `runId`;
- трассировка — `trace_run_id` в Python, `traceRunId` в JSON, заголовок
  `X-Run-Id`.

Путь распространения:

1. `RequestIdMiddleware` кладёт значение в `scope["state"]` и в contextvar;
2. `AuthContext.trace_run_id` несёт его в командный слой;
3. `record_event` пишет колонку `events.trace_run_id` и добавляет `traceRunId`
   в payload outbox;
4. Context Adapter кладёт `traceRunId` каждого события в `data` наблюдения и
   отправляет заголовок `X-Run-Id` на батч (значение адаптера,
   `adapter_<uuid4.hex>`, так как батч объединяет события разных трасс);
5. синхронный `POST /context` передаёт `X-Run-Id` самого запроса в Memory;
6. `JsonFormatter` печатает `run_id` рядом с `request_id`.

Колонка `events.trace_run_id` nullable: исторические события её не имеют, и это
корректно.

## Последствия

- Одно значение связывает HTTP-запрос, доменное событие, доставку в Memory и
  логи обеих сторон.
- Клиент может подделать `X-Run-Id`; это трассировка, а не авторизация, и она
  никогда не участвует в принятии решений. Формат ограничен, чтобы значение
  нельзя было использовать для инъекции в логи.
- В ingest-батче заголовок принадлежит адаптеру, а не событию: per-observation
  трасса живёт в `data.traceRunId`. Иначе пришлось бы дробить батчи по трассам и
  терять пропускную способность.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- grep: {path: src/control_plane/api/middleware.py, pattern: '_TRACE_RUN_ID_RE = re\.compile\(r"\^\[A-Za-z0-9\._:\\-\]\{1,128\}\$"\)'}
  repo: control-plane
- grep: {path: src/control_plane/infrastructure/db/models.py, pattern: 'trace_run_id'}
  repo: control-plane
- grep: {path: src/control_plane/application/events.py, pattern: '"traceRunId": trace_run_id'}
  repo: control-plane
- grep: {path: src/control_plane/worker/context_adapter.py, pattern: 'trace_run_id=f"adapter_\{uuid\.uuid4\(\)\.hex\}"'}
  repo: control-plane
- grep: {path: src/control_plane/application/context/mapping.py, pattern: 'data\["traceRunId"\] = event\.trace_run_id'}
  repo: control-plane
- grep: {path: src/control_plane/logging.py, pattern: 'trace_run_id_var'}
  repo: control-plane
```
