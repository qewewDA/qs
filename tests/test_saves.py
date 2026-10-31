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
