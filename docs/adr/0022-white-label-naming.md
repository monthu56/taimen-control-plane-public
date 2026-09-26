# ADR-0022: White-label нейтрализация имён (v0.4)

Статус: Принято (v0.4)

## Контекст

До v0.4 клиентские пакеты, протокольный идентификатор, MCP-инструменты, CLI
и env-переменные несли продуктовое кодовое имя «taimen». Control Plane
позиционируется как product-neutral координационная платформа, пригодная для
white-label дистрибуции: кодовое имя в core-идентичности недопустимо.

## Решение

Канонические нейтральные имена; кодовое имя остаётся только как
документированный legacy-вход:

| Было | Стало | Legacy |
|---|---|---|
| `taimen_client` | `control_plane_client` | shim-пакет с DeprecationWarning |
| `TaimenClient`/`TaimenError` | `ControlPlaneClient`/`ControlPlaneError` | алиасы сохранены |
| `taimen`/`taimen-mcp`/`taimen-agent` (scripts) | `control-plane`/`control-plane-mcp`/`control-plane-agent` | старые имена — алиасы тех же entrypoint'ов |
| MCP server `taimen`, tools `taimen_*` | `control-plane`, tools `cp_*` | нет (реregистрация MCP — локальная операция) |
| `taimen-harness` | `control-harness` | сервер объявляет `legacyNames` |
| `TAIMEN_*` env | `CONTROL_PLANE_*` | старые читаются как fallback |
| Keychain `ai.taimen.api-key`, `~/.config/taimen/`, `.taimen/` | `control-plane.api-key`, `~/.config/control-plane/`, `.control-plane/` | старые локации ЧИТАЮТСЯ; записи — только в новые |

Не переименовано:

- **Alembic-миграции и исторические ADR (0016, 0017)** — неизменяемая
  история; кодовое имя в них — historical product codename.
- **Схема БД** — брендинга в ней не было.

Гард: `tests/unit/test_branding.py` — repo-wide скан с явным allowlist;
каждый элемент allowlist существует по документированной причине.

## Последствия

Публичный SDK/CLI/MCP нейтральны; действующие установки продолжают работать
через fallback-чтение legacy-локаций. Полное удаление legacy-входов —
отдельное решение будущего релиза.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

Legacy-входы (shim `taimen_client`, fallback-чтение старых локаций) сняты ADR-0040, поэтому пробы проверяют только нейтральные канонические имена и гард брендинга.

```conformance
- file: tests/unit/test_branding.py
  repo: control-plane
- grep: {path: tests/unit/test_branding.py, pattern: 'def test_codename_absent_outside_allowlist\('}
  repo: control-plane
- grep: {path: client/src/control_plane_client/client.py, pattern: '^class ControlPlaneClient'}
  repo: control-plane
- grep: {path: src/control_plane_mcp/server.py, pattern: 'name="control-plane",'}
  repo: control-plane
- grep: {path: src/control_plane/domain/enums.py, pattern: 'HARNESS_PROTOCOL_NAME = "control-harness"'}
  repo: control-plane
```
