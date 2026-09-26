# ADR-0065: Аварийный ключ (break-glass) — вход в Control Plane при недоступном IAM

Статус: Accepted (2026-09-25), TASK-000341.

Контекст: ADR-0012 (IAM как единственный источник identity), окно совместимости
legacy-ключей (`CP_LEGACY_API_KEYS_ENABLED`), bootstrap суперпроекта отзывает
единственный legacy-ключ установки.

## Контекст

После перехода на IAM-only (`CP_LEGACY_API_KEYS_ENABLED=false`) вход в Control
Plane идёт только через access token IAM. Переключатель окна считался
аварийным выходом: вернуть `true`, перезапустить API — и войти legacy-ключом.
На деле он ничего не даёт: bootstrap отзывает legacy-ключ, а выпустить новый
можно только через API, в который войти нечем. Там, где ключ намеренно не
отозван, открытие окна оживляет **все** неотозванные legacy-ключи установки —
вход шире, чем нужен в аварию.

## Решение

1. **Аварийный ключ — отдельный вид legacy-ключа.** Формат тот же
   (`cp_<prefix>_<secret>`, в базе только хэш), но префикс начинается с `bg`.
   Обычный префикс — шестнадцатеричный, `g` в нём не встречается, поэтому метка
   не требует ни колонки, ни миграции и не может возникнуть случайно.
2. **Выпуск — только из shell на хосте.** API для выпуска нет. Команда
   выполняется внутри контейнера `control-plane-api` и пишет в базу напрямую:

       python -m control_plane.break_glass issue --principal <uuid> --ttl 3600 \
           --reason "IAM недоступен, восстанавливаем"
       python -m control_plane.break_glass revoke

   Граница доверия — доступ к хосту (`docker compose exec`), тот же, что даёт
   доступ к базе и секретам. Ключ печатается один раз в stdout.
3. **Ограничения выпуска.** Только активный principal вида `human` (ключ
   администратора без человека за ним — ровно то, чего аварийный путь не должен
   порождать); права — `admin`; срок жизни обязателен, 60 с …
   `CP_BREAK_GLASS_MAX_TTL_SECONDS` (по умолчанию 4 часа, по умолчанию ключ
   живёт 1 час); причина обязательна (до 500 символов).
4. **Аутентификация.** Аварийный ключ принимается и при закрытом окне
   legacy-ключей — он существует ровно для момента, когда IAM недоступен.
   Обычные legacy-ключи при закрытом окне по-прежнему отвергаются. Ключ с `bg`
   без срока или со сроком длиннее разрешённого (например, продлённый правкой
   базы) не принимается. `CP_BREAK_GLASS_ENABLED=false` выключает и выпуск, и
   приём уже выпущенных ключей.
5. **Аудит.** Выпуск — событие `api_key.break_glass_issued` (principal, префикс,
   права, срок, причина, `issuedBy` = `BREAK_GLASS_OPERATOR` или пользователь
   процесса плюс hostname контейнера), `actor_id` пуст: выпускает не principal,
   а хост. Отзыв — `api_key.revoked` с `breakGlass: true`. Сам ключ в событие не
   попадает.
6. **Выход из аварии.** Как только IAM поднят — `revoke`: отзываются все живые
   аварийные ключи всех tenant'ов.

## Последствия

- При недоступном IAM владелец хоста входит в Control Plane за одну команду, без
  перезапуска API и без открытия окна для старых ключей.
- Компрометация хоста уже даёт доступ к базе; аварийный ключ новой поверхности
  не добавляет, но делает вход видимым в журнале.
- Установка, которой аварийный вход не нужен, выключает его
  `CP_BREAK_GLASS_ENABLED=false`.

## Conformance

```conformance
- grep: {path: src/control_plane/infrastructure/auth/api_keys.py, pattern: 'def generate_break_glass_key'}
- grep: {path: src/control_plane/application/commands/break_glass.py, pattern: 'event_type="api_key\.break_glass_issued"'}
- grep: {path: src/control_plane/infrastructure/auth/service.py, pattern: 'is_break_glass_prefix'}
- grep: {path: src/control_plane/break_glass.py, pattern: 'def main'}
```
