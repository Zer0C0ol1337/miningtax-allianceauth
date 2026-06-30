from esi.openapi_clients import ESIClientProvider
from esi.models import Token
from esi.exceptions import HTTPNotModified

from .models import MiningLedgerEntry


# Einmaliger, geteilter ESI-Client für die ganze App (django-esi 9.x / aiopenapi3 Architektur).
# tags begrenzt den Client bewusst auf die Bereiche, die wir brauchen (Mining + Erz-Typnamen),
# das hält den Speicherverbrauch klein, wie von django-esi empfohlen.
esi = ESIClientProvider(
    compatibility_date="2026-06-09",
    ua_appname="EVE Mining Manager Plugin",
    ua_version="1.0",
    tags=['Industry', 'Universe', 'Market'],
)


# Holt ein gültiges Mining-Token für genau diesen Character. django-esi kümmert sich
# automatisch um Token-Refresh über token.valid_access_token().
def _get_mining_token(character):
    token = Token.objects.filter(
        character_id=character.character_id
    ).require_scopes('esi-industry.read_character_mining.v1').require_valid().first()

    if not token:
        raise Exception(f'Kein gültiges Mining-Token für {character.character_name} gefunden')

    return token


# Erz-Typnamen cachen wir einfach per dict im Prozess-Speicher.
_type_name_cache = {}


def _get_type_name(type_id):
    if type_id in _type_name_cache:
        return _type_name_cache[type_id]
    try:
        # public Endpoint, kein Token nötig. WICHTIG: .results() liefert hier eine Liste mit
        # genau einem Objekt zurück, nicht das Objekt direkt — daher result[0].name, nicht result.name.
        result = esi.client.Universe.GetUniverseTypesTypeId(type_id=type_id).results()
        name = result[0].name if result else f'Type {type_id}'
    except Exception:
        name = f'Type {type_id}'
    _type_name_cache[type_id] = name
    return name


# Cache für System-/Struktur-Namen, damit wir nicht für jeden Eintrag erneut nachfragen.
_location_name_cache = {}


def _get_location_name(location_id, token):
    if location_id is None:
        return ''
    if location_id in _location_name_cache:
        return _location_name_cache[location_id]

    name = f'Unbekannt ({location_id})'

    # Struktur-IDs (Refineries auf Monden etc.) sind sehr große Zahlen, echte Solar-System-IDs
    # liegen im niedrigen Millionenbereich — gleiche Unterscheidung wie im alten Node-Projekt.
    if location_id > 100_000_000:
        try:
            structure = esi.client.Universe.GetUniverseStructuresStructureId(
                structure_id=location_id,
                token=token
            ).results()
            # auch hier: Liste mit einem Objekt, nicht das Objekt direkt
            name = structure[0].name if structure else name
        except Exception:
            # Kein Zugriff auf die Struktur (z.B. fremde Corp-Refinery) — Platzhalter behalten
            name = f'Mond-Struktur ({location_id})'
    else:
        try:
            system = esi.client.Universe.GetUniverseSystemsSystemId(system_id=location_id).results()
            name = system[0].name if system else name
        except Exception:
            pass

    _location_name_cache[location_id] = name
    return name


# Synct den persönlichen Mining-Ledger eines Characters von ESI in unsere Datenbank.
def sync_character_mining(character):
    token = _get_mining_token(character)

    try:
        # Operationsname laut Spec: GetCharactersCharacterIdMining (PascalCase)
        # Wichtig: hier das Token-Objekt selbst übergeben (nicht token.valid_access_token() als String) —
        # die Bibliothek liest intern token.character_id für ihr Rate-Limiting.
        ledger = esi.client.Industry.GetCharactersCharacterIdMining(
            character_id=character.character_id,
            token=token
        ).results()
    except HTTPNotModified:
        # ESI sagt "seit dem letzten Abruf hat sich nichts geändert" — kein Fehler,
        # einfach 0 neue Einträge zurückgeben statt die Exception hochzureichen.
        return 0

    saved = 0
    for entry in ledger:
        # aiopenapi3 gibt strukturierte Objekte zurück (Attribut-Zugriff wie entry.type_id),
        # nicht Dictionaries wie der alte Bravado-Client (entry['type_id']) — daher hier
        # konsequent Punkt-Notation statt eckiger Klammern.
        type_name = _get_type_name(entry.type_id)
        location_id = getattr(entry, 'solar_system_id', None)
        location_name = _get_location_name(location_id, token)

        MiningLedgerEntry.objects.update_or_create(
            character=character,
            date=entry.date,
            type_id=entry.type_id,
            solar_system_id=location_id,
            defaults={
                'type_name': type_name,
                'quantity': entry.quantity,
                'solar_system_name': location_name,
            }
        )
        saved += 1

    return saved


# Synct alle Characters mit gültigem Mining-Scope-Token (für den täglichen Hintergrund-Job).
def sync_all_characters():
    from allianceauth.eveonline.models import EveCharacter

    character_ids = Token.objects.filter(
        scopes__name='esi-industry.read_character_mining.v1'
    ).values_list('character_id', flat=True).distinct()

    total_synced = 0
    for char_id in character_ids:
        try:
            character = EveCharacter.objects.get(character_id=char_id)
            total_synced += sync_character_mining(character)
        except Exception as e:
            print(f'⚠️ Sync Fehler für Character {char_id}: {e}')

    return total_synced


# Jita Region ID (10000002) — Standardquelle für Marktpreise, mit Amarr (10000043) als Fallback
JITA_REGION_ID = 10000002
AMARR_REGION_ID = 10000043

# Preise cachen wir pro type_id für die Dauer eines Sync-Laufs, damit wir nicht für jeden
# einzelnen Ledger-Eintrag erneut bei ESI nachfragen müssen.
_price_cache = {}


def _get_jita_sell_price(type_id):
    if type_id in _price_cache:
        return _price_cache[type_id]

    price = 0
    for region_id in (JITA_REGION_ID, AMARR_REGION_ID):
        try:
            orders = esi.client.Market.GetMarketsRegionIdOrders(
                region_id=region_id,
                type_id=type_id,
                order_type='sell'
            ).results()
            if orders:
                # Attribut-Zugriff (.price), nicht dict-Zugriff — gleiches Muster wie beim Mining-Ledger
                price = min(o.price for o in orders)
                break
        except Exception:
            continue

    _price_cache[type_id] = price
    return price


# Aktualisiert price_per_unit und total_value für alle Ledger-Einträge, die noch keinen Preis haben.
# Wird täglich nach dem Mining-Sync aufgerufen.
def update_market_prices():
    entries = MiningLedgerEntry.objects.filter(price_per_unit=0)
    type_ids = entries.values_list('type_id', flat=True).distinct()

    updated = 0
    for type_id in type_ids:
        price = _get_jita_sell_price(type_id)
        if price <= 0:
            continue

        matching_entries = entries.filter(type_id=type_id)
        for entry in matching_entries:
            entry.price_per_unit = price
            entry.total_value = price * entry.quantity
            entry.save(update_fields=['price_per_unit', 'total_value'])
            updated += 1

    return updated