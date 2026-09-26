# ADR-0020. Ревизии артефактов: supersedes-цепочка

Статус: принято (2026-08-11, v0.3)

## Контекст

v0.2 документировала отсутствие ревизий. Нужен audit-friendly путь
`draft v1 → draft v2 → final` без mutable document store. Кандидаты:
`supersedes_artifact_id` или пара `logical_artifact_id` + `revision`.

## Решение

Минимальный вариант — nullable self-FK `artifacts.supersedes_artifact_id`
(в пределах tenant, проверяется командой). Новая ревизия — новый append-only
артефакт, ссылающийся на вытесняемый; старые записи неизменны. Событие
`artifact.created` несёт `supersedesArtifactId`.

## Обоснование

Одна колонка выражает lineage без новой сущности и без счётчиков, которые
пришлось бы сериализовывать. Цепочка восстанавливается прямым обходом; head
определяется отсутствием ссылающихся записей.

## Последствия

- Сервер не запрещает двум артефактам вытеснить один и тот же (ветвление
  ревизий) — допустимо и остаётся видимым в аудите.
- «Текущая версия» — derived-понятие читателя; materialized-указатель можно
  добавить позже без миграции данных.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

```conformance
- grep: {path: src/control_plane/infrastructure/db/models.py, pattern: 'supersedes_artifact_id: Mapped\[uuid\.UUID \| None\] = mapped_column\(ForeignKey\("artifacts\.id"\)\)'}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/artifacts.py, pattern: '"supersedesArtifactId": \('}
  repo: control-plane
```
