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


_RESEARCH_AREAS = ('physics', 'society', 'engineering')


def _single(node: PDXNode | None, key: str) -> Any:
    values = node.get_all(key) if isinstance(node, PDXNode) else []
    return values[0] if len(values) == 1 else None


def _single_node(node: PDXNode | None, key: str) -> PDXNode | None:
    value = _single(node, key)
    return value if isinstance(value, PDXNode) else None


def _data_key(value: Any) -> bool:
    return (isinstance(value, str) and bool(value) and value.isascii()
            and all(char.isalnum() or char in '_.-' for char in value))


def _observed_number(value: Any, *, positive: bool = False) -> float | None:
    if type(value) not in (int, float):
        return None
    number = _number(value)
    return number if number is not None and (number > 0 if positive else number >= 0) else None


def _date_text(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parts = value.split('.')
    return (len(parts) == 3 and all(part.isascii() and part.isdecimal() for part in parts)
            and 1 <= int(parts[1]) <= 12 and 1 <= int(parts[2]) <= 31)


def _research_status(country: PDXNode | None) -> dict[str, Any]:
    """Observe current-format research without interpreting missing queues as idle."""
    tech_status = _single_node(country, 'tech_status')
    alternatives = _single_node(tech_status, 'alternatives')
    completed = tech_status.get_all('technology') if tech_status is not None else []
    completed_known = (tech_status is not None and bool(completed) and all(_data_key(key) for key in completed)
                       and len(completed) == len(set(completed)))
    result: dict[str, Any] = {
        'known': False, 'areas': {}, 'unknown_fields': [],
        'completed_known': completed_known,
        'completed_technologies': completed if completed_known else None,
    }
    if not completed_known:
        result['unknown_fields'].append('completed_technologies')
    for area in _RESEARCH_AREAS:
        available = _single_node(alternatives, area)
        alternatives_known = available is not None
        choices: list[str] = []
        if available is not None:
            for key, value in available.entries:
                if key is not None or not _data_key(value) or value in choices:
                    alternatives_known = False
                else:
                    choices.append(value)

        queue = _single_node(tech_status, area + '_queue')
        queue_known = queue is not None
        queue_entries: list[dict[str, Any]] = []
        technologies: set[str] = set()
        if queue is not None:
            for key, entry in queue.entries:
                if key is not None or not isinstance(entry, PDXNode):
                    queue_known = False
                    continue
                technology = _single(entry, 'technology')
                progress = _observed_number(_single(entry, 'progress'))
                date = _single(entry, 'date')
                progress_fields = entry.get_all('progress')
                if (not _data_key(technology) or technology in technologies or not _date_text(date)
                        or len(progress_fields) > 1 or progress_fields and progress is None):
                    queue_known = False
                    continue
                technologies.add(technology)
                queue_entries.append({'technology': technology, 'progress': progress,
                                      'progress_known': progress is not None, 'date': date})

        auto = _single(tech_status, 'auto_researching_' + area)
        auto_known = auto in ('yes', 'no')
        progress_known = queue_known and all(item['progress_known'] for item in queue_entries)
        status = {
            'known': alternatives_known and queue_known and auto_known and progress_known,
            'alternatives_known': alternatives_known,
            'alternatives': choices if alternatives_known else None,
            'queue_known': queue_known,
            'queue': queue_entries if queue_known else None,
            'current_known': queue_known,
            'current': queue_entries[0]['technology'] if queue_known and queue_entries else None,
            'auto_researching': auto == 'yes' if auto_known else None,
            'progress_known': progress_known,
        }
        result['areas'][area] = status
        for field, known in (('alternatives', alternatives_known), ('queue', queue_known), ('auto_researching', auto_known)):
            if not known:
                result['unknown_fields'].append(f'{area}.{field}')
        if queue_known and not progress_known:
            result['unknown_fields'].append(f'{area}.progress')
    result['known'] = not result['unknown_fields']
    return result


def _indexed_construction_nodes(collection: PDXNode, label: str, issues: set[str]) -> dict[str, PDXNode]:
    objects: dict[str, PDXNode] = {}
    seen: set[str] = set()
    duplicates: set[str] = set()
    for key, value in collection.entries:
        identifier = _identifier(key)
        if identifier is None:
            issues.add(label + '.entries')
            continue
        if identifier in seen:
            duplicates.add(identifier)
            issues.add(label + '.duplicate_ids')
        seen.add(identifier)
        if isinstance(value, PDXNode):
            objects[identifier] = value
        elif value != 'none':
            issues.add(label + '.entries')
    for identifier in duplicates:
        objects.pop(identifier, None)
    return objects


def _unambiguous_data(value: Any) -> bool:
    """Construction payloads must retain their structure without last-key wins."""
    if not isinstance(value, PDXNode):
        return not isinstance(value, float) or math.isfinite(value)
    keys = [key for key, _ in value.entries if key is not None]
    return (len(keys) == len(set(keys)) and (not keys or len(keys) == len(value.entries))
            and all(_unambiguous_data(item) for _, item in value.entries))


def _construction(root: PDXNode, country_id: str, *, country_known: bool) -> dict[str, Any]:
    """Join only owned queues and their selected-country paid items.

    ``known`` describes the observed queue/item membership, not whether an
    external order succeeded. Resources/progress are serialized observations.
    The item manager's iteration order does not establish native queue order.
    """
    result: dict[str, Any] = {
        'known': False, 'queues_known': False, 'items_known': False,
        'queue_count': None, 'item_count': None,
        'queues': [], 'queues_by_id': {}, 'items': [], 'items_by_id': {}, 'unknown_fields': [],
    }
    construction = _single_node(root, 'construction')
    queues = _single_node(_single_node(construction, 'queue_mgr'), 'queues')
    items = _single_node(_single_node(construction, 'item_mgr'), 'items')
    if not country_known or queues is None or items is None:
        result['unknown_fields'] = ['country'] if not country_known else []
        if queues is None:
            result['unknown_fields'].append('queue_mgr.queues')
        if items is None:
            result['unknown_fields'].append('item_mgr.items')
        return result

    queue_issues: set[str] = set()
    item_issues: set[str] = set()
    queue_nodes = _indexed_construction_nodes(queues, 'queues', queue_issues)
    item_nodes = _indexed_construction_nodes(items, 'items', item_issues)
    owners: dict[str, str] = {}
    own_queues: dict[str, dict[str, Any]] = {}
    for queue_id, queue in queue_nodes.items():
        owner = _identifier(_single(queue, 'owner'))
        if owner is None:
            queue_issues.add('queues.owner')
            continue
        owners[queue_id] = owner
        if owner != country_id:
            continue
        location = _single_node(queue, 'location')
        location_type = _single(location, 'type')
        location_id = _identifier(_single(location, 'id'))
        queue_type = _single(queue, 'type')
        simultaneous = _single(queue, 'simultaneous')
        disabled = queue.get_all('disabled')
        header_known = (_data_key(queue_type) and type(simultaneous) is int and simultaneous > 0
                        and type(location_type) is int and location_type >= 0 and location_id is not None
                        and (not disabled or len(disabled) == 1 and disabled[0] in ('yes', 'no')))
        if not header_known:
            queue_issues.add('queues.header')
        own_queues[queue_id] = {
            'id': queue_id, 'owner': owner, 'type': queue_type if _data_key(queue_type) else None,
            'location': {'type': location_type if type(location_type) is int else None, 'id': location_id}
            if location is not None else None,
            'simultaneous': simultaneous if type(simultaneous) is int and simultaneous > 0 else None,
            'disabled': disabled[0] == 'yes' if len(disabled) == 1 and disabled[0] in ('yes', 'no') else None,
            'known': header_known, 'items_known': False, 'item_count': None, 'items': [], 'order_known': False,
        }

    # The actual planet.build_queue field is a reference into queue_mgr, not
    # the queue's content. Validate that reference before reporting it empty.
    raw_planets = _single_node(root, 'planet')
    if not root.get_all('planet'):
        raw_planets = _single_node(_single_node(root, 'planets'), 'planet')
    for planet_id, planet in _objects(raw_planets):
        planet_owners = planet.get_all('owner')
        if country_id not in {_identifier(owner) for owner in planet_owners}:
            continue
        if len(planet_owners) != 1 or len(raw_planets.get_all(planet_id)) != 1:
            queue_issues.add('queues.owned_planet_reference')
            continue
        references = planet.get_all('build_queue')
        if len(references) > 1:
            queue_issues.add('queues.owned_planet_reference')
        elif references:
            queue_id = _identifier(references[0])
            queue = own_queues.get(queue_id) if queue_id is not None else None
            if (queue is None or queue['type'] != 'planet' or queue['location'] is None
                    or queue['location']['id'] != planet_id or queue['location']['type'] != 2):
                queue_issues.add('queues.owned_planet_reference')

    own_items: dict[str, dict[str, Any]] = {}
    for item_id, item in item_nodes.items():
        queue_id = _identifier(_single(item, 'queue'))
        payer = _identifier(_single(item, 'paying_country'))
        if queue_id not in queue_nodes or queue_id not in owners or payer is None:
            item_issues.add('items.references')
            continue
        owner = owners[queue_id]
        if owner != country_id and payer != country_id:
            continue
        if owner != country_id or payer != country_id:
            item_issues.add('items.ownership_conflict')
            continue
        progress = _observed_number(_single(item, 'progress'))
        progress_needed = _observed_number(_single(item, 'progress_needed'), positive=True)
        resources = _single_node(item, 'resources')
        costs: dict[str, float] = {}
        resources_known = resources is not None
        if resources is not None:
            for resource, value in resources.entries:
                amount = _observed_number(value)
                if not _data_key(resource) or resource in costs or amount is None:
                    resources_known = False
                else:
                    costs[resource] = amount
        buildables = [(key, value) for key, value in item.items() if key.startswith('buildable_')]
        buildable_known = (len(buildables) == 1 and _data_key(buildables[0][0])
                           and isinstance(buildables[0][1], PDXNode)
                           and bool(buildables[0][1].entries) and _unambiguous_data(buildables[0][1]))
        if progress is None or progress_needed is None or not resources_known or not buildable_known:
            item_issues.add('items.payload')
            continue
        buildable_type, buildable = buildables[0]
        location = own_queues[queue_id]['location']
        target = None
        if isinstance(location, dict) and location.get('type') == 2 and buildable.get_all('planet'):
            # 4.5.2 construction's historical 'planet' field is a colony
            # handle. Queue location type 2 is the physical celestial body.
            # They must join through colony.carrier and planet.colony.
            colony_id = _identifier(_single(buildable, 'planet'))
            colony = _single_node(_single_node(root, 'colony'), colony_id) if colony_id is not None else None
            carrier = _single_node(colony, 'carrier')
            planets = _single_node(root, 'planet')
            if not root.get_all('planet'):
                planets = _single_node(_single_node(root, 'planets'), 'planet')
            planet = _single_node(planets, location.get('id'))
            if (_single(carrier, 'type') != 'planet'
                    or _identifier(_single(carrier, 'reference')) != location.get('id')
                    or _identifier(_single(planet, 'colony')) != colony_id
                    or _identifier(_single(planet, 'build_queue')) != queue_id
                    or _identifier(_single(planet, 'owner')) != country_id):
                item_issues.add('items.location_conflict')
                continue
            target = {'planet_id': location['id'], 'colony_id': colony_id}
        summary = {
            'id': item_id, 'queue_id': queue_id, 'paying_country': payer,
            'progress': progress, 'progress_needed': progress_needed, 'resources': costs,
            'buildable_type': buildable_type, 'buildable': _structure(buildable),
            **({'target': target} if target is not None else {}),
        }
        own_items[item_id] = summary
        own_queues[queue_id]['items'].append(summary)

    for queue in own_queues.values():
        queue['items_known'] = queue['known'] and not item_issues
        queue['known'] = queue['items_known']
        if queue['items_known']:
            queue['item_count'] = len(queue['items'])
            queue['order_known'] = not queue['items']
    result['queues_known'] = not queue_issues
    result['items_known'] = not queue_issues and not item_issues
    result['known'] = result['queues_known'] and result['items_known']
    result['queues'] = list(own_queues.values())
    result['queues_by_id'] = own_queues
    result['items'] = list(own_items.values())
    result['items_by_id'] = own_items
    result['queue_count'] = len(own_queues) if result['queues_known'] else None
    result['item_count'] = len(own_items) if result['items_known'] else None
    result['unknown_fields'] = sorted(queue_issues | item_issues)
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


def _object_summary(object_id: str, value: PDXNode) -> dict[str, Any]:
    item: dict[str, Any] = {'id': object_id}
    for key in ('name', 'owner', 'controller', 'orbit', 'station', 'fleet_power', 'military_power', 'combat', 'size', 'class', 'ship_class', 'planet_size', 'planet_class', 'colony', 'build_queue', 'queue', 'construction_queue', 'current_order', 'order', 'actions', 'buildings'):
        raw = _single(value, key)
        if raw is not None:
            item[key] = _plain(raw) if key == 'name' else _structure(raw)
    mobile = _single(_single_node(value, 'properties'), 'mobile')
    if mobile in ('yes', 'no'):
        item['mobile'] = mobile == 'yes'
    # A repeated primary coordinate cannot be repaired by silently falling
    # back to a secondary location. Ambiguity must block exact targeting.
    position = _single_node(value, 'coordinate')
    if not value.get_all('coordinate'):
        position = _single_node(_single_node(value, 'movement_manager'), 'coordinate')
    origin = _identifier(_single(position, 'origin'))
    if origin is not None and origin != '4294967295':
        item['system_id'] = origin
    return item


def _owned(root: PDXNode, category: str, country_id: str) -> list[dict[str, Any]]:
    collection = _single_node(root, category)
    if category == 'planet' and not root.get_all(category):
        collection = _single_node(_single_node(root, 'planets'), 'planet')
    # Territorial controllers also appear on stars and uncolonized bodies.
    # Only a legal owner establishes a player-owned planet; colony references
    # in country.owned_planets are a separate ID namespace in current saves.
    return [_object_summary(object_id, value) for object_id, value in _objects(collection)
            if len(collection.get_all(object_id)) == 1 and str(_single(value, 'owner')) == country_id]


def _identifier(value: Any) -> str | None:
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        identifier = str(value)
        if identifier.isascii() and identifier.isdecimal():
            return identifier
    return None


def _planet_district_levels(root: PDXNode, planet: dict) -> dict:
    """Current-build district levels through the colony's complete ID list."""
    unknown = {'districts_known': False, 'district_levels': None}
    colony_id = _identifier(planet.get('colony'))
    colony = _single_node(_single_node(root, 'colony'), colony_id) if colony_id is not None else None
    carrier = _single_node(colony, 'carrier')
    if (_single(carrier, 'type') != 'planet'
            or _identifier(_single(carrier, 'reference')) != planet['id']):
        return unknown
    references = _single_node(colony, 'districts')
    districts = _single_node(root, 'districts')
    if references is None or districts is None:
        return unknown
    levels: dict[str, int] = {}
    seen: set[str] = set()
    for key, value in references.entries:
        reference = _identifier(value)
        if key is not None or reference is None or reference in seen:
            return unknown
        seen.add(reference)
        district = _single_node(districts, reference)
        district_type = _single(district, 'type')
        level = _single(district, 'level')
        if not _data_key(district_type) or district_type in levels or type(level) is not int or level < 0:
            return unknown
        levels[district_type] = level
    return {'districts_known': True, 'district_levels': levels}


def _planet_zones(root: PDXNode, planet: dict) -> dict:
    """Join the planet's colony/district/zone/building handles without guessing empty lists."""
    unknown = {'zones_known': False, 'zones_by_id': {}}
    colony_id = _identifier(planet.get('colony'))
    colony = _single_node(_single_node(root, 'colony'), colony_id) if colony_id is not None else None
    carrier = _single_node(colony, 'carrier')
    if (_single(carrier, 'type') != 'planet'
            or _identifier(_single(carrier, 'reference')) != planet['id']):
        return unknown
    references = _single_node(colony, 'districts')
    districts, zones, buildings = (_single_node(root, key) for key in ('districts', 'zones', 'buildings'))
    if references is None or districts is None or zones is None:
        return unknown
    result, seen_districts = {}, set()
    for key, value in references.entries:
        district_id = _identifier(value)
        if key is not None or district_id is None or district_id in seen_districts:
            return unknown
        seen_districts.add(district_id)
        district = _single_node(districts, district_id)
        district_type = _single(district, 'type')
        zone_refs = _single_node(district, 'zones')
        if not _data_key(district_type) or zone_refs is None:
            return unknown
        for slot_index, (zone_key, zone_value) in enumerate(zone_refs.entries):
            zone_id = _identifier(zone_value)
            if zone_key is not None or zone_id is None:
                return unknown
            if zone_id == '4294967295':  # Observed inactive rural-zone slot.
                continue
            if zone_id in result:
                return unknown
            zone = _single_node(zones, zone_id)
            zone_type = _single(zone, 'type')
            if not _data_key(zone_type):
                return unknown
            summary = {'id': zone_id, 'district_id': district_id, 'district_type': district_type,
                       'slot_index': slot_index,
                       'type': zone_type, 'buildings_known': False, 'buildings_by_id': None}
            building_refs = _single_node(zone, 'buildings')
            if building_refs is not None and buildings is not None:
                found, positions, valid = {}, set(), True
                for building_key, building_value in building_refs.entries:
                    building_id = _identifier(building_value)
                    building = _single_node(buildings, building_id) if building_id is not None else None
                    building_type, position = _single(building, 'type'), _single(building, 'position')
                    if (building_key is not None or building_id is None or building_id in found
                            or not _data_key(building_type) or type(position) is not int
                            or position < 0 or position in positions):
                        valid = False
                        break
                    positions.add(position)
                    found[building_id] = {'id': building_id, 'type': building_type, 'position': position}
                if valid:
                    summary.update({'buildings_known': True, 'buildings_by_id': found})
            result[zone_id] = summary
    # A zone positively referenced by two district objects cannot establish a
    # unique parent. Reject that identity instead of picking a convenient one.
    for zone_id, zone in result.items():
        parents = [district_id for district_id, district in _objects(districts)
                   if isinstance((refs := _single_node(district, 'zones')), PDXNode)
                   and any(_identifier(value) == zone_id for key, value in refs.entries)]
        if parents != [zone['district_id']]:
            return unknown
    return {'zones_known': True, 'zones_by_id': result}


def _empire_build(country: PDXNode | None) -> dict:
    """Observe strategic choices for interpretation; this grants no control over them."""
    result = {}
    for field in ('government', 'ethos', 'traditions', 'ascension_perks', 'policies', 'edicts'):
        value = _single_node(country, field)
        known = value is not None and _unambiguous_data(value)
        result[field] = {'known': known, 'value': _structure(value) if known else None}
    return result


def _fleet_references(value: Any) -> tuple[set[str], bool]:
    """Read observed owned_fleets lists without interpreting their metadata."""
    if not isinstance(value, PDXNode):
        return set(), False
    direct = value.get_all('fleet')
    if direct:
        if len(direct) != 1:
            return set(), False
        identifier = _identifier(direct[0])
        return ({identifier}, True) if identifier is not None else (set(), False)
    identifiers: set[str] = set()
    known = True
    for key, entry in value.entries:
        if (key is None or key.isdecimal()) and isinstance(entry, PDXNode):
            found, understood = _fleet_references(entry)
            known = known and understood and not identifiers.intersection(found)
            identifiers.update(found)
        else:
            known = False
    return identifiers, known


def _fleets(root: PDXNode, country_id: str) -> tuple[list[dict[str, Any]], bool]:
    collection = _single_node(root, 'fleet')
    if not isinstance(collection, PDXNode):
        return [], False
    fleets: dict[str, PDXNode] = {}
    duplicate_ids: set[str] = set()
    for fleet_id, fleet in _objects(collection):
        if fleet_id in fleets:
            duplicate_ids.add(fleet_id)
        fleets[fleet_id] = fleet
    claims: dict[str, set[str]] = {}
    selected_references: set[str] = set()
    selected_manager_present = False
    selected_manager_known = False
    for empire_id, empire in _objects(root.get('country')):
        managers = empire.get_all('fleets_manager')
        if not managers:
            continue
        references: set[str] = set()
        known = len(managers) == 1
        for manager in managers:
            lists = _node(manager).get_all('owned_fleets')
            known = known and len(lists) == 1
            for owned in lists:
                found, understood = _fleet_references(owned)
                references.update(found)
                known = known and understood
        for fleet_id in references:
            claims.setdefault(fleet_id, set()).add(empire_id)
        if empire_id == country_id:
            selected_manager_present = True
            selected_manager_known = known
            selected_references = references

    invalid_owners = set(duplicate_ids)
    for fleet_id, fleet in fleets.items():
        # A controller may differ from its legal owner; use it only in formats
        # which omit the owner field entirely.
        owners = fleet.get_all('owner') or fleet.get_all('controller')
        if len(owners) > 1 or len(fleet.get_all('controller')) > 1:
            invalid_owners.add(fleet_id)
        for raw in owners:
            owner = _identifier(raw)
            if owner is None:
                invalid_owners.add(fleet_id)
            else:
                claims.setdefault(fleet_id, set()).add(owner)

    # A valid selected-country list establishes the complete set even when
    # unrelated fleets lack ownership fields. Otherwise every object needs an
    # unambiguous ownership claim before an empty result can mean zero fleets.
    known = selected_manager_known or all(len(claims.get(fleet_id, set())) == 1
                                         and fleet_id not in invalid_owners for fleet_id in fleets)
    # Current dumps retain numeric deleted slots as `id=none`. An exhaustive
    # selected-country manager can establish its own fleet set despite those
    # unrelated tombstones. A selected reference to a deleted slot, unknown
    # scalar, anonymous entry, or nonnumeric tombstone still cannot do so.
    known = known and all(
        key is not None and (
            isinstance(value, PDXNode)
            or (selected_manager_known and _identifier(key) is not None
                and key not in selected_references and value == 'none')
        )
        for key, value in collection.entries
    )
    if selected_manager_present and not selected_manager_known:
        known = False
    if duplicate_ids:
        known = False
    if not selected_references <= fleets.keys():
        known = False
    result: list[dict[str, Any]] = []
    for fleet_id, fleet in fleets.items():
        owners = claims.get(fleet_id, set())
        if len(owners) != 1 or fleet_id in invalid_owners:
            if country_id in owners:
                known = False
            continue
        if country_id not in owners:
            continue
        # Conflicting positive ownership and an exhaustive manager list cannot
        # establish that this fleet belongs to the selected empire.
        if selected_manager_known and fleet_id not in selected_references:
            known = False
            continue
        summary = _object_summary(fleet_id, fleet)
        summary['orders_known'] = all(len(fleet.get_all(key)) <= 1 for key in ('actions', 'current_order', 'order'))
        # Preserve unexpected queue containers defensively; their presence must
        # not silently turn a stopped-fleet check into known absence.
        for key in ('orders', 'queued_orders'):
            fields = fleet.get_all(key)
            if len(fields) > 1:
                summary['orders_known'] = False
            elif fields:
                summary[key] = _structure(fields[0])
        result.append(summary)
    return result, known


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

    return normalize_gamestate(root, path, country_id=country_id)


def normalize_gamestate(root: PDXNode, path: Path, *, country_id: str | None = None) -> dict[str, Any]:
    """Normalize an already parsed snapshot without reopening its source file.

    Direct bridge dumps are overwritten while the game runs. Keeping this step
    independent of file I/O lets callers parse one immutable, stable byte copy.
    """
    from .ship_commands import observe_shipyards
    from .fleet_observe import observe_space
    path = Path(path)
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
    unique_country = _single_node(_single_node(root, 'country'), country_id)
    resources, resource_path = _resources(country)
    research = _research(country)
    fleets, fleets_known = _fleets(root, country_id)
    planets = _owned(root, 'planet', country_id)
    for planet in planets:
        planet.update(_planet_district_levels(root, planet))
        planet.update(_planet_zones(root, planet))
    at_war, war_known = _at_war(root, country, country_id)
    missing = []
    if not resources:
        missing.append('resources')
    if not research:
        missing.append('research')
    if not fleets_known:
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
        'research_status': _research_status(unique_country),
        'empire_build': _empire_build(unique_country),
        'construction': _construction(root, country_id, country_known=unique_country is not None),
        'shipbuilding': observe_shipyards(root, country_id, fleets) if fleets_known else {},
        'space': observe_space(root, country_id),
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
