# ADR-0006. API-key аутентификация в MVP

Статус: Superseded — [ADR-0053](0053-iam-identity-source-and-binding-api.md) (2026-09-12): credential по умолчанию — токен IAM, ключ `cp_` остаётся bootstrap- и аварийным путём; формат и хранение ключа в силе. Принято 2026-08-11.

## Контекст

Клиенты сервиса — агенты, сервисы и CLI-инструменты людей. Нужен механизм,
который прост для machine-to-machine, отзываем и не хранит секретов в открытом
виде. Альтернативы: OAuth2/OIDC, mTLS, JWT.

## Решение

Bearer API-ключи формата `cp_<prefix>_<secret>`:

- хранится только SHA-256 хэш; полный ключ возвращается ровно один раз при
  создании;
- lookup по индексированному `key_prefix`, сравнение хэшей — constant-time;
- у ключа: список permissions, срок действия, отзыв (`revoked_at`),
  `last_used_at` (обновляется с троттлингом);
- ключ admin может выпустить только другой ключ admin (нет эскалации);
- в логах ключи фигурируют только по префиксу.

Bootstrap первого tenant защищён отдельным статическим токеном из окружения и
работает только пока в системе нет ни одного tenant.

## Обоснование

- OAuth/OIDC требует внешнего IdP и interactive-флоу — лишнее для
  agent-to-server MVP; JWT без ротации усложняет отзыв.
- SHA-256 достаточен для высокоэнтропийного секрета (32 байта из CSPRNG) —
  затраты на bcrypt/argon2 не нужны, перебор невозможен по определению.

## Последствия

- Ротация — создание нового ключа + отзыв старого (без grace-механики).
- OIDC для людей — кандидат на v2; permissions уже отделены от механизма
  аутентификации, замена не потребует переписывать авторизацию.

## Conformance

Пробы для `adr.conformance_check` (пилот «саморазработка»):

Ключ как основной credential вытеснен ADR-0053; пробы проверяют ту часть решения, что осталась в силе, — формат и хранение ключа, запрет эскалации admin и bootstrap-токен.

```conformance
- grep: {path: src/control_plane/infrastructure/auth/api_keys.py, pattern: 'full_key = f"\{_KEY_TAG\}_\{prefix\}_\{secret\}"'}
  repo: control-plane
- grep: {path: src/control_plane/infrastructure/auth/api_keys.py, pattern: 'hashlib\.sha256\(full_key\.encode\(\)\)\.hexdigest\(\)'}
  repo: control-plane
- grep: {path: src/control_plane/infrastructure/auth/api_keys.py, pattern: 'hmac\.compare_digest\(hash_api_key\(full_key\), stored_hash\)'}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/principals.py, pattern: 'Only an admin key can create another admin key'}
  repo: control-plane
- grep: {path: src/control_plane/application/commands/bootstrap.py, pattern: '"already_bootstrapped"'}
  repo: control-plane
```
