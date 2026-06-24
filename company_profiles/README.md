# Company Profiles

Папка хранит словари и правила матчинга по компаниям.

Структура:

- `company_profiles/default/profile.json` — базовая библиотека слов и правил.
- `company_profiles/<company-id>/profile.json` — отдельный профиль компании.

Правила:

- Если профиль создан через интерфейс, он сохраняется сюда же.
- Если `inherit_default = true`, профиль расширяет базовую библиотеку `default`.
- Технические правки можно делать напрямую в `profile.json`.

Минимальный формат:

```json
{
  "profile_id": "acme",
  "company_name": "ACME",
  "description": "Custom terms",
  "inherit_default": true,
  "matching": {
    "manual_mappings": {},
    "name_keywords": {},
    "table_name_keywords": {},
    "intent_keywords": {},
    "date_intent_keywords": {},
    "flag_name_map": {},
    "column_bonus_rules": [],
    "table_choice_rules": [],
    "table_bias_rules": [],
    "transliteration_map": {}
  }
}
```
