# Mining Tax — Alliance Auth Plugin

Django-App für Alliance Auth zur Verwaltung von Mining-Steuern in EVE Online.

## Features
- Persönliches Mining-Dashboard (Ledger-Sync via ESI)
- Alliance-weite Abrechnung nach Corp
- Steuersätze pro Erz-Kategorie (R4/R8/R16/R32/R64/Ice/Ore)
- Moon Rentals (steuerfrei für mietende Corps)
- Steuerfreie Event-Monde
- PDF-Export pro Corp + ZIP aller Corps
- Permissions: `basic_access`, `mining_officer`, `admin_access`

## Installation
1. App in `INSTALLED_APPS` eintragen: `'miningtax'`
2. `python manage.py migrate miningtax`
3. Permissions im Alliance Auth Admin zuweisen

## Abhängigkeiten
- alliance-auth
- django-esi
- reportlab
