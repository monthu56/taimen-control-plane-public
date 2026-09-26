# ADR-0025: Внешний Context Memory Engine за жёсткой HTTP-границей

Статус: Принято (v0.4)

## Контекст

Организации нужна долговременная память о работе (findings, decisions,
накопленные факты) с retrieval и компиляцией контекста. Полный Context
Memory Engine (Observation journal, temporal facts, AGE/pgvector, hybrid
retrieval, Context Compiler) уже существует как отдельный, независимо
лицензируемый продукт.

## Решение

Control Plane интегрирует память ТОЛЬКО через сетевую границу:

- runtime-зависимость — исключительно публичный HTTP-контракт
  (`/api/memory/observations:batch`, `/api/memory/context`, `/healthz`);
- никаких импортов Python-кода Memory, доступа к его PostgreSQL, знания AGE-
  схемы, общих транзакций, memory-таблиц в Control Plane;
- DTO на границе — небольшой явный дубликат контракта (plain dict + typed
  builders), закреплённый контрактными тестами против реального сервиса
  (`tests/contract/`), а не генерацией из чужого кода;
- абстракция нейтральна: `ContextProvider` (`none|http`), конфиг
  `CP_CONTEXT_*`; reference-реализация — `HttpContextProvider`.

Опциональность: при `CP_CONTEXT_PROVIDER=none` Control Plane полностью
функционален; provider не участвует в `/health/ready`; его недоступность —
деградация памяти, никогда не деградация координации.

Отображение идентичности: `tenant:<uuid>` → Memory namespace (жёсткая
изоляция), `workspace/task/run/principal/artifact:<uuid>` → Memory scopes
(видимость внутри namespace). Только стабильные UUID, никогда display-имена.
Авторизация scope'ов — обязанность Control Plane ДО вызова провайдера:
Memory не расширяет ничьих полномочий (его токен вообще не покидает
серверную сторону — harness'ы ходят только в Control Plane).

## Последствия

Memory Service заменяем и независимо деплоится/лицензируется; correctness
координации математически не зависит от него. Расхождение ADR-016 (Proposed)
в repo Memory с фактической реализацией зафиксировано в отчёте v0.4 — сам
контракт подтверждён контрактными тестами против работающего сервиса.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- grep: {path: src/control_plane/infrastructure/context_provider/base.py, pattern: '^class ContextProvider\(Protocol\)'}
  repo: control-plane
- grep: {path: src/control_plane/infrastructure/context_provider/http.py, pattern: '"/api/memory/observations:batch"'}
  repo: control-plane
- grep: {path: src/control_plane/config.py, pattern: 'context_provider: str = "none"'}
  repo: control-plane
- file: tests/contract/test_memory_contract.py
  repo: control-plane
- absent: {path: "src/**/*.py", pattern: '^\s*(import|from)\s+(memory_service|company_brain|apache_age|age)\b'}
  repo: control-plane
```
