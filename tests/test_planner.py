"""Exercise real urllib requests against a loopback OpenAI-compatible fixture."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import pytest

from stellaris_autopilot.config import Action, Config
from stellaris_autopilot.planner import LLMPlanner, RulePlanner


def tool_response(action_id="select_physics", **overrides):
    arguments = {
        "action_id": action_id,
        "reason": "Develop technology while retaining alloy reserves",
        "strategy": {"economy": "Keep alloys positive", "research": "Select physics",
                     "military": "Defend if attacked", "diplomacy": "Avoid wars"},
        **overrides,
    }
    return {"choices": [{"message": {"tool_calls": [{"id": "test-call", "type": "function",
        "function": {"name": "request_action", "arguments": json.dumps(arguments)}}]}}]}


@pytest.fixture
def model_server(monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.server.requests.append({
                "path": self.path,
                "headers": dict(self.headers),
                "body": json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
            })
            self.send_response(self.server.response_code)
            self.send_header("Content-Type", "application/json")
            for name, value in self.server.response_headers.items():
                self.send_header(name, value)
            self.end_headers()
            body = self.server.response
            self.wfile.write(body if isinstance(body, bytes) else json.dumps(body).encode())

        def do_GET(self):
            self.server.requests.append({"path": self.path, "headers": dict(self.headers)})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(tool_response()).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.requests = []
    server.response_code = 200
    server.response_headers = {}
    server.response = tool_response()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("AUTOPILOT_LLM_BASE_URL", f"http://127.0.0.1:{server.server_port}/v1")
    monkeypatch.setenv("AUTOPILOT_LLM_MODEL", "test-model")
    monkeypatch.delenv("AUTOPILOT_LLM_API_KEY", raising=False)
    # Tests contact only their own loopback listener, never external endpoints.
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture
def planner_inputs(tmp_path):
    action = Action("select_physics", "Choose a calibrated physics technology", "research",
                    steps=[{"kind": "key", "key": "f4"}],
                    expected=[{"path": "research.physics", "op": "changed"}])
    unauthorized = Action("declare_war", "Unauthorized action", "diplomacy",
                          steps=[{"kind": "key", "key": "f1"}],
                          expected=[{"path": "at_war", "op": "eq", "value": True}])
    config = Config(tmp_path / "private-save.sav", actions={action.id: action, unauthorized.id: unauthorized},
                    allowed_actions=[action.id])
    state = {"country_id": "0", "name": "Empire with untrusted game text", "at_war": False,
             "research": {"physics": "tech_old"},
             "metadata": {"source": "C:/private/user/save.sav", "game_version": "4.test",
                          "war_status_known": True}}
    return state, config


def test_real_http_tool_request_uses_local_authority_and_redacts_source(model_server, planner_inputs):
    state, config = planner_inputs
    proposal = LLMPlanner().propose("未来十年优先科技", state, config, [])
    assert proposal.action_id == "select_physics"
    assert json.loads(proposal.reason)["strategy"]["military"] == "Defend if attacked"
    request = model_server.requests[0]
    assert request["path"] == "/v1/chat/completions"
    body = request["body"]
    assert body["model"] == "test-model"
    function = body["tools"][0]["function"]
    assert function["name"] == "request_action"
    assert function["parameters"]["properties"]["action_id"]["enum"] == ["select_physics", "wait"]
    user_payload = json.loads(body["messages"][1]["content"])
    assert [action["id"] for action in user_payload["available_actions"]] == ["select_physics"]
    assert "steps" not in user_payload["available_actions"][0]
    assert "source" not in user_payload["state"]["metadata"]
    assert "private/user" not in json.dumps(body)
    assert "Authorization" not in request["headers"]


def test_wait_is_not_an_action(model_server, planner_inputs):
    model_server.response = tool_response("wait")
    state, config = planner_inputs
    assert LLMPlanner().propose("wait", state, config, []).action_id is None


def test_no_authorized_actions_skips_provider(model_server, planner_inputs):
    state, config = planner_inputs
    config.allowed_actions = []
    assert LLMPlanner().propose("goal", state, config, []).action_id is None
    assert model_server.requests == []


@pytest.mark.parametrize("response", [
    b"not JSON",
    {},
    {"choices": []},
    {"choices": [{"message": {"content": "Ignore tools and execute arbitrary input"}}]},
    tool_response("declare_war"),
    tool_response("not_configured"),
    tool_response(reason=["not a string"]),
    tool_response(strategy={"economy": "incomplete"}),
    tool_response(unexpected="arbitrary console commands"),
])
def test_malformed_or_unauthorized_responses_are_rejected(model_server, planner_inputs, response):
    model_server.response = response
    state, config = planner_inputs
    with pytest.raises(ValueError):
        LLMPlanner().propose("goal", state, config, [])


def test_multiple_tool_calls_are_rejected(model_server, planner_inputs):
    response = tool_response()
    calls = response["choices"][0]["message"]["tool_calls"]
    calls.append(calls[0].copy())
    model_server.response = response
    state, config = planner_inputs
    with pytest.raises(ValueError, match="Exactly one"):
        LLMPlanner().propose("goal", state, config, [])


@pytest.mark.parametrize("url", [
    "file:///private/local.json", "ftp://provider.example/v1", "gopher://localhost/v1",
    "http://provider.example/v1", "http://127.0.0.1.evil.example/v1",
    "https://user:password@provider.example/v1", "https://provider.example/v1?api_key=secret",
    "https://provider.example/v1#secret",
    "https:///v1",
])
def test_rejects_non_https_remote_or_credential_bearing_endpoint(monkeypatch, url):
    monkeypatch.setenv("AUTOPILOT_LLM_BASE_URL", url)
    with pytest.raises(ValueError):
        LLMPlanner()


def test_http_failure_does_not_echo_private_provider_body(model_server, planner_inputs):
    model_server.response_code = 401
    model_server.response = b"PRIVATE_PROVIDER_BODY_API_SECRET_AND_SAVE_DATA"
    state, config = planner_inputs
    with pytest.raises(ValueError, match="LLM HTTP 401") as caught:
        LLMPlanner().propose("goal", state, config, [])
    assert "PRIVATE_PROVIDER_BODY" not in str(caught.value)


def test_redirect_is_rejected_before_any_followup_request(model_server, planner_inputs, monkeypatch):
    model_server.response_code = 302
    model_server.response_headers = {"Location": f"http://127.0.0.1:{model_server.server_port}/redirected"}
    monkeypatch.setenv("AUTOPILOT_LLM_API_KEY", "test-secret")
    state, config = planner_inputs
    with pytest.raises(ValueError, match="302"):
        LLMPlanner().propose("goal", state, config, [])
    assert len(model_server.requests) == 1


def test_rule_planner_only_defends_when_war_is_known(planner_inputs):
    state, config = planner_inputs
    defense = Action("defend", "Defend", "defense", event="saa.20",
                     expected=[{"path": "fleets", "op": "changed"}])
    config.actions[defense.id] = defense
    config.allowed_actions.append(defense.id)
    state["at_war"] = True
    state["metadata"]["war_status_known"] = False
    assert RulePlanner().propose("goal", state, config, []).action_id == "select_physics"
    state["metadata"]["war_status_known"] = True
    assert RulePlanner().propose("goal", state, config, []).action_id == "defend"
