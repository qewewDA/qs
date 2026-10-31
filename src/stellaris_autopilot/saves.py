"""Read textual Stellaris saves without extracting untrusted ZIP members."""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Iterator
import zipfile

from .pdx import PDXError, PDXNode, loads


class SaveError(ValueError):
    """The save is unreadable, ambiguous, or outside the supported text format."""


def _node(value: Any) -> PDXNode:
    return value if isinstance(value, PDXNode) else PDXNode()


def _plain(value: Any) -> Any:
    """Small metadata/name structures; never expose the entire raw save."""
    if isinstance(value, PDXNode):
        # Name structures commonly use { key="LOC_KEY" variables={...} }.
        return str(value.get('key', value.get('name', '')))
    return value


def _structure(value: Any) -> Any:
    if not isinstance(value, PDXNode):
        return value
    named = list(value.items())
    anonymous = list(value.values())
    if not named:
        return [_structure(item) for item in anonymous]
    result: dict[str, Any] = {}
    for key, item in named:
        if len(value.get_all(key)) > 1:
            result[key] = [_structure(entry) for entry in value.get_all(key)]
        else:
            result[key] = _structure(item)
    if anonymous:
        result['_values'] = [_structure(item) for item in anonymous]
    return result


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _objects(value: Any) -> Iterator[tuple[str, PDXNode]]:
    if not isinstance(value, PDXNode):
        return
    for key, item in value.items():
        if isinstance(item, PDXNode):
            yield key, item


def _player_ids(players: Any) -> set[str]:
    ids: set[str] = set()
    if not isinstance(players, PDXNode):
        return ids
    # Support both player={country=0} and player={{country=0}} / player={0={...}}.
    country = players.get('country')
    if country is not None:
        if isinstance(country, (str, int)) and str(country).isascii() and str(country).isdecimal():
            ids.add(str(country))
        return ids
    # Player account IDs and name variables are not empire IDs. Recurse only
    # through actual list/indexed containers, never arbitrary player metadata.
    for key, value in players.entries:
        if isinstance(value, PDXNode) and (key is None or key.isdecimal()):
            ids.update(_player_ids(value))
    return ids


def _resources(country: PDXNode) -> tuple[dict[str, float], str | None]:
    paths = [
        ('modules.standard_economy_module.resources', _node(_node(country.get('modules')).get('standard_economy_module')).get('resources')),
        ('modules.standard_economy_module.resource_stockpile', _node(_node(country.get('modules')).get('standard_economy_module')).get('resource_stockpile')),
        ('resources', country.get('resources')),
        ('resource_stockpile', country.get('resource_stockpile')),
    ]
    for path, value in paths:
        if isinstance(value, PDXNode):
            result = {key: number for key, item in value.items() if (number := _number(item)) is not None}
            if result:
                return result, path
    return {}, None


def _research(country: PDXNode) -> dict[str, str]:
    result: dict[str, str] = {}
    tech_status = _node(country.get('tech_status'))
    for field in ('physics', 'society', 'engineering'):
        queue = _node(tech_status.get(field + '_queue'))
        entries = list(queue.values())
        if not entries:
            entries = [value for _, value in queue.items()]
        if entries and isinstance(entries[0], PDXNode):
            technology = entries[0].get('technology')
            if technology is not None:
                result[field] = str(technology)
    research = _node(country.get('research'))
    for field in ('physics', 'society', 'engineering'):
        value = research.get(field)
        if isinstance(value, PDXNode):
            value = value.get('technology', value.get('tech', value.get('current')))
        if value is not None and not isinstance(value, PDXNode):
            result.setdefault(field, str(value))
    # Older formats use current_physics_research directly on the country.
    for field in ('physics', 'society', 'engineering'):
        value = country.get('current_' + field + '_research')
        if value is not None and not isinstance(value, PDXNode):
            result.setdefault(field, str(value))
    return result


def _budget(country: PDXNode) -> dict[str, Any]:
    budget = _node(country.get('budget'))
    period = 'last_month' if isinstance(budget.get('last_month'), PDXNode) else 'current_month'
    last_month = _node(budget.get(period))

    def sum_leaves(value: Any) -> dict[str, float]:
        result: dict[str, float] = {}
        if not isinstance(value, PDXNode):
            return result
        for key, item in value.items():
            if isinstance(item, PDXNode):
                for resource, amount in sum_leaves(item).items():
                    result[resource] = result.get(resource, 0.0) + amount
            elif (amount := _number(item)) is not None:
                result[key] = result.get(key, 0.0) + amount
        return result

    income = sum_leaves(last_month.get('income'))
    expenses = sum_leaves(last_month.get('expenses'))
    # A missing side is unknown, not a zero expense/income ledger.
    net = ({key: income.get(key, 0.0) - expenses.get(key, 0.0) for key in income.keys() | expenses.keys()}
           if isinstance(last_month.get('income'), PDXNode) and isinstance(last_month.get('expenses'), PDXNode) else {})
    return {'monthly_income': income, 'monthly_expenses': expenses, 'monthly_net_income': net, 'budget_period': period if income or expenses else 'unknown'}


def _owned(root: PDXNode, category: str, country_id: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    collection = root.get(category)
    if category == 'planet' and not isinstance(collection, PDXNode):
        collection = _node(root.get('planets')).get('planet')
    for object_id, value in _objects(collection):
        owner = value.get('owner', value.get('controller'))
        if str(owner) != country_id:
            continue
        item: dict[str, Any] = {'id': object_id}
        for key in ('name', 'owner', 'controller', 'orbit', 'station', 'fleet_power', 'military_power', 'combat', 'size', 'class', 'queue', 'construction_queue', 'current_order', 'order', 'buildings'):
            raw = value.get(key)
            if raw is not None:
                item[key] = _plain(raw) if key == 'name' else _structure(raw)
        position = _node(value.get('coordinate'))
        if position.get('origin') is not None:
            item['system_id'] = str(position.get('origin'))
        result.append(item)
    return result


def _at_war(root: PDXNode, country: PDXNode, country_id: str) -> tuple[bool, bool]:
    declared = country.get('at_war')
    if declared in ('yes', 'no'):
        return declared == 'yes', True
    collections = root.get_all('war')
    if not collections:
        return False, False

    def participant_ids(value: Any) -> tuple[set[str], bool]:
        """Accept ID lists and named/anonymous participant objects.

        An object with ``country`` identifies one participant; its other fields
        are participant metadata. Containers recurse into anonymous and keyed
        objects. Unrecognized nonempty containers cannot establish peace.
        """
        if not isinstance(value, PDXNode):
            identifier = str(value)
            return ({identifier}, True) if identifier.isdecimal() else (set(), False)
        direct = value.get_all('country')
        if direct:
            ids: set[str] = set()
            known = True
            for identifier in direct:
                found, understood = participant_ids(identifier)
                ids.update(found)
                known = known and understood
            return ids, known
        ids = set()
        known = True
        for key, entry in value.entries:
            if key is None or isinstance(entry, PDXNode):
                found, understood = participant_ids(entry)
                ids.update(found)
                known = known and understood
            else:
                known = False
        return ids, known

    all_known = True
    for collection in collections:
        if not isinstance(collection, PDXNode):
            all_known = False
            continue
        for _, war in collection.entries:
            if not isinstance(war, PDXNode):
                all_known = False
                continue
            groups_present: set[str] = set()
            for group, alternatives in (
                ('attackers', ('attackers', 'attacker')),
                ('defenders', ('defenders', 'defender')),
            ):
                for side in alternatives:
                    for side_value in war.get_all(side):
                        groups_present.add(group)
                        ids, known = participant_ids(side_value)
                        if country_id in ids:
                            return True, True
                        all_known = all_known and known
            # Missing sides in an active war may hide the player empire.
            all_known = all_known and groups_present == {'attackers', 'defenders'}
    return False, all_known


def read_save(path: Path, *, country_id: str | None = None, max_bytes: int = 128 * 1024 * 1024) -> dict[str, Any]:
    """Read a ZIP ``gamestate`` or uncompressed text save.

    Supports observed text field layouts, not a guarantee across game versions.
    Missing fields stay empty and are identified in ``metadata.missing_fields``.
    A missing war field yields at_war=False with war_status_known=False: callers
    must check that metadata flag before making peace/war decisions.
    """
    path = Path(path)
    if max_bytes <= 0:
        raise SaveError('max_bytes must be positive')
    try:
        if path.stat().st_size > max_bytes:
            raise SaveError(f'Save exceeds the {max_bytes}-byte input limit')
        if zipfile.is_zipfile(path):
            with zipfile.ZipFile(path) as archive:
                matches = [info for info in archive.infolist() if info.filename == 'gamestate']
                if len(matches) != 1:
                    raise SaveError('ZIP save must contain exactly one gamestate member')
                info = matches[0]
                if info.file_size > max_bytes:
                    raise SaveError(f'Decompressed gamestate exceeds the {max_bytes}-byte limit')
                with archive.open(info) as stream:
                    data = stream.read(max_bytes + 1)
        else:
            with path.open('rb') as stream:
                data = stream.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise SaveError(f'Gamestate exceeds the {max_bytes}-byte limit')
        text = data.decode('utf-8-sig')
        root = loads(text)
    except SaveError:
        raise
    except (OSError, UnicodeError, zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError, PDXError) as exc:
        raise SaveError(f'Cannot read textual Stellaris save {path.name}: {exc}') from exc

    countries = dict(_objects(root.get('country')))
    if not countries:
        raise SaveError('No country map found; this save format is unsupported')
    candidates: set[str] = set()
    for player in root.get_all('player'):
        candidates.update(_player_ids(player))
    if country_id is None:
        if len(candidates) != 1:
            raise SaveError('Player empire is missing or ambiguous; pass an explicit country_id')
        country_id = next(iter(candidates))
    country_id = str(country_id)
    if country_id not in countries:
        raise SaveError(f'Country {country_id!r} is absent from the save')
    country = countries[country_id]
    resources, resource_path = _resources(country)
    research = _research(country)
    fleets = _owned(root, 'fleet', country_id)
    planets = _owned(root, 'planet', country_id)
    at_war, war_known = _at_war(root, country, country_id)
    missing = []
    if not resources:
        missing.append('resources')
    if not research:
        missing.append('research')
    if not isinstance(root.get('fleet'), PDXNode):
        missing.append('fleets')
    if not isinstance(root.get('planet'), PDXNode) and not isinstance(_node(root.get('planets')).get('planet'), PDXNode):
        missing.append('planets')
    if not war_known:
        missing.append('at_war')
    date = root.get('date')
    if date is None:
        raise SaveError('Save has no date; cannot establish a state snapshot')
    return {
        'date': str(date),
        'country_id': country_id,
        'name': str(_plain(country.get('name', ''))),
        'resources': resources,
        'research': research,
        'fleets': fleets,
        'planets': planets,
        'fleets_by_id': {fleet['id']: fleet for fleet in fleets},
        'planets_by_id': {planet['id']: planet for planet in planets},
        'at_war': at_war,
        'metadata': {
            'source': str(path.resolve()),
            'format': 'stellaris-text',
            'game_version': _plain(root.get('version', root.get('savegame_version', 'unknown'))),
            'campaign_id': (str(_node(root.get('galaxy')).get('name')).strip() or None)
            if isinstance(_node(root.get('galaxy')).get('name'), str) else None,
            'player_country_ids': sorted(candidates),
            'resource_path': resource_path,
            'war_status_known': war_known,
            'ironman': root.get('ironman', root.get('ironman_mode', _node(root.get('galaxy')).get('ironman', 'unknown'))),
            'multiplayer': 'yes' if len(candidates) > 1 else root.get('multiplayer', 'unknown'),
            'missing_fields': missing,
            'schema_notice': 'Field layouts vary by Stellaris version; empty values are unknown, not zero.',
            **_budget(country),
        },
    }
