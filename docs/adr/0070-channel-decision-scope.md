# ADR-0070: Решение из канала — scope `control-plane:decide`, `purpose_ref`, `channel` в событии

Статус: Accepted (2026-09-25), фича `notifications`, задача N005 (TASK-000414).
Spec/plan — `specs/notifications/` суперпроекта (FR-006, FR-007, SC-004);
дизайн одобрен владельцем в TASK-000408. Платформенное решение — ADR IAM
«Канал как способ входа» (N001); токен выпускает IAM (N004,
`POST …/channel-assertions:exchange`); вызывает ядро адаптер канала в
notification-service (N007).

Контекст: ADR-0018 (approvals и право решать), ADR-0055 (режимы
`authorize`: local/shadow/policy), ADR-0061 (исходы решения выполняются с
полномочиями решившего), ADR-0068 (payload `approval.*` v2 с полем
`channel`); IAM-7 (scope токена — потолок прав binding).

## Контекст

Человек решает approval кнопкой в Telegram, не открывая веб-консоль. Адаптер
канала обменивает в IAM assertion «этот аккаунт сейчас решил» на короткий
токен от имени привязанного человека:

```text
aud = control-plane, scope = scope_ceiling = [control-plane:decide]
principal_type = human, acr = channel:telegram, amr = [channel:telegram]
purpose_ref = <что решается>, credential_id = id привязки, срок 60 с
```

Это вход по более слабому фактору, чем веб-сессия, и токен проходит через
сторонний сервис. Поэтому он должен уметь ровно одно — решить тот approval,
ради которого выпущен. Ядро до этого решения не знало ни scope `decide`
(PEP отказывал бы такому токену), ни `purpose_ref`, ни канала входа.

## Решение

1. **Scope `control-plane:decide`** входит в набор scope ядра. Из прав
   binding он оставляет только `approvals.decide`: право должно быть у
   binding, само по себе scope ничего не даёт (без права — `403
   permission_denied`). Если на том же токене есть другие scope
   (read/write/admin), `decide` всё равно побеждает: токен одного решения
   остаётся токеном одного решения. Комментарий решения — это поле тела
   `:approve|:reject`, отдельного права на него нет.
2. **`purpose_ref` = `approval:<uuid>`** — ссылка на ресурс в той же форме,
   что `ResourceRef.key` ядра. Токен со scope `decide` обязан её нести, иначе
   `403 purpose_ref_required`. Токен принимается только на
   `POST /api/v1/approvals/{id}:approve` и `:reject` с тем же `{id}`; любой
   другой запрос, включая чтение этого же approval, `:cancel`, чужой
   approval и `WS /events/ws`, получает `403 outside_purpose`. Проверка идёт
   при аутентификации, до чтения данных, поэтому отказ не говорит, что
   существует. Команда `decide_approval` повторяет проверку по
   `AuthContext.purpose_ref` (защита в глубину).
3. **Потолок держится во всех режимах `authorize`.** В режиме `policy` PDP
   решает по человеку и не знает потолка токена. Поэтому для контекста с
   `purpose_ref` `authorize` сначала требует плоское право (`require`), а
   затем спрашивает PDP. В режимах local/shadow так было и раньше.
4. **`acr=channel:<имя>` → `AuthContext.channel`** (имя
   `^[a-z0-9][a-z0-9_-]{0,39}$`, иначе канала нет). В событиях
   `approval.approved` / `approval.rejected` поле `channel` несёт это имя
   (`telegram`); у прямого вызова API — `null`. Схема события не меняется:
   поле объявлено в v2 (ADR-0068).
5. **`Idempotency-Key` обязателен** для любой записи контекстом с
   `purpose_ref`: без него `422 idempotency_key_required`. Адаптер ставит
   ключом id callback'а. Повторное нажатие или повторная доставка вебхука
   становятся replay (`Idempotency-Replayed: true`), а не второй попыткой
   решить (которая получила бы `409 approval_already_decided`).

## Последствия

- Утечка токена канала за его 60 секунд открывает одно решение одного
  approval, и только если человек вправе его принять (права binding и
  организационная пригодность ADR-0018).
- **Исходы gate-решения (ADR-0061)** снимают полномочия решившего с
  контекста решения, то есть `{approvals.decide}`. В режимах local/shadow
  действия исхода, которым нужны другие права (записать задачу, вызвать
  скилл), падают на проверке прав. Решение при этом остаётся в силе, заводится
  задача-разбор, и решивший повторяет исход через веб
  (`:replay-outcome` с полной учёткой). Это осознанный выбор: токен канала
  не пишет ничего, кроме решения. Если нужно, чтобы исход решения из канала
  исполнялся с полными правами binding, это отдельное решение владельца
  (например, снимок прав binding, а не токена).
- Для bootstrap: audience `control-plane` в IAM должен разрешать scope
  `control-plane:decide` (N009).

## Contract-проверки

- `tests/integration/test_iam_enforcement.py`: токен канала решает свой
  approval и получает отказ на чтение (approval, задачи, журнал), на запись
  (задача, комментарий, `:cancel`) и на чужой approval. Решение без ключа
  отклоняется, повтор с ключом — replay. Событие несёт `channel=telegram`,
  прямой вызов — `channel=null`. Токен без `purpose_ref`, токен с лишними
  scope и binding без права получают отказ.
- `tests/unit/test_authorizer.py`: контекст с `purpose_ref` не проходит мимо
  своих прав в режимах shadow и policy.

## Conformance

```conformance
- grep: {path: src/control_plane/infrastructure/auth/iam.py, pattern: 'SCOPE_DECIDE = "control-plane:decide"'}
  repo: control-plane
- grep: {path: src/control_plane/infrastructure/auth/iam.py, pattern: 'purpose_ref = decision_purpose\(ctx\.claims, action\) if SCOPE_DECIDE in scopes else None'}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/approvals.py, pattern: '"channel": ctx\.channel'}
  repo: control-plane
- grep: {path: src/control_plane/api/write_flow.py, pattern: 'idempotency_key is None and ctx\.purpose_ref is not None'}
  repo: control-plane
- grep: {path: tests/integration/test_iam_enforcement.py, pattern: 'def test_channel_token_decides_only_its_own_approval'}
  repo: control-plane
```
