# Multi-Company Roadmap

Цель: сделать нормальный multi-company режим без переписывания алгоритма матчинга с нуля.

## Phase 1

- Вынести словари и бизнес-эвристики из кода в `company_profiles/<company-id>/profile.json`.
- Добавить выбор профиля в UI и проброс `profile_id` в Step 1.
- Сохранять изменения профиля из интерфейса прямо в репозиторий проекта.

## Phase 2

- Разделить историю маппинга не только по Excel-файлу, но и по `profile_id`/компании.
- Привязать run-state, override, migration history и AI context к профилю компании.
- Хранить метаданные профиля рядом с результатами `mapping_json`.

## Phase 3

- Добавить режим наследования профилей: `default -> industry -> company`.
- Вынести дополнительные правила в конфиг:
  - required column heuristics
  - bool/date/id intent dictionaries
  - value normalization policies
  - table selection bias rules

## Phase 4

- Сделать полноценный company workspace:
  - отдельные папки импорта/истории
  - собственные default schemas
  - изоляция AI context и connector settings

## Technical note

Алгоритм матчинга должен оставаться общим:

- сравнение по значениям
- сравнение по типам данных
- profile-aware name matching
- profile-aware table bias

То есть переносимость строится не на форке алгоритма под каждую компанию, а на конфигурации и изоляции контекста.
