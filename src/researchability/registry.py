"""Declarative gate registry loader and DAG validator."""

from __future__ import annotations

from collections import defaultdict
import heapq
from pathlib import Path

import yaml

from researchability.models import FieldProfile, GateDefinition


def default_gates_path() -> Path:
    """Locate configs/gates.yaml relative to the repository root or package."""
    repo_candidate = Path(__file__).resolve().parents[2] / "configs" / "gates.yaml"
    if repo_candidate.exists():
        return repo_candidate
    cwd_candidate = Path.cwd() / "configs" / "gates.yaml"
    if cwd_candidate.exists():
        return cwd_candidate
    raise FileNotFoundError("Could not locate configs/gates.yaml")


class GateRegistryError(ValueError):
    """Raised when gate definitions contain invalid references or cycles."""


class GateRegistry:
    """Validated collection of gate definitions supporting profile filtering and topological order."""

    def __init__(self, gates: list[GateDefinition], version: str = "1.0") -> None:
        self.version = version
        self._gates: dict[str, GateDefinition] = {}
        for gate in gates:
            if gate.id in self._gates:
                raise GateRegistryError(f"Duplicate gate ID: {gate.id}")
            self._gates[gate.id] = gate
        self._ordered_ids = self._validate_dag()

    @classmethod
    def from_yaml(cls, path: Path | None = None) -> GateRegistry:
        """Load and validate gate definitions from a YAML file."""
        target = path or default_gates_path()
        raw = yaml.safe_load(target.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or "gates" not in raw:
            raise GateRegistryError(f"Invalid gate config structure in {target}")
        gates = [GateDefinition.model_validate(item) for item in raw["gates"]]
        return cls(gates=gates, version=str(raw.get("version", "1.0")))

    def _validate_dag(self) -> list[str]:
        """Ensure all dependencies exist, point to <= stage, and form a strict DAG."""
        in_degree: dict[str, int] = {gid: 0 for gid in self._gates}
        dependents: dict[str, list[str]] = defaultdict(list)

        for gid, gate in self._gates.items():
            for dep_id in gate.depends_on:
                if dep_id not in self._gates:
                    raise GateRegistryError(f"Gate {gid} depends on unknown gate {dep_id}")
                if dep_id == gid:
                    raise GateRegistryError(f"Gate {gid} cannot depend on itself")
                dep_gate = self._gates[dep_id]
                if dep_gate.stage > gate.stage:
                    raise GateRegistryError(
                        f"Gate {gid} (stage {gate.stage}) depends on later-stage gate "
                        f"{dep_id} (stage {dep_gate.stage})"
                    )
                dependents[dep_id].append(gid)
                in_degree[gid] += 1

        heap: list[tuple[int, str]] = [
            (self._gates[gid].stage, gid)
            for gid, deg in in_degree.items()
            if deg == 0
        ]
        heapq.heapify(heap)
        ordered: list[str] = []

        while heap:
            _, current = heapq.heappop(heap)
            ordered.append(current)
            for child in dependents[current]:
                in_degree[child] -= 1
                if in_degree[child] == 0:
                    heapq.heappush(heap, (self._gates[child].stage, child))

        if len(ordered) != len(self._gates):
            unresolved = sorted(set(self._gates) - set(ordered))
            raise GateRegistryError(f"Cycle detected in gate dependencies: {unresolved}")
        return ordered

    def get(self, gate_id: str) -> GateDefinition:
        """Return a gate definition by ID."""
        if gate_id not in self._gates:
            raise KeyError(f"Unknown gate ID: {gate_id}")
        return self._gates[gate_id]

    def all_gates(self) -> list[GateDefinition]:
        """Return all gates in topological stage order."""
        return [self._gates[gid] for gid in self._ordered_ids]

    def gates_for_profile(self, profile: FieldProfile) -> list[GateDefinition]:
        """Return active gates in topological order for a given disciplinary profile."""
        active: list[GateDefinition] = []
        for gid in self._ordered_ids:
            gate = self._gates[gid]
            if FieldProfile.CORE in gate.profiles or profile in gate.profiles:
                active.append(gate)
        return active
