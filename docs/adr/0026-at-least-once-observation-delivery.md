# ADR-0026: At-least-once доставка Observations и курсор Context Adapter

Статус: Принято (v0.4)

## Контекст

События журнала должны попадать в Memory без потерь, но выстраивать
exactly-once между двумя PostgreSQL — распределённая ловушка. Memory Service
уже дедуплицирует по стабильной source identity
`(system, stream, external_id)`.

## Решение

Простое **at-least-once + идемпотентность**, вместо распределённых гарантий:

    read events after cursor → whitelist/translate → POST batch per tenant
    → provider confirms ALL → advance durable cursor

- Идентичность наблюдения стабильна и не зависит от попытки доставки:
  `system=control-plane, stream=domain-events, external_id=event:<uuid>`.
  Crash между подтверждением и коммитом курсора ⇒ повторная отправка ⇒
  duplicate на стороне Memory (проверено E2E). Delivery-attempt id не
  используется нигде.
- Курсор — строка `event_consumer_cursors` (name / tx_id / sequence /
  updated_at / metadata) — **глобальный один** на консюмера, не per-tenant:
  сохраняет глобальный порядок журнала и тривиальное состояние. Per-tenant
  изоляция отклонена для v0.4 (усложнение ради сценария «валидационная
  ошибка одного tenant'а», который закрывается policy ниже). Группировка по
  tenant происходит на уровне батча (у Memory один write-namespace на
  запрос).
- Singleton: session-level advisory lock (`pg_try_advisory_lock`) на
  выделенном соединении; вторая реплика ждёт, а не двоит консюмер.
  Дубликаты при failover допустимы by contract.
- Poison policy — **никогда silently drop**: permanent-отказ провайдера
  (4xx-валидция) НЕ продвигает курсор; адаптер паркуется на отравленной
  единице с экспоненциальным backoff и видимой диагностикой
  (`metadata.last_error`, `failures_total`, метрики). Разрешение —
  операторское (починить mapping/provider и перезапустить, либо явно
  передвинуть строку курсора). `log error + advance` уничтожил бы
  replayability и запрещён.
- Retry: bounded timeout, экспоненциальный backoff с cap, без hot loop;
  outage провайдера просто копит lag (проверено E2E: catch-up после
  рестарта без потерь и без семантических дублей).
- Data minimization: перевод — явный whitelist полей per event type
  (`mapping.py`), домены никогда не сериализуются вслепую; секреты,
  API-ключи, скрытые рассуждения не пересекают границу. `mappingVersion`
  записывается в каждый observation для интерпретации после future rebuild.
- Шумовые события (session lifecycle, claim churn, checkpoints, run
  actions) не доставляются, но продвигают курсор.

## Последствия

Rebuild Memory из журнала Control Plane воспроизводит все event-derived и
explicit наблюдения (контент внешних документов — вне зоны; re-ingest их
источников — ответственность document-коннекторов Memory).

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

Глобальный единственный курсор заменён per-tenant курсором решением ADR-0036 — пробы проверяют то, что осталось в силе: стабильную идентичность наблюдения, whitelist-маппинг с `mappingVersion`, singleton по advisory lock и poison policy без продвижения курсора.

```conformance
- grep: {path: src/control_plane/application/context/mapping.py, pattern: '"external_id": f"event:\{event\.id\}"'}
  repo: control-plane
- grep: {path: src/control_plane/application/context/mapping.py, pattern: 'data\["mappingVersion"\] = MAPPING_VERSION'}
  repo: control-plane
- grep: {path: src/control_plane/application/context/mapping.py, pattern: 'def is_memory_worthy\('}
  repo: control-plane
- grep: {path: src/control_plane/worker/context_adapter.py, pattern: 'func\.pg_try_advisory_lock\(_ADVISORY_LOCK_KEY\)'}
  repo: control-plane
- grep: {path: src/control_plane/worker/context_adapter.py, pattern: 'merged\["failures_total"\] = int\('}
  repo: control-plane
```
