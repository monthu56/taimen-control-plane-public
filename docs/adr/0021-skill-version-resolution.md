# ADR-0021. Резолюция версий skills: name / name@version, без пакетного солвера

Статус: принято (2026-08-11, v0.3)

## Контекст

v0.2 матчила skills только по имени (любая версия). Нужны точные версии для
безопасной эволюции `github.create_pr v1 → v2` — но не полноценный package
solver и не диапазоны semver.

## Решение

- Требование задачи: `name` (v0.2-семантика: удовлетворяет любая назначенная
  не-disabled версия) или `name@version` (точная версия; хранится
  `skill_id` + флаг `task_requirements.skill_exact`).
- Дефолт-резолюция `name` при создании требования: свежайшая **active**
  версия, fallback — свежайшая не-disabled (deprecated-only skill продолжает
  работать).
- Disabled-версия никогда не участвует: не удовлетворяет требования, не
  резолвится в run actions (`409 skill_unavailable`), помечается
  неисполнимой в контексте.
- Смена версии у существующих назначений — только явная (assign новой версии
  принципалу / новое требование); тихого переключения нет: requirement
  хранит конкретный `skill_id`, зафиксированный при создании.
- Исполнимость на harness: пересечение назначенных skills с заявленными
  `skills.protocol.*` capabilities сессии (`executable` в
  context/RunContext); сессия без заявленных протоколов видит всё.

## Обоснование

Две формы записи покрывают реальные сценарии (пин и «любая рабочая версия»)
без constraint-языка. SkillDefinition/SkillVersion-раскол отложен: текущая
модель `(name, version)`-строк уже уникальна и достаточна.

## Последствия

- `requirements.skills: ["deploy@2.0.0"]` в API; ответ `GET
  /tasks/{ref}/requirements` включает конкретную версию.
- Диапазоны версий (`^2`, `>=1.4`) сознательно не поддерживаются — при
  необходимости это отдельное решение поверх той же модели.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

Ошибка `409 skill_unavailable` при записи action заменена на `tool_not_authorized` решением ADR-0045 — пробы проверяют остальное решение (точный пин, дефолт-резолюция, исключение disabled, отсутствие диапазонов).

```conformance
- grep: {path: src/control_plane/infrastructure/db/models.py, pattern: 'skill_exact: Mapped\[bool\]'}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/eligibility.py, pattern: 'skill_exact=version is not None'}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/eligibility.py, pattern: 'the newest non-disabled one \(a deprecated-only skill still works\)'}
  repo: control-plane
- grep: {path: src/control_plane/application/queries/harness.py, pattern: 'async def resolve_executable_skills\('}
  repo: control-plane
- absent: {path: src/control_plane/application/commands/eligibility.py, pattern: '(?i)semver|version_range|\^\d'}
  repo: control-plane
```
