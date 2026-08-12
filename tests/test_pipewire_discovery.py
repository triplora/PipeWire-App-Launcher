import json
import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

from pipewire_launcher.pipewire_discovery import (
    AssociationConfidence,
    DiscoveryState,
    PipeWireDumpParseError,
    PipeWireDiscoverySnapshot,
    PipeWireNode,
    PipeWirePort,
    PortDirection,
    parse_pw_dump,
)


def item(object_id, object_type, **properties):
    return {"id": object_id, "type": object_type, "info": {"props": properties}}


def snapshot_items():
    return [
        item(20, "PipeWire:Interface:Port", **{
            "node.id": "10", "port.name": "capture", "port.direction": "INPUT",
        }),
        item(3, "PipeWire:Interface:Client", **{
            "application.name": "Example App", "application.process.id": "4321",
        }),
        item(10, "PipeWire:Interface:Node", **{
            "node.name": "example-node", "node.description": "Example Node",
            "application.name": "Example App", "application.process.id": "4321",
            "client.id": "3", "media.class": "Audio/Source",
        }),
        item(21, "PipeWire:Interface:Port", **{
            "node.id": 10, "port.name": "playback", "port.alias": "out",
            "port.description": "Playback port", "port.direction": "output",
        }),
        item(999, "PipeWire:Interface:Metadata", **{"metadata.name": "default"}),
    ]


class PipeWireDiscoveryTests(unittest.TestCase):
    def test_parse_pw_dump_valid_document(self):
        nodes = parse_pw_dump(json.dumps(snapshot_items()))
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0].name, "example-node")

    def test_parse_pw_dump_accepts_bytes(self):
        nodes = parse_pw_dump(json.dumps(snapshot_items()).encode())
        self.assertEqual(nodes[0].process_id, 4321)

    def test_parse_pw_dump_rejects_invalid_utf8(self):
        with self.assertRaises(PipeWireDumpParseError):
            parse_pw_dump(b"\xff")

    def test_parse_pw_dump_rejects_invalid_json(self):
        with self.assertRaises(PipeWireDumpParseError):
            parse_pw_dump("not json")

    def test_parse_pw_dump_rejects_non_list_root(self):
        with self.assertRaises(PipeWireDumpParseError):
            parse_pw_dump(json.dumps({"objects": []}))

    def test_parse_pw_dump_accepts_missing_properties(self):
        nodes = parse_pw_dump(json.dumps([item(1, "PipeWire:Interface:Node")]))
        self.assertEqual(nodes[0].name, "")
        self.assertIsNone(nodes[0].process_id)

    def test_parse_pw_dump_ignores_unknown_object_types(self):
        nodes = parse_pw_dump(json.dumps([item(1, "PipeWire:Interface:Unknown")]))
        self.assertEqual(nodes, ())

    def test_parse_pw_dump_is_independent_of_object_order(self):
        forward = parse_pw_dump(json.dumps(snapshot_items()))
        reverse = parse_pw_dump(json.dumps(list(reversed(snapshot_items()))))
        self.assertEqual(forward, reverse)

    def test_parse_pw_dump_normalizes_numeric_pid(self):
        nodes = parse_pw_dump(json.dumps([item(1, "PipeWire:Interface:Node", **{
            "application.process.id": 123,
        })]))
        self.assertEqual(nodes[0].process_id, 123)

    def test_parse_pw_dump_normalizes_text_pid(self):
        nodes = parse_pw_dump(json.dumps([item(1, "PipeWire:Interface:Node", **{
            "application.process.id": " 123 ",
        })]))
        self.assertEqual(nodes[0].process_id, 123)

    def test_parse_pw_dump_handles_invalid_pid_as_missing(self):
        nodes = parse_pw_dump(json.dumps([item(1, "PipeWire:Interface:Node", **{
            "application.process.id": "pid-123",
        })]))
        self.assertIsNone(nodes[0].process_id)

    def test_parse_pw_dump_associates_ports_with_node(self):
        nodes = parse_pw_dump(json.dumps(snapshot_items()))
        self.assertEqual([port.object_id for port in nodes[0].ports], [20, 21])
        self.assertEqual(nodes[0].ports[0].node_id, 10)

    def test_parse_pw_dump_preserves_multiple_nodes(self):
        payload = snapshot_items() + [item(11, "PipeWire:Interface:Node", **{"node.name": "second"})]
        nodes = parse_pw_dump(json.dumps(payload))
        self.assertEqual([node.object_id for node in nodes], [10, 11])

    def test_parse_pw_dump_preserves_multiple_ports(self):
        nodes = parse_pw_dump(json.dumps(snapshot_items()))
        self.assertEqual(len(nodes[0].ports), 2)

    def test_parse_pw_dump_normalizes_input_direction(self):
        nodes = parse_pw_dump(json.dumps(snapshot_items()))
        self.assertEqual(nodes[0].ports[0].direction, PortDirection.INPUT)

    def test_parse_pw_dump_normalizes_output_direction(self):
        nodes = parse_pw_dump(json.dumps(snapshot_items()))
        self.assertEqual(nodes[0].ports[1].direction, PortDirection.OUTPUT)

    def test_parse_pw_dump_uses_unknown_direction_when_missing(self):
        nodes = parse_pw_dump(json.dumps([item(1, "PipeWire:Interface:Node"), item(
            2, "PipeWire:Interface:Port", **{"node.id": 1, "port.name": "port"}
        )]))
        self.assertEqual(nodes[0].ports[0].direction, PortDirection.UNKNOWN)

    def test_parse_pw_dump_resolves_client_metadata(self):
        payload = [
            item(7, "PipeWire:Interface:Client", **{
                "application.name": "Client App", "application.process.id": "99",
            }),
            item(8, "PipeWire:Interface:Node", **{"client.id": 7}),
        ]
        node = parse_pw_dump(json.dumps(payload))[0]
        self.assertEqual(node.application_name, "Client App")
        self.assertEqual(node.process_id, 99)

    def test_parse_pw_dump_keeps_unassociated_port_out_of_nodes(self):
        payload = [item(1, "PipeWire:Interface:Node"), item(
            2, "PipeWire:Interface:Port", **{"node.id": 999}
        )]
        nodes = parse_pw_dump(json.dumps(payload))
        self.assertEqual(nodes[0].ports, ())

    def test_models_are_runtime_only_and_immutable(self):
        port = PipeWirePort(1, 2, "port")
        node = PipeWireNode(2, "node", ports=(port,))
        self.assertEqual(AssociationConfidence.LOW.value, "low")
        self.assertEqual(DiscoveryState.IDLE.value, "idle")
        with self.assertRaises(FrozenInstanceError):
            node.name = "changed"
        with self.assertRaises(TypeError):
            port.properties["changed"] = "value"

    def test_pipewire_node_defensively_freezes_ports(self):
        port = PipeWirePort(1, 2, "port")
        source = [port]
        node = PipeWireNode(2, "node", ports=source)
        source.clear()
        self.assertEqual(node.ports, (port,))
        self.assertIsInstance(node.ports, tuple)

    def test_pipewire_node_defensively_freezes_association_basis(self):
        source = ["process-pid"]
        node = PipeWireNode(2, "node", association_basis=source)
        source.append("name")
        self.assertEqual(node.association_basis, ("process-pid",))
        self.assertIsInstance(node.association_basis, tuple)

    def test_discovery_snapshot_defensively_freezes_nodes(self):
        node = PipeWireNode(2, "node")
        source = [node]
        snapshot = PipeWireDiscoverySnapshot(
            "profile", 1, datetime.now(timezone.utc), nodes=source
        )
        source.clear()
        self.assertEqual(snapshot.nodes, (node,))
        self.assertIsInstance(snapshot.nodes, tuple)

    def test_port_defensively_copies_diagnostic_properties(self):
        source = {"node.id": "2"}
        port = PipeWirePort(1, 2, "port", properties=source)
        source["node.id"] = "changed"
        source["new"] = "value"
        self.assertEqual(dict(port.properties), {"node.id": "2"})
        with self.assertRaises(TypeError):
            port.properties["blocked"] = "value"

    def test_mutating_source_collections_does_not_change_models(self):
        properties = {"key": "original"}
        port = PipeWirePort(1, 2, "port", properties=properties)
        ports = [port]
        basis = ["initial"]
        nodes = [PipeWireNode(2, "node", ports=ports, association_basis=basis)]
        snapshot = PipeWireDiscoverySnapshot(
            "profile", 1, datetime.now(timezone.utc), nodes=nodes
        )
        properties["key"] = "changed"
        ports.clear()
        basis.clear()
        nodes.clear()
        self.assertEqual(dict(snapshot.nodes[0].ports[0].properties), {"key": "original"})
        self.assertEqual(snapshot.nodes[0].ports, (port,))
        self.assertEqual(snapshot.nodes[0].association_basis, ("initial",))


if __name__ == "__main__":
    unittest.main()
