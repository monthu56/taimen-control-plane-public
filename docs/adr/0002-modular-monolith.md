# ADR-0002. Модульный монолит вместо микросервисов

Статус: принято (2026-08-11)

## Контекст

Домены сервиса (principals, sessions, tasks, claims, events) сильно связаны
транзакционно: захват задачи атомарно меняет task + claim + journal + outbox.

## Решение

Одна кодовая база, слои `api / application / domain / infrastructure`, два
процесса деплоя: HTTP API и background worker. Никаких сетевых границ между
доменами.

## Обоснование

- Ключевые операции требуют одной транзакции над несколькими агрегатами —
  в микросервисах это saga/2PC, то есть на порядок больше сложности без выгоды.
- Один репозиторий, один CI, одна миграционная цепочка.
- Слоистая структура сохраняет возможность выделения сервисов позже: границы
  модулей уже проведены, общение — через application-команды.

## Последствия

- Масштабирование — репликами целого API-процесса (stateless, состояние в БД).
- Worker деплоится отдельно, но из того же образа.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- file: src/control_plane/api/v1/router.py
  repo: control-plane
- file: src/control_plane/application/commands/claims.py
  repo: control-plane
- file: src/control_plane/domain/enums.py
  repo: control-plane
- file: src/control_plane/infrastructure/db/models.py
  repo: control-plane
- grep: {path: docker-compose.yml, pattern: 'command: python -m control_plane\.worker$'}
  repo: control-plane
```
