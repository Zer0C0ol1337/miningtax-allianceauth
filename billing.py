from decimal import Decimal

from django.utils import timezone

from .models import OreCategory, TaxRate, FleetSession, AllianceMoon, MoonRental


# Default-Steuersatz, falls für eine Kategorie noch kein TaxRate-Eintrag existiert
DEFAULT_TAX_RATE = Decimal('10.00')


# Ermittelt die Erz-Kategorie (z.B. "R16") für einen type_id. Fällt auf "Default" zurück,
# wenn das Erz noch nicht in OreCategory eingetragen ist.
def get_ore_category(type_id):
    try:
        return OreCategory.objects.get(type_id=type_id).category
    except OreCategory.DoesNotExist:
        return 'Default'


# Holt den aktuellen Steuersatz für eine Kategorie. WICHTIG: hier KEIN "or" / Python-Truthy-Check
# auf den Decimal-Wert verwenden — ein Steuersatz von 0.00 ist ein gültiger, bewusst gesetzter Wert
# (z.B. für Mining-Officer-Event-Erze) und darf nicht versehentlich durch den Default ersetzt werden.
# (Das war im alten Node-Projekt ein konkreter Bug: `taxRate || 10` behandelte 0 als falsy.)
def get_tax_rate(category):
    try:
        rate_obj = TaxRate.objects.get(ore_category=category)
        return rate_obj.tax_rate  # bleibt 0.00, falls explizit so gesetzt
    except TaxRate.DoesNotExist:
        return DEFAULT_TAX_RATE


# Prüft, ob ein Ledger-Eintrag wegen einer aktiven Fleet-Session von der Abrechnung
# ausgeschlossen werden soll (z.B. "Ice Fleet Montag schließt Ice für 4h aus").
def is_excluded_by_fleet_session(entry, ore_category):
    # entry.date ist ein reines Datum (kein Zeitpunkt). Für den Vergleich mit den
    # zeitzonenbewussten start_time/end_time wandeln wir es in einen "Tagesanfang"-Zeitpunkt
    # in der aktuellen Django-Zeitzone um — vermeidet die "naive datetime" Warnung.
    entry_datetime = timezone.make_aware(
        timezone.datetime.combine(entry.date, timezone.datetime.min.time())
    )

    matching_sessions = FleetSession.objects.filter(
        exclude_from_billing=True,
        start_time__lte=entry_datetime,
        end_time__gte=entry_datetime,
    )
    for session in matching_sessions:
        if session.ore_type_id and session.ore_type_id == entry.type_id:
            return True
        if session.ore_category and session.ore_category == ore_category:
            return True
        if not session.ore_type_id and not session.ore_category:
            return True  # Fleet ohne Einschränkung schließt alles im Zeitraum aus
    return False


# Prüft, ob der Eintrag von einem steuerfreien Alliance-Event-Mond stammt (exakter Match
# auf solar_system_name, genau wie im Node-Projekt nach dem Structure-Name-Lookup gelöst).
def is_excluded_by_alliance_moon(entry):
    if not entry.solar_system_name:
        return False
    # __icontains statt __iexact: der Ledger speichert den vollen Struktur-Namen
    # (z.B. "P9F-ZG - P8 - M10 - PRIVATE EE"), wir pflegen aber nur den System-Namen
    # ("P9F-ZG") in AllianceMoon — das war im alten Node-Projekt ein konkreter Stolperstein.
    for moon in AllianceMoon.objects.filter(is_tax_free=True):
        if moon.solar_system_name and moon.solar_system_name.lower() in entry.solar_system_name.lower():
            return True
    return False


# Prüft, ob die Corp diesen Mond/diese Struktur gemietet hat (MoonRental) — dann ist
# Mining dort für diese Corp ebenfalls steuerfrei.
def is_excluded_by_moon_rental(entry, corporation):
    if not entry.solar_system_name or not corporation:
        return False
    return MoonRental.objects.filter(
        corporation=corporation,
        active=True,
        structure_name__iexact=entry.solar_system_name
    ).exists()


# Berechnet für einen einzelnen Ledger-Eintrag, ob er steuerpflichtig ist und wie viel
# Steuer anfällt. Gibt ein dict zurück, statt mehrere Rückgabewerte — leichter erweiterbar.
def calculate_entry_tax(entry, corporation=None):
    category = get_ore_category(entry.type_id)

    excluded = (
        is_excluded_by_fleet_session(entry, category)
        or is_excluded_by_alliance_moon(entry)
        or is_excluded_by_moon_rental(entry, corporation)
    )

    if excluded:
        return {
            'category': category,
            'tax_rate': Decimal('0.00'),
            'tax_amount': Decimal('0.00'),
            'excluded': True,
        }

    tax_rate = get_tax_rate(category)
    tax_amount = entry.total_value * (tax_rate / Decimal('100'))

    return {
        'category': category,
        'tax_rate': tax_rate,
        'tax_amount': tax_amount,
        'excluded': False,
    }


# Berechnet die komplette Alliance-Abrechnung für einen Monat: alle Corps, alle Mitglieder,
# Steuer pro Kategorie. Entspricht der /alliance/billing Route aus dem alten Node-Projekt.
def calculate_alliance_billing(year, month):
    from allianceauth.eveonline.models import EveCorporationInfo
    from .models import MiningLedgerEntry

    entries = MiningLedgerEntry.objects.filter(
        date__year=year, date__month=month
    ).select_related('character', 'character__character_ownership__user')

    corps_data = {}
    alliance_totals = {'mined': Decimal('0'), 'tax': Decimal('0')}

    for entry in entries:
        corp = entry.character.corporation_id
        corp_name = entry.character.corporation_name or 'Unbekannt'

        tax_info = calculate_entry_tax(entry, corporation=_get_corp_info(corp))

        if corp not in corps_data:
            corps_data[corp] = {
                'corp_name': corp_name,
                'total_mined': Decimal('0'),
                'total_tax': Decimal('0'),
                'members': {},
                'categories': {},
            }

        corp_entry = corps_data[corp]
        corp_entry['total_mined'] += entry.total_value
        corp_entry['total_tax'] += tax_info['tax_amount']

        char_name = entry.character.character_name
        if char_name not in corp_entry['members']:
            corp_entry['members'][char_name] = {'mined': Decimal('0'), 'tax': Decimal('0')}
        corp_entry['members'][char_name]['mined'] += entry.total_value
        corp_entry['members'][char_name]['tax'] += tax_info['tax_amount']

        cat = tax_info['category']
        if cat not in corp_entry['categories']:
            corp_entry['categories'][cat] = {'value': Decimal('0'), 'tax': Decimal('0'), 'rate': tax_info['tax_rate']}
        corp_entry['categories'][cat]['value'] += entry.total_value
        corp_entry['categories'][cat]['tax'] += tax_info['tax_amount']

        alliance_totals['mined'] += entry.total_value
        alliance_totals['tax'] += tax_info['tax_amount']

    return {'corps': corps_data, 'totals': alliance_totals}


# Kleine Cache-Helper-Funktion, damit calculate_entry_tax() das EveCorporationInfo-Objekt
# bekommt, das es für den Moon-Rental-Check braucht (statt nur die ID).
_corp_cache = {}


def _get_corp_info(corp_id):
    from allianceauth.eveonline.models import EveCorporationInfo
    if corp_id in _corp_cache:
        return _corp_cache[corp_id]
    try:
        corp = EveCorporationInfo.objects.get(corporation_id=corp_id)
    except EveCorporationInfo.DoesNotExist:
        corp = None
    _corp_cache[corp_id] = corp
    return corp