"""HTTP-level state simulator, NOT a Cypher parser or proof of Neptune execution."""

from contextlib import contextmanager
import json
from urllib.parse import parse_qs

import httpx

from app.graph.neptune import NeptuneGraphRepository
from app.graph.neptune_client import NeptuneClient


class FakeNeptune:
    def __init__(self):
        self.nodes = {}
        self.edges = {}
        self.calls = []

    def handle(self, request):
        assert request.method == "POST"
        assert str(request.url) == "https://neptune.invalid:8182/openCypher"
        form = parse_qs(request.content.decode())
        query, params = form["query"][0], json.loads(form["parameters"][0])
        self.calls.append((query, params))
        operation = query.splitlines()[0].removeprefix("// riskgraph:")
        rows = self.execute(operation, params)
        return httpx.Response(200, json={"results": rows})

    def node(self, identifier, kind, key, properties):
        self.nodes[identifier] = {
            "~id": identifier, "~entityType": "node", "~labels": [kind],
            "~properties": {"node_kind": kind, "node_key": key} | properties,
        }

    def execute(self, operation, p):
        if operation == "upsert":
            tid = p["transaction_node_id"]
            self.node(tid, "Transaction", p["transaction_id"], {key: p[key] for key in (
                "transaction_id", "amount", "currency", "card_token", "device_token", "event_time",
            )})
            self.edges = {key: value for key, value in self.edges.items()
                          if value[0] != tid or key in {p["card_edge_id"], p["device_edge_id"]}}
            self.node(p["card_node_id"], "Card", p["card_token"], {"card_token": p["card_token"]})
            self.edges[p["card_edge_id"]] = (tid, p["card_node_id"], "USES_CARD")
            if p["device_token"] is not None:
                self.node(p["device_node_id"], "Device", p["device_token"], {"device_token": p["device_token"]})
                self.edges[p["device_edge_id"]] = (tid, p["device_node_id"], "USES_DEVICE")
            return []
        if operation == "seed":
            return [{"node": self.nodes[p["node_id"]]}] if p["node_id"] in self.nodes else []
        if operation == "neighbors":
            refs = set()
            for source, target, _ in self.edges.values():
                if source == p["node_id"]:
                    refs.add(target)
                if target == p["node_id"]:
                    refs.add(source)
            nodes = [self.nodes[ref] for ref in refs if ref not in p["excluded_ids"]]
            def key(node):
                props = node["~properties"]
                return props["node_kind"], props["node_key"]
            return [{"node": node} for node in sorted(nodes, key=key)
                    if key(node) > (p["after_kind"], p["after_key"])][:100]
        if operation in {"card_history", "device_history"}:
            kind = "USES_CARD" if operation == "card_history" else "USES_DEVICE"
            refs = {source for source, target, relation in self.edges.values()
                    if target == p["token_node_id"] and relation == kind}
            nodes = [self.nodes[ref] for ref in refs]
            nodes.sort(key=lambda node: node["~properties"]["transaction_id"])
            return [{"node": node} for node in nodes
                    if p["lower"] <= node["~properties"]["event_time"] < p["cutoff"]
                    and node["~properties"]["transaction_id"] > p["after_id"]][:100]
        if operation == "edges":
            rows = []
            for identifier, (source, target, kind) in sorted(self.edges.items()):
                if source in p["selected_ids"] and target in p["selected_ids"] and identifier > p["after_id"]:
                    props = self.nodes[target]["~properties"]
                    rows.append({"edge_id": identifier,
                                 "transaction_id": self.nodes[source]["~properties"]["transaction_id"],
                                 "relationship_kind": kind, "target_kind": props["node_kind"],
                                 "target_key": props["node_key"]})
            return rows[:100]
        raise AssertionError(f"Unknown fake operation: {operation}")


@contextmanager
def fake_repository(server=None):
    server = server if server is not None else FakeNeptune()
    with httpx.Client(transport=httpx.MockTransport(server.handle)) as http:
        client = NeptuneClient("neptune.invalid", iam_auth_enabled=False, http_client=http)
        yield NeptuneGraphRepository(client)
