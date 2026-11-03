import asyncio
import json

import pytest

from stellaris_autopilot.knowledge import GameKnowledge, build_knowledge


@pytest.fixture
def game_dir(tmp_path):
    files = {
        "common/scripted_variables/base.txt": "@price=400 @days=360",
        "common/inline_scripts/buildings/cost.txt": '''cost={trigger={is_nomadic=no} $RESOURCE$=$PRICE$}
cost={trigger={is_nomadic=yes} alloys=40}''',
        "common/buildings/lab.txt": '''building_lab={base_buildtime=@days
resources={inline_script={script=buildings/cost RESOURCE=minerals PRICE=@price} upkeep={energy=2}}
prerequisites={tech_lab} modifier={job_physicist_add=60} unknown=@not_known}''',
        "common/ship_behaviors/basic.txt": 'ship_behavior={name=artillery preferred_attack_range=max} ship_behavior={name=swarm preferred_attack_range=5}',
        "localisation/simp_chinese/labels.yml": 'l_simp_chinese:\n building_lab:0 "研究实验室"\n building_lab_desc:0 "需要科研岗位"\n',
    }
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return tmp_path


def test_compile_resolves_variables_and_preserves_conditional_costs_and_sources(game_dir):
    knowledge = GameKnowledge(build_knowledge(game_dir, "4.test"))
    record = knowledge.lookup("buildings", "building_lab")
    assert record["definition"]["base_buildtime"] == 360
    assert record["definition"]["resources"]["cost"] == [
        {"trigger": {"is_nomadic": "no"}, "minerals": 400},
        {"trigger": {"is_nomadic": "yes"}, "alloys": 40},
    ]
    assert record["unresolved_symbols"] == ["@not_known"]
    assert "common/scripted_variables/base.txt" in record["source_hashes"]
    assert knowledge.search("研究实验室")["results"][0]["id"] == "building_lab"
    assert knowledge.status()["categories"]["ship_behaviors"] == 2
    assert knowledge.lookup("ship_behaviors", "swarm")["definition"]["preferred_attack_range"] == 5


def test_duplicate_ids_are_ambiguous_not_last_wins(game_dir):
    (game_dir / "common/buildings/second.txt").write_text("building_lab={base_buildtime=1}")
    result = GameKnowledge(build_knowledge(game_dir, "4.test")).lookup("buildings", "building_lab")
    assert result["status"] == "ambiguous" and "definition" not in result


def test_ambiguous_local_variable_cannot_fall_back_to_plausible_global_price(game_dir):
    (game_dir / "common/buildings/ambiguous.txt").write_text("@price=1 @price=2 building_wrong={cost=@price}")
    record = GameKnowledge(build_knowledge(game_dir, "4.test")).lookup("buildings", "building_wrong")
    assert record["definition"]["cost"] == "@price"
    assert record["unresolved_symbols"] == ["@price"]


@pytest.mark.parametrize("query,limit", [("", 8), ("a" * 201, 8), ("lab", True), ("lab", 31)])
def test_bad_queries_cannot_read_unbounded_data(game_dir, query, limit):
    knowledge = GameKnowledge(build_knowledge(game_dir, "4.test"))
    with pytest.raises(ValueError):
        knowledge.search(query, limit=limit)


def test_wiki_reference_requires_attribution_and_rejects_challenge_html(game_dir):
    page = {"title": "建筑", "url": "https://qunxing.huijiwiki.com/wiki/建筑", "revision": "123",
            "license": "source license as exported", "version": "4.test", "text": "建筑需要资源与建造时间。"}
    knowledge = GameKnowledge(build_knowledge(game_dir, "4.test", wiki_pages=[page]))
    assert knowledge.wiki_page("建筑")["revision"] == "123"
    assert knowledge.search("资源")["wiki_matches"][0]["url"] == page["url"]
    with pytest.raises(ValueError):
        build_knowledge(game_dir, "4.test", wiki_pages=[{**page, "text": "<!DOCTYPE html> challenge"}])
    with pytest.raises(ValueError):
        build_knowledge(game_dir, "4.test", wiki_pages=[{**page, "license": ""}])


def test_sections_and_unknown_inline_paths_preserve_unknown(game_dir):
    (game_dir / "common/buildings/extra.txt").write_text('building_extra={inline_script="../private"}')
    knowledge = GameKnowledge(build_knowledge(game_dir, "4.test"))
    assert knowledge.lookup("buildings", "building_lab", ["base_buildtime"])["definition"] == {"base_buildtime": 360}
    assert knowledge.lookup("buildings", "building_extra")["unresolved_symbols"] == ["inline_script:../private"]


def test_whole_game_reference_includes_strategic_choices_and_numeric_defines(game_dir):
    for directory, text in {
        "ascension_perks": 'ap_example={prerequisites={tech_lab} modifier={naval_cap_add=20}}',
        "traditions": 'tr_example={modifier={planet_jobs_produces_mult=0.05}}',
        "governments/civics": 'origin_example={is_origin=yes} civic_example={modifier={research_speed=0.1}}',
        "defines": 'NPop={POP_BULK_UNIT_SIZE=100}',
        "special_projects": 'special_project={key=PROJECT_EXAMPLE days_to_research=180}',
    }.items():
        folder = game_dir / "common" / directory
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "sample.txt").write_text(text)
    knowledge = GameKnowledge(build_knowledge(game_dir, "4.test"))
    for category, object_id in (("ascension_perks", "ap_example"), ("traditions", "tr_example"),
                                ("origins", "origin_example"), ("civics", "civic_example"),
                                ("defines", "NPop.POP_BULK_UNIT_SIZE"), ("special_projects", "PROJECT_EXAMPLE")):
        assert knowledge.lookup(category, object_id)["status"] == "found"
    assert knowledge.lookup("civics", "origin_example")["status"] == "not_found"


def test_empire_build_observes_user_choices_without_inventing_unset_perks():
    from stellaris_autopilot.pdx import loads
    from stellaris_autopilot.saves import _empire_build

    result = _empire_build(loads('government={origin=origin_example civics={civic_example}} ethos={ethics={ethic_xenophile}}'))
    assert result["government"]["value"]["origin"] == "origin_example"
    assert result["ascension_perks"] == {"known": False, "value": None}
    assert _empire_build(loads('traditions={} traditions={tr_example}'))["traditions"]["known"] is False


def test_mcp_knowledge_tools_are_readonly_version_bound_and_need_no_game(game_dir, tmp_path):
    pytest.importorskip("mcp")
    from stellaris_autopilot.direct_mcp import build_direct_server

    config = tmp_path / "config.json"
    config.write_text(json.dumps({"user_dir": str(tmp_path), "state_dir": str(tmp_path / "state"),
        "country_id": "0", "campaign_id": "x", "verified_game_version": "4.test", "session_token": "a" * 32}))
    server = build_direct_server(config, game_knowledge=GameKnowledge(build_knowledge(game_dir, "4.test")))
    tool = server._tool_manager.get_tool("get_game_definition")
    assert tool.annotations.readOnlyHint is True
    result = asyncio.run(server._tool_manager.call_tool("get_game_definition", {"category": "buildings", "object_id": "building_lab"}))
    assert result["definition"]["base_buildtime"] == 360
    config.write_text(config.read_text().replace("4.test", "4.other"))
    with pytest.raises(Exception, match="version"):
        asyncio.run(server._tool_manager.call_tool("get_game_definition", {"category": "buildings", "object_id": "building_lab"}))
