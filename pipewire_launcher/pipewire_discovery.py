"""Pure, runtime-only models and parser for ``pw-dump -N`` snapshots.

This module deliberately has no Qt, subprocess, filesystem, or PipeWire
runtime dependency.  Query execution and association policy belong to later
phases of the feature.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping


class PortDirection(str, Enum):
    INPUT = "input"
    OUTPUT = "output"
    UNKNOWN = "unknown"


class AssociationConfidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class DiscoveryState(str, Enum):
    IDLE = "idle"
    PROBING = "probing"
    AVAILABLE = "available"
    CANDIDATES = "candidates"
    EMPTY = "empty"
    UNAVAILABLE = "unavailable"
    ERROR = "error"


class PipeWireDumpParseError(ValueError):
    """Raised when a ``pw-dump`` payload cannot be parsed safely."""


@dataclass(frozen=True)
class PipeWirePort:
    """Immutable runtime representation of a PipeWire port."""

    object_id: int
    node_id: int | None
    name: str
    alias: str | None = None
    description: str | None = None
    direction: PortDirection = PortDirection.UNKNOWN
    properties: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "properties",
            MappingProxyType(dict(self.properties)),
        )


@dataclass(frozen=True)
class PipeWireNode:
    """Immutable runtime representation of a PipeWire node and its ports."""

    object_id: int
    name: str
    description: str | None = None
    application_name: str | None = None
    process_id: int | None = None
    client_id: int | None = None
    media_class: str | None = None
    ports: tuple[PipeWirePort, ...] = ()
    association_basis: tuple[str, ...] = ()
    association_confidence: AssociationConfidence = AssociationConfidence.LOW

    def __post_init__(self) -> None:
        object.__setattr__(self, "ports", tuple(self.ports))
        object.__setattr__(self, "association_basis", tuple(self.association_basis))


@dataclass(frozen=True)
class PipeWireDiscoverySnapshot:
    """Immutable runtime snapshot associated with one profile generation."""

    profile_id: str
    generation: int
    captured_at: datetime
    nodes: tuple[PipeWireNode, ...] = ()
    discovery_state: DiscoveryState = DiscoveryState.IDLE
    diagnostic: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "nodes", tuple(self.nodes))


def _properties(item: Mapping[str, Any]) -> dict[str, Any]:
    info = item.get("info")
    if not isinstance(info, Mapping):
        return {}
    props = info.get("props")
    return dict(props) if isinstance(props, Mapping) else {}


def _first(properties: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = properties.get(key)
        if value is not None and value != "":
            return value
    return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _runtime_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str):
        value = value.strip()
        if value.isdigit():
            return int(value)
    return None


def _diagnostic_properties(properties: Mapping[str, Any]) -> Mapping[str, str]:
    return MappingProxyType({str(key): str(value) for key, value in properties.items()})


def _direction(value: Any) -> PortDirection:
    normalized = str(value).strip().casefold() if value is not None else ""
    if normalized == PortDirection.INPUT.value:
        return PortDirection.INPUT
    if normalized == PortDirection.OUTPUT.value:
        return PortDirection.OUTPUT
    return PortDirection.UNKNOWN


def _object_id(item: Mapping[str, Any]) -> int | None:
    return _runtime_id(item.get("id"))


def parse_pw_dump(payload: str | bytes) -> tuple[PipeWireNode, ...]:
    """Parse a JSON snapshot produced by ``pw-dump -N``.

    Field precedence is intentionally explicit: names use the node/port
    specific property before ``object.name``; descriptions use the specific
    description before ``object.description``; application name uses
    ``application.name`` before ``application.binary``; and PID uses
    ``application.process.id`` before ``process.id``.  Client metadata fills
    only fields absent from the node itself.  Missing values remain ``None``
    or an empty string and are never synthesized.
    """

    if isinstance(payload, bytes):
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PipeWireDumpParseError("pw-dump payload is not valid UTF-8") from exc
    if not isinstance(payload, str):
        raise PipeWireDumpParseError("pw-dump payload must be str or bytes")

    try:
        document = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise PipeWireDumpParseError("pw-dump payload is not valid JSON") from exc
    if not isinstance(document, list):
        raise PipeWireDumpParseError("pw-dump JSON root must be a list")

    clients: dict[int, dict[str, Any]] = {}
    node_items: list[tuple[int, Mapping[str, Any], dict[str, Any]]] = []
    port_items: list[tuple[int, Mapping[str, Any], dict[str, Any]]] = []

    for item in document:
        if not isinstance(item, Mapping):
            continue
        object_id = _object_id(item)
        object_type = item.get("type")
        properties = _properties(item)
        if object_id is None:
            continue
        if object_type == "PipeWire:Interface:Client":
            clients[object_id] = properties
        elif object_type == "PipeWire:Interface:Node":
            node_items.append((object_id, properties, dict(properties)))
        elif object_type == "PipeWire:Interface:Port":
            port_items.append((object_id, properties, dict(properties)))

    ports_by_node: dict[int, list[PipeWirePort]] = {}
    for object_id, properties, _ in port_items:
        node_id = _runtime_id(properties.get("node.id"))
        port = PipeWirePort(
            object_id=object_id,
            node_id=node_id,
            name=_text(_first(properties, "port.name", "object.name")) or "",
            alias=_text(_first(properties, "port.alias")),
            description=_text(_first(properties, "port.description", "object.description")),
            direction=_direction(properties.get("port.direction")),
            properties=_diagnostic_properties(properties),
        )
        if node_id is not None:
            ports_by_node.setdefault(node_id, []).append(port)

    nodes: list[PipeWireNode] = []
    for object_id, properties, _ in node_items:
        client_id = _runtime_id(properties.get("client.id"))
        client_properties = clients.get(client_id, {}) if client_id is not None else {}
        application_name = _text(
            _first(
                properties,
                "application.name",
                "application.binary",
            )
            or _first(client_properties, "application.name", "application.binary")
        )
        process_id = _runtime_id(
            _first(properties, "application.process.id", "process.id")
        )
        if process_id is None:
            process_id = _runtime_id(
                _first(client_properties, "application.process.id", "process.id")
            )
        node = PipeWireNode(
            object_id=object_id,
            name=_text(_first(properties, "node.name", "object.name")) or "",
            description=_text(_first(properties, "node.description", "object.description")),
            application_name=application_name,
            process_id=process_id,
            client_id=client_id,
            media_class=_text(_first(properties, "media.class")),
            ports=tuple(sorted(ports_by_node.get(object_id, ()), key=lambda port: port.object_id)),
        )
        nodes.append(node)

    return tuple(sorted(nodes, key=lambda node: node.object_id))
