"""The n8n workflow files: well-formed, up to date, and only calling endpoints that exist."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from snipmint.automation.settings import EVENT_PATHS
from snipmint.server import create_app

ROOT = Path(__file__).resolve().parents[2]
FILES = sorted((ROOT / "workflows" / "n8n").glob("*.json"))
NODE_VERSIONS = {
    "n8n-nodes-base.webhook": {2}, "n8n-nodes-base.httpRequest": {4.2}, "n8n-nodes-base.if": {2.2},
    "n8n-nodes-base.wait": {1.1}, "n8n-nodes-base.scheduleTrigger": {1.2}, "n8n-nodes-base.errorTrigger": {1},
    "n8n-nodes-base.splitOut": {1}, "n8n-nodes-base.splitInBatches": {3}, "n8n-nodes-base.code": {2},
    "n8n-nodes-base.telegram": {1.2}, "n8n-nodes-base.discord": {2}, "n8n-nodes-base.emailSend": {2.1},
}  # fmt: skip
EXPECTED = {"automated-publishing.json", "scheduled-publishing.json", "batch-publishing.json",
            "metadata-approval.json", "publishing-notifications.json", "error-handling.json"}  # fmt: skip


def load(p: Path) -> dict:
    return json.loads(p.read_text(encoding="utf-8"))


def test_all_files_present_and_generated_from_the_script():
    import importlib.util

    assert {p.name for p in FILES} == EXPECTED
    spec = importlib.util.spec_from_file_location("build", ROOT / "scripts" / "build_n8n_workflows.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for name, data in mod.build().items():
        assert load(ROOT / "workflows" / "n8n" / name) == data, f"{name} is stale: run scripts/build_n8n_workflows.py"


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_structure(path):
    wf = load(path)
    assert wf["name"].startswith("Snipmint: ") and wf["active"] is False
    names = [n["name"] for n in wf["nodes"]]
    assert len(names) == len(set(names)), "node names must be unique"
    assert len({n["id"] for n in wf["nodes"]}) == len(names)
    for n in wf["nodes"]:
        assert n["typeVersion"] in NODE_VERSIONS[n["type"]], (n["name"], n["type"], n["typeVersion"])
        assert len(n["position"]) == 2
    for src, outs in wf["connections"].items():
        assert src in names
        for branch in outs["main"]:
            for link in branch:
                assert link["node"] in names and link["type"] == "main"
    # every node except the trigger is reachable
    triggers = [n["name"] for n in wf["nodes"] if n["type"].endswith(("webhook", "scheduleTrigger", "errorTrigger"))]
    assert len(triggers) == 1
    seen, todo = set(), list(triggers)
    while todo:
        cur = todo.pop()
        if cur in seen:
            continue
        seen.add(cur)
        for branch in wf["connections"].get(cur, {}).get("main", []):
            todo.extend(link["node"] for link in branch)
    assert seen == set(names), f"unreachable nodes: {set(names) - seen}"
    # no secrets in the files
    text = path.read_text(encoding="utf-8")
    assert "X-Snipmint-Secret" not in text and "token" not in text.lower().replace("idempotency", "")


def test_webhook_paths_match_snipmint_events():
    paths = {n["parameters"]["path"] for p in FILES for n in load(p)["nodes"] if n["type"] == "n8n-nodes-base.webhook"}
    assert paths == set(EVENT_PATHS.values())
    for p in FILES:
        for n in load(p)["nodes"]:
            if n["type"] == "n8n-nodes-base.webhook":
                assert n["parameters"]["authentication"] == "headerAuth" and n["credentials"]["httpHeaderAuth"]


def test_http_calls_hit_real_snipmint_routes():
    app = create_app()
    routes = [(m, re.compile("^" + re.sub(r"\{[^}]+\}", "[^/]+", r.path) + "$"))
              for r in app.routes if hasattr(r, "methods") for m in r.methods]  # fmt: skip
    calls = 0
    for p in FILES:
        for n in load(p)["nodes"]:
            if n["type"] != "n8n-nodes-base.httpRequest":
                continue
            url = n["parameters"]["url"].lstrip("=")
            assert url.startswith("http://snipmint:8000/api/integrations/n8n/"), url
            path = re.sub(r"\{\{.*?\}\}", "x", url.split("8000", 1)[1]).split("?")[0]
            method = n["parameters"]["method"]
            assert any(m == method and rx.match(path) for m, rx in routes), f"{method} {path} is not a Snipmint route"
            assert n["parameters"]["authentication"] == "genericCredentialType"
            assert n["credentials"]["httpHeaderAuth"]["name"] == "Snipmint API"
            if method == "POST" and path.endswith("/publish"):
                headers = {h["name"] for h in n["parameters"]["headerParameters"]["parameters"]}
                assert "Idempotency-Key" in headers, f"{p.name}: publishing must be idempotent"
            calls += 1
    assert calls >= 10


def test_notifiers_ship_disabled_and_never_hardcode_credentials():
    for p in FILES:
        for n in load(p)["nodes"]:
            if n["type"] in ("n8n-nodes-base.telegram", "n8n-nodes-base.discord", "n8n-nodes-base.emailSend"):
                assert n.get("disabled") is True and n.get("onError") == "continueRegularOutput"
