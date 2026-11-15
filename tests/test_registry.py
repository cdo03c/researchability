"""Tests for declarative gate loading, DAG validation, and field profile filtering."""

import pytest

from researchability.models import (
    EvaluatorKind,
    FieldProfile,
    GateDefinition,
    ProblemDomain,
)
from researchability.registry import GateRegistry, GateRegistryError


def _make_gate(
    gate_id: str,
    stage: int = 1,
    depends_on: list[str] | None = None,
) -> GateDefinition:
    return GateDefinition(
        id=gate_id,
        stage=stage,
        name=gate_id,
        description="test gate",
        problem_domain=ProblemDomain.TRANSPARENCY_REPORTING,
        evaluator_kind=EvaluatorKind.DETERMINISTIC,
        depends_on=depends_on or [],
    )


def test_default_gates_yaml_loads_and_covers_all_domains() -> None:
    registry = GateRegistry.from_yaml()
    gates = registry.all_gates()
    assert len(gates) >= 25
    stages = [g.stage for g in gates]
    assert stages == sorted(stages)

    domains_present = {g.problem_domain for g in gates}
    for required_domain in ProblemDomain:
        assert required_domain in domains_present


def test_profile_filtering_activates_domain_overlays() -> None:
    registry = GateRegistry.from_yaml()
    ml_ids = {g.id for g in registry.gates_for_profile(FieldProfile.ML_CS)}
    bio_ids = {g.id for g in registry.gates_for_profile(FieldProfile.BIOINFORMATICS)}
    wet_ids = {g.id for g in registry.gates_for_profile(FieldProfile.BIOMEDICINE_WETLAB)}

    assert "G2.6_BIO_REFERENCE_PINNED" in bio_ids
    assert "G2.6_BIO_REFERENCE_PINNED" not in ml_ids
    assert "G2.7_WETLAB_MATERIALS_RRID" in wet_ids
    assert "G2.7_WETLAB_MATERIALS_RRID" not in bio_ids


def test_registry_rejects_unknown_dependency() -> None:
    with pytest.raises(GateRegistryError, match="unknown gate"):
        GateRegistry([_make_gate("G1.1", stage=1, depends_on=["G0.MISSING"])])


def test_registry_rejects_cycle() -> None:
    with pytest.raises(GateRegistryError, match="Cycle detected"):
        GateRegistry(
            [
                _make_gate("G1.A", stage=1, depends_on=["G1.B"]),
                _make_gate("G1.B", stage=1, depends_on=["G1.A"]),
            ]
        )


def test_registry_rejects_later_stage_dependency() -> None:
    with pytest.raises(GateRegistryError, match="later-stage"):
        GateRegistry(
            [
                _make_gate("G1.A", stage=1, depends_on=["G2.B"]),
                _make_gate("G2.B", stage=2, depends_on=[]),
            ]
        )
