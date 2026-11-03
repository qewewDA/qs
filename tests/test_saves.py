from pathlib import Path
import zipfile

import pytest

from stellaris_autopilot.pdx import PDXError, loads
from stellaris_autopilot.saves import SaveError, read_save


def test_player_account_id_is_not_an_empire_id(tmp_path):
    path = tmp_path / 'account.txt'
    path.write_text('date=2200.01.01 player={id=0} country={0={name="AI"}}', encoding='utf-8')
    with pytest.raises(SaveError, match='missing or ambiguous'):
        read_save(path)


def test_player_name_metadata_does_not_create_second_player(tmp_path):
    path = tmp_path / 'name.txt'
    path.write_text('''date=2200.01.01
        player={country=0 name={key=example variables={country=1}}}
        country={0={} 1={}}''', encoding='utf-8')
    assert read_save(path)['metadata']['player_country_ids'] == ['0']


FIXTURE = '''
version="4.3.1" date="2200.01.02" ironman=no multiplayer=no
player={ { name="Human" country=0 } }
country={
 0={ name={key="United Nations of Earth"}
  modules={standard_economy_module={resources={energy=120 minerals=40.5 alloys=32}}}
  budget={last_month={income={base={energy=8 alloys=5} jobs={alloys=12}} expenses={fleet={energy=3 alloys=2}}}}
  research={physics={technology="tech_lasers_2"} society="tech_genome_mapping"}
 }
 1={ name="Alien" resources={energy=900} }
}
fleet={ 3={ owner=0 name="First Fleet" military_power=100 current_order={type="return" target=10} }
        4={ owner=1 name="Alien Fleet" } }
planet={ 10={ owner=0 name="Earth" buildings={"building_research_lab_1"} queue={item={building="building_lab"}}} }
war={}
'''


def write_save(tmp_path: Path, text: str = FIXTURE, zipped: bool = False) -> Path:
    path = tmp_path / 'state.sav'
    if zipped:
        with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('gamestate', text)
            archive.writestr('meta', 'version="4.3.1"')
    else:
        path.write_text(text, encoding='utf-8')
    return path


def test_parser_preserves_duplicates_and_anonymous_lists():
    root = loads('foo={ bar=1 bar=2 items={ 2 "3" -1.2 yes } }')
    assert root.get('foo').get('bar') == 2
    assert root.get('foo').get_all('bar') == [1, 2]
    assert list(root.get('foo').get('items').values()) == [2, '3', -1.2, 'yes']
    assert list(root.items())[0][0] == 'foo'


def test_parser_strings_comments_and_dates():
    root = loads('# a comment\n date=2200.01.01 text="quoted \\"value\\" # not comment" escaped="a\\\\b"')
    assert root.get('date') == '2200.01.01'
    assert root.get('text') == 'quoted "value" # not comment'
    assert root.get('escaped') == 'a\\b'


@pytest.mark.parametrize('text', ['a={', 'a=', '}', 'a="oops', '=1', 'a={x=}', '\x00binary'])
def test_parser_rejects_malformed_inputs(text):
    with pytest.raises(PDXError):
        loads(text)


def test_parser_limits_recursion():
    with pytest.raises(PDXError, match='nesting'):
        loads('x={' * 130 + '}' * 130)


@pytest.mark.parametrize('zipped', [False, True])
def test_extracts_selected_empire_not_first_country(tmp_path, zipped):
    state = read_save(write_save(tmp_path, zipped=zipped))
    assert state['country_id'] == '0'
    assert state['name'] == 'United Nations of Earth'
    assert state['resources'] == {'energy': 120, 'minerals': 40.5, 'alloys': 32}
    assert state['research']['physics'] == 'tech_lasers_2'
    assert [fleet['id'] for fleet in state['fleets']] == ['3']
    assert state['fleets'][0]['current_order']['type'] == 'return'
    assert state['planets'][0]['buildings'] == ['building_research_lab_1']
    assert state['fleets_by_id']['3']['current_order']['type'] == 'return'
    assert state['planets_by_id']['10']['buildings'] == ['building_research_lab_1']
    assert state['at_war'] is False
    assert state['metadata']['war_status_known'] is True
    assert state['metadata']['monthly_net_income'] == {'energy': 5.0, 'alloys': 15.0}
    assert state['metadata']['ironman'] == 'no'
    assert state['metadata']['multiplayer'] == 'no'


def test_explicit_country_overrides_player(tmp_path):
    state = read_save(write_save(tmp_path), country_id='1')
    assert state['country_id'] == '1'
    assert state['resources']['energy'] == 900


def test_multiplayer_is_ambiguous_and_reported_when_explicit(tmp_path):
    path = write_save(tmp_path, FIXTURE + '\nplayer={country=1}')
    with pytest.raises(SaveError, match='ambiguous'):
        read_save(path)
    assert read_save(path, country_id='0')['metadata']['multiplayer'] == 'yes'


def test_missing_player_is_not_guessed(tmp_path):
    with pytest.raises(SaveError, match='explicit country_id'):
        read_save(write_save(tmp_path, 'date=2200.01.01 country={0={name="Human"}}'))


def test_missing_fields_stay_unknown(tmp_path):
    state = read_save(write_save(tmp_path, 'date=2200.01.01 player={country=0} country={0={}}'))
    assert state['resources'] == {}
    assert state['research'] == {}
    assert state['metadata']['war_status_known'] is False
    assert set(state['metadata']['missing_fields']) == {'resources', 'research', 'fleets', 'planets', 'at_war'}


def test_current_fleet_schema_preserves_actions_class_and_explicit_mobility(tmp_path):
    text = '''date=2200.01.01 player={country=0}
    country={0={fleets_manager={owned_fleets={{fleet=3} {fleet=4} {fleet=5}}}}}
    fleet={
      3={ship_class="shipclass_military" actions={wait={time=30}}
         properties={weapon=yes mobile=yes valid_for_combat=yes}}
      4={ship_class="shipclass_starbase" properties={mobile=no}}
      5={ship_class="shipclass_military" properties={mobile=unknown}}
    }'''
    fleets = read_save(write_save(tmp_path, text))['fleets_by_id']
    assert fleets['3']['actions'] == {'wait': {'time': 30}}
    assert fleets['3']['ship_class'] == 'shipclass_military'
    assert fleets['3']['mobile'] is True
    assert fleets['4']['mobile'] is False
    assert 'mobile' not in fleets['5']
    assert 'properties' not in fleets['3']


NATIVE_RESEARCH = '''tech_status={
    technology=tech_finished_a level=1 technology=tech_finished_b level=1
    physics_queue={{technology=tech_a progress=12 date=2200.01.01}}
    society_queue={{technology=tech_b progress=8 date=2200.01.01}}
    engineering_queue={{technology=tech_c progress=5 date=2200.01.01}}
    alternatives={physics={tech_a tech_a2} society={tech_b} engineering={tech_c}}
    auto_researching_physics=no auto_researching_society=yes auto_researching_engineering=no
}'''


def native_state(tmp_path, *, research=NATIVE_RESEARCH, construction=''):
    return read_save(write_save(tmp_path, 'date=2200.01.05 player={country=0} '
                               'country={0={' + research + '} 1={name="Foreign private empire" '
                               'tech_status={alternatives={physics={tech_foreign_secret}}}}} '
                               'planet={3={owner=0 colony=0 build_queue=0}} colony={0={carrier={type=planet reference=3}}} ' + construction))


def test_native_research_status_exposes_candidates_current_progress_and_explicit_auto_flags(tmp_path):
    state = native_state(tmp_path)
    status = state['research_status']
    assert status['known'] is True
    assert status['unknown_fields'] == []
    assert state['research'] == {'physics': 'tech_a', 'society': 'tech_b', 'engineering': 'tech_c'}
    physics = status['areas']['physics']
    assert physics['alternatives'] == ['tech_a', 'tech_a2']
    assert physics['current'] == 'tech_a'
    assert physics['queue'] == [{'technology': 'tech_a', 'progress': 12, 'progress_known': True, 'date': '2200.01.01'}]
    assert physics['auto_researching'] is False
    assert status['areas']['society']['auto_researching'] is True
    assert 'tech_foreign_secret' not in str(state)
    assert status['completed_known'] is True
    assert status['completed_technologies'] == ['tech_finished_a', 'tech_finished_b']


def test_absent_research_queue_is_unknown_while_explicit_empty_queue_is_observable(tmp_path):
    without = NATIVE_RESEARCH.replace('physics_queue={{technology=tech_a progress=12 date=2200.01.01}}', '')
    physics = native_state(tmp_path, research=without)['research_status']['areas']['physics']
    assert physics['queue_known'] is False
    assert physics['current_known'] is False
    assert physics['queue'] is None
    assert physics['current'] is None
    assert physics['alternatives'] == ['tech_a', 'tech_a2']
    assert physics['auto_researching'] is False
    explicit = without.replace('tech_status={', 'tech_status={physics_queue={}', 1)
    physics = native_state(tmp_path, research=explicit)['research_status']['areas']['physics']
    assert physics['known'] is True
    assert physics['queue_known'] is True
    assert physics['current_known'] is True
    assert physics['queue'] == []
    assert physics['current'] is None


@pytest.mark.parametrize('old,new,unknown_field', [
    ('physics_queue={{', 'physics_queue={} physics_queue={{', 'physics.queue'),
    ('technology=tech_a progress=12', 'technology=tech_a technology=tech_other progress=12', 'physics.queue'),
    ('technology=tech_a progress=12', 'technology=tech_a', 'physics.progress'),
    ('progress=12', 'progress=-1', 'physics.queue'),
    ('date=2200.01.01', 'date=2200.13.01', 'physics.queue'),
    ('physics={tech_a tech_a2}', 'physics={tech_a tech_a}', 'physics.alternatives'),
    ('physics={tech_a tech_a2}', 'physics={} physics={tech_a}', 'physics.alternatives'),
    ('physics={tech_a tech_a2}', 'physics=unknown', 'physics.alternatives'),
    ('auto_researching_physics=no', 'auto_researching_physics=unknown', 'physics.auto_researching'),
    ('auto_researching_physics=no', 'auto_researching_physics=no auto_researching_physics=yes', 'physics.auto_researching'),
])
def test_ambiguous_or_missing_native_research_data_stays_unknown(tmp_path, old, new, unknown_field):
    status = native_state(tmp_path, research=NATIVE_RESEARCH.replace(old, new, 1))['research_status']
    assert status['known'] is False
    assert unknown_field in status['unknown_fields']
    assert status['areas']['physics']['known'] is False


NATIVE_CONSTRUCTION = '''construction={
    queue_mgr={queues={
      0={owner=0 location={type=2 id=3} simultaneous=1 type=planet}
      1={owner=1 location={type=2 id=99} simultaneous=1 type=planet}
      2={owner=4294967295 location={type=2 id=100} simultaneous=1 type=planet}
      4=none
    }}
    item_mgr={items={
      7={queue=0 paying_country=0 progress=10 progress_needed=360 resources={minerals=400}
         buildable_district={district=district_city planet=0}}
      8={queue=1 paying_country=1 progress=20 progress_needed=600 resources={alloys=250}
         buildable_district={district=foreign_private_district planet=99}}
      9=none
    }}
}'''


def test_native_construction_joins_owned_paid_items_and_never_exposes_foreign_queues(tmp_path):
    construction = native_state(tmp_path, construction=NATIVE_CONSTRUCTION)['construction']
    assert construction['known'] is True
    assert construction['queue_count'] == 1
    assert construction['item_count'] == 1
    assert construction['unknown_fields'] == []
    assert set(construction['queues_by_id']) == {'0'}
    assert set(construction['items_by_id']) == {'7'}
    queue = construction['queues_by_id']['0']
    assert queue['location'] == {'type': 2, 'id': '3'}
    assert queue['disabled'] is None
    assert queue['items_known'] is True
    assert queue['order_known'] is False
    assert queue['item_count'] == 1
    item = construction['items_by_id']['7']
    assert queue['items'] == [item]
    assert item['queue_id'] == '0'
    assert item['paying_country'] == '0'
    assert item['resources'] == {'minerals': 400}
    assert item['progress'] == 10
    assert item['progress_needed'] == 360
    assert item['buildable_type'] == 'buildable_district'
    assert item['buildable'] == {'district': 'district_city', 'planet': 0}
    assert item['target'] == {'planet_id': '3', 'colony_id': '0'}
    assert 'foreign_private_district' not in str(construction)
    assert '4294967295' not in str(construction)


def test_construction_without_paid_items_requires_a_complete_explicit_item_manager(tmp_path):
    empty = '''construction={queue_mgr={queues={
       0={owner=0 location={type=2 id=3} simultaneous=1 type=planet}
    }} item_mgr={items={}}}'''
    construction = native_state(tmp_path, construction=empty)['construction']
    assert construction['known'] is True
    assert construction['item_count'] == 0
    assert construction['queues_by_id']['0']['item_count'] == 0
    assert construction['queues_by_id']['0']['order_known'] is True
    for incomplete in ('', empty.replace('item_mgr={items={}}', 'item_mgr={}'),
                       empty.replace('queue_mgr={queues={', 'queue_mgr={unexpected={')):
        construction = native_state(tmp_path, construction=incomplete)['construction']
        assert construction['known'] is False
        assert construction['item_count'] is None
        assert construction['queue_count'] is None


@pytest.mark.parametrize('old,new', [
    ('queue=0 paying_country=0', 'queue=999 paying_country=0'),
    ('queue=0 paying_country=0', 'queue=0 queue=1 paying_country=0'),
    ('queue=0 paying_country=0', 'queue=0 paying_country=1'),
    ('queue=0 paying_country=0', 'queue=1 paying_country=0'),
    ('queue=0 paying_country=0', 'queue=0'),
    ('queue=0 paying_country=0', 'queue=0 paying_country=unknown'),
    ('progress_needed=360', 'progress_needed=0'),
    ('resources={minerals=400}', 'resources={minerals=400 minerals=500}'),
    ('resources={minerals=400}', 'missing_resources={}'),
    ('buildable_district={', 'buildable_ship={} buildable_district={'),
    ('district=district_city planet=0', 'district=district_city planet=0 planet=99'),
    ('9=none', '7=none'),
    ('9=none', '9=unknown'),
])
def test_bad_paid_item_references_or_payloads_never_become_known_empty_queue(tmp_path, old, new):
    construction = native_state(tmp_path, construction=NATIVE_CONSTRUCTION.replace(old, new, 1))['construction']
    assert construction['known'] is False
    assert construction['items_known'] is False
    assert construction['item_count'] is None
    assert construction['queues_by_id']['0']['items_known'] is False
    assert construction['queues_by_id']['0']['item_count'] is None


@pytest.mark.parametrize('old,new', [
    ('0={owner=0', '0={owner=0 owner=1'),
    ('4=none', '0=none'),
    ('4=none', '4=unknown'),
    ('location={type=2 id=3}', 'location={type=2 id=99}'),
    ('location={type=2 id=3}', 'location={type=unknown id=3}'),
    ('simultaneous=1 type=planet', 'simultaneous=1 type=planet type=ships'),
])
def test_ambiguous_queues_and_broken_owned_planet_reference_stay_unknown(tmp_path, old, new):
    construction = native_state(tmp_path, construction=NATIVE_CONSTRUCTION.replace(old, new, 1))['construction']
    assert construction['known'] is False
    assert construction['queues_known'] is False
    assert construction['queue_count'] is None
    assert construction['item_count'] is None


def test_duplicate_selected_country_or_tech_status_does_not_select_new_details_by_last_key(tmp_path):
    text = ('date=2200.01.01 player={country=0} country={0={' + NATIVE_RESEARCH + '} 0={}} '
            + NATIVE_CONSTRUCTION)
    state = read_save(write_save(tmp_path, text))
    assert state['research_status']['known'] is False
    assert state['construction']['known'] is False
    duplicated = native_state(tmp_path, research=NATIVE_RESEARCH + NATIVE_RESEARCH)['research_status']
    assert duplicated['known'] is False
    assert duplicated['areas']['physics']['alternatives'] is None
    assert duplicated['areas']['physics']['queue'] is None


def test_tech_status_queues_and_galaxy_ironman(tmp_path):
    text = '''date=2200.01.01 galaxy={ironman=yes} player={country=0}
    country={0={tech_status={physics_queue={{technology="tech_lasers_2" progress=3}}}
    budget={current_month={income={jobs={energy=12}} expenses={upkeep={energy=7}}}}}}'''
    state = read_save(write_save(tmp_path, text))
    assert state['research'] == {'physics': 'tech_lasers_2'}
    assert state['metadata']['ironman'] == 'yes'
    assert state['metadata']['budget_period'] == 'current_month'
    assert state['metadata']['monthly_net_income'] == {'energy': 5.0}


def test_nested_planet_collection_and_player_ids(tmp_path):
    text = '''date=2200.01.01 player={country=0} country={0={}} planets={planet={
    10={owner=0 name="Earth"} 11={owner=1 name="Alien"}}}'''
    state = read_save(write_save(tmp_path, text))
    assert state['metadata']['player_country_ids'] == ['0']
    assert [planet['id'] for planet in state['planets']] == ['10']
    assert 'planets' not in state['metadata']['missing_fields']


def test_planets_require_owner_and_preserve_current_and_legacy_fields(tmp_path):
    text = '''date=2200.01.01 player={country=0}
    country={0={owned_planets={0} controlled_colonies={0}}}
    planets={planet={
      0={controller=0 planet_class=pc_g_star planet_size=30}
      3={name="Earth" owner=0 controller=0 planet_class=pc_continental
         planet_size=18 colony=0 build_queue=7 class=legacy_class size=18
         construction_queue={item={building=building_lab}}}
      5={controller=0 planet_class=pc_barren planet_size=13}
      8={owner=1 controller=0 planet_class=pc_continental colony=2}
    }}'''
    state = read_save(write_save(tmp_path, text))
    assert [planet['id'] for planet in state['planets']] == ['3']
    earth = state['planets_by_id']['3']
    assert earth['planet_class'] == 'pc_continental'
    assert earth['planet_size'] == 18
    assert earth['colony'] == 0
    # The current-format queue field is an object reference, not queue details.
    assert earth['build_queue'] == 7
    assert earth['class'] == 'legacy_class'
    assert earth['size'] == 18
    assert earth['construction_queue'] == {'item': {'building': 'building_lab'}}


def test_fleets_manager_ownership_movement_and_orders(tmp_path):
    text = '''date=2200.01.01 player={country=0}
    country={
      0={fleets_manager={owned_fleets={{fleet=3} {fleet=5}}}}
      1={fleets_manager={owned_fleets={{fleet=4}}}}
    }
    fleet={
      3={name="First Fleet" military_power=100
         movement_manager={coordinate={x=1 y=2 origin=42}}
         current_order={aggressive_stance_fleet_order={target=9 order_id=7}}}
      4={name="Other Fleet" movement_manager={coordinate={origin=80}}}
      5={name="Station" station=yes movement_manager={coordinate={origin=42}}}
    }'''
    state = read_save(write_save(tmp_path, text))
    assert [fleet['id'] for fleet in state['fleets']] == ['3', '5']
    assert state['fleets_by_id']['3']['system_id'] == '42'
    assert state['fleets_by_id']['3']['current_order'] == {
        'aggressive_stance_fleet_order': {'target': 9, 'order_id': 7},
    }
    assert 'fleets' not in state['metadata']['missing_fields']


def test_ownerless_fleets_without_manager_stay_unknown(tmp_path):
    text = 'date=2200.01.01 player={country=0} country={0={}} fleet={3={name="Unidentified"}}'
    state = read_save(write_save(tmp_path, text))
    assert state['fleets'] == []
    assert 'fleets' in state['metadata']['missing_fields']


@pytest.mark.parametrize('owned', [
    '{unexpected=3}', '{{fleet=999}}', '{{fleet=unknown}}', '{{fleet=3 fleet=4}}',
])
def test_incomplete_fleet_manager_stays_unknown(tmp_path, owned):
    text = ('date=2200.01.01 player={country=0} '
            'country={0={fleets_manager={owned_fleets=' + owned + '}}} fleet={3={} 4={}}')
    state = read_save(write_save(tmp_path, text))
    assert state['fleets'] == []
    assert 'fleets' in state['metadata']['missing_fields']


@pytest.mark.parametrize('countries, fleet', [
    ('0={fleets_manager={owned_fleets={{fleet=3}}}} '
     '1={fleets_manager={owned_fleets={{fleet=3}}}}', '3={}'),
    ('0={fleets_manager={owned_fleets={{fleet=3}}}} 1={}', '3={owner=1}'),
    ('0={fleets_manager={owned_fleets={{fleet=3}}}}', '3={owner=unknown}'),
    ('0={fleets_manager={owned_fleets={}}}', '3={owner=0}'),
    ('0={}', '3={owner=1} 3={owner=0}'),
])
def test_conflicting_fleet_ownership_is_not_selected(tmp_path, countries, fleet):
    text = 'date=2200.01.01 player={country=0} country={' + countries + '} fleet={' + fleet + '}'
    state = read_save(write_save(tmp_path, text))
    assert state['fleets'] == []
    assert 'fleets' in state['metadata']['missing_fields']


def test_empty_fleet_manager_establishes_known_empty_player_fleet(tmp_path):
    text = '''date=2200.01.01 player={country=0}
    country={0={fleets_manager={owned_fleets={}}}} fleet={3={name="Unidentified"}}'''
    state = read_save(write_save(tmp_path, text))
    assert state['fleets'] == []
    assert 'fleets' not in state['metadata']['missing_fields']


def test_deleted_fleet_slots_do_not_invalidate_an_exhaustive_player_fleet_list(tmp_path):
    text = '''date=2200.01.01 player={country=0}
    country={0={fleets_manager={owned_fleets={{fleet=3}}}}}
    fleet={
      3={ship_class="shipclass_military" actions={wait={time=30}} properties={mobile=yes}}
      4={ship_class="shipclass_military"}
      28=none 30=none 245=none
    }'''
    state = read_save(write_save(tmp_path, text))
    assert [fleet['id'] for fleet in state['fleets']] == ['3']
    assert state['fleets_by_id']['3']['actions'] == {'wait': {'time': 30}}
    assert 'fleets' not in state['metadata']['missing_fields']


@pytest.mark.parametrize('owned,fleets', [
    ('{{fleet=3}}', '3=none 28=none'),
    ('{{fleet=999}}', '3={} 28=none'),
    ('{{fleet=3}}', '3={owner=1} 28=none'),
    ('{{fleet=3}}', '3={} 3={} 28=none'),
    ('{{fleet=3}}', '3={owner=unknown} 28=none'),
    ('{{fleet=3} {fleet=3}}', '3={} 28=none'),
    ('{{fleet=3}}', '3={} 28=unexpected'),
    ('{{fleet=3}}', '3={} none'),
    ('{{fleet=3}}', '3={} unexpected=none'),
])
def test_deleted_slots_do_not_hide_missing_conflicting_duplicate_or_unknown_fleets(tmp_path, owned, fleets):
    text = ('date=2200.01.01 player={country=0} '
            'country={0={fleets_manager={owned_fleets=' + owned + '}}} fleet={' + fleets + '}')
    state = read_save(write_save(tmp_path, text))
    assert 'fleets' in state['metadata']['missing_fields']


def test_deleted_slot_without_exhaustive_manager_keeps_fleet_set_unknown(tmp_path):
    text = 'date=2200.01.01 player={country=0} country={0={}} fleet={3={owner=0} 28=none}'
    assert 'fleets' in read_save(write_save(tmp_path, text))['metadata']['missing_fields']


def test_fleet_unset_system_is_not_a_system_identifier(tmp_path):
    text = '''date=2200.01.01 player={country=0} country={0={}}
    fleet={3={owner=0 movement_manager={coordinate={origin=4294967295}}}}'''
    state = read_save(write_save(tmp_path, text))
    assert 'system_id' not in state['fleets_by_id']['3']


@pytest.mark.parametrize('side', [
    'attackers={0}', 'attacker={country=0}', 'attackers={1={country=0}}',
    'attackers={{country=0}}', 'attacker={country=1} attacker={country=0}',
    'attackers={members={{country=0 war_exhaustion=0.2}}}',
])
def test_detects_wars(tmp_path, side):
    state = read_save(write_save(tmp_path, FIXTURE.replace('war={}', 'war={7={' + side + '}}')))
    assert state['at_war'] is True
    assert state['metadata']['war_status_known'] is True


def test_multiple_foreign_wars_establish_peace(tmp_path):
    wars = '''war={
    1={attackers={{country=1} {country=2}} defenders={{country=3}}}
    2={attacker={country=4} attacker={country=5} defender={country=6}}
    }'''
    state = read_save(write_save(tmp_path, FIXTURE.replace('war={}', wars)))
    assert state['at_war'] is False
    assert state['metadata']['war_status_known'] is True


@pytest.mark.parametrize('war', [
    'attackers={unexpected="unknown"} defenders={country=1}',
    'attackers={{country=1} {unexpected="unknown"}} defenders={country=2}',
    'attackers={country=1}',
    'unrecognized_side={country=0}',
    'attackers=unknown defenders={country=1}',
])
def test_unrecognized_war_participants_cannot_establish_peace(tmp_path, war):
    state = read_save(write_save(tmp_path, FIXTURE.replace('war={}', 'war={1={' + war + '}}')))
    assert state['at_war'] is False
    assert state['metadata']['war_status_known'] is False
    assert 'at_war' in state['metadata']['missing_fields']


def test_campaign_id_comes_from_galaxy_name_not_seed(tmp_path):
    text = FIXTURE + '\ngalaxy={name="ccdf5e03-7ad7-4455-8e85-1ca7f1281040" random_seed=123}'
    assert read_save(write_save(tmp_path, text))['metadata']['campaign_id'] == 'ccdf5e03-7ad7-4455-8e85-1ca7f1281040'
    assert read_save(write_save(tmp_path))['metadata']['campaign_id'] is None


def test_bad_encoding_and_missing_file(tmp_path):
    path = tmp_path / 'bad.sav'
    path.write_bytes(b'\xff\xfe\x01')
    with pytest.raises(SaveError, match='textual'):
        read_save(path)
    with pytest.raises(SaveError):
        read_save(tmp_path / 'missing.sav')


def test_decompression_size_limit(tmp_path):
    path = write_save(tmp_path, '#' + 'x' * 5000, zipped=True)
    assert path.stat().st_size < 1000
    with pytest.raises(SaveError, match='Decompressed'):
        read_save(path, max_bytes=1000)


def test_duplicate_gamestate_members_rejected(tmp_path):
    path = tmp_path / 'duplicate.sav'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('gamestate', FIXTURE)
        with pytest.warns(UserWarning):
            archive.writestr('gamestate', FIXTURE)
    with pytest.raises(SaveError, match='exactly one'):
        read_save(path)


def test_unknown_country_and_missing_date(tmp_path):
    with pytest.raises(SaveError, match='absent'):
        read_save(write_save(tmp_path), country_id='99')
    with pytest.raises(SaveError, match='date'):
        read_save(write_save(tmp_path, 'player={country=0} country={0={}}'))
