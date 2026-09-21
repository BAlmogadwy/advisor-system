"""PostgreSQL proves empty-scope serialization and mutable-catalogue locking."""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from django.db import connection, connections

from core.models import (
    ElectiveCourse,
    ElectiveMappingScope,
    ElectiveTermMapping,
    ProgrammeRequirement,
)
from core.services import elective_validation as service

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def postgres():
    if connection.vendor != "postgresql":
        if os.environ.get("REQUIRE_POSTGRES_TESTS") == "1":
            pytest.fail("PostgreSQL elective concurrency tests were required")
        pytest.skip("Requires PostgreSQL locking semantics")


@pytest.fixture
def world():
    ProgrammeRequirement.objects.create(
        program="LOCK", course_code="LE1", type="Program Elective", credit_hours=3
    )
    return {
        code: ElectiveCourse.objects.create(programme="LOCK", course_code=code, credit_hours=3)
        for code in ("LX401", "LX402")
    }


def replace(code):
    try:
        return service.replace_mappings(
            "1448", 1, "LOCK", [{"placeholder_code": "LE1", "course_code": code}]
        )
    finally:
        connections.close_all()


@pytest.mark.parametrize("existing_scope", [False, True])
def test_concurrent_replacements_are_complete_and_serialized(world, monkeypatch, existing_scope):
    if existing_scope:
        ElectiveMappingScope.objects.create(academic_year="1448", term=1, programme="LOCK")
        ElectiveTermMapping.objects.create(
            academic_year="1448",
            term=1,
            programme="LOCK",
            placeholder_code="LE1",
            elective=world["LX402"],
        )
    first_inside, second_inside, release, second_started = (threading.Event() for _ in range(4))
    original = service._lock_validation_inputs
    calls = []

    def hold_first():
        original()
        calls.append(True)
        if len(calls) == 1:
            first_inside.set()
            assert release.wait(10), "test failed to release publication transaction"
        else:
            second_inside.set()

    monkeypatch.setattr(service, "_lock_validation_inputs", hold_first)

    def second():
        second_started.set()
        return replace("LX402")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(replace, "LX401")
        assert first_inside.wait(10)
        later = pool.submit(second)
        try:
            assert second_started.wait(5)
            assert not second_inside.wait(0.3), "the second publisher bypassed the scope lock"
        finally:
            release.set()
        assert first.result(timeout=10)["ok"]
        assert later.result(timeout=10)["ok"]
    assert second_inside.is_set()
    assert list(ElectiveTermMapping.objects.values_list("elective__course_code", flat=True)) == [
        "LX402"
    ]
    assert ElectiveMappingScope.objects.count() == 1


def test_catalogue_mutation_waits_until_validated_replacement_commits(world, monkeypatch):
    validation_locked, release, mutation_started, mutation_done = (
        threading.Event() for _ in range(4)
    )
    original = service._lock_validation_inputs

    def hold_publication():
        original()
        validation_locked.set()
        assert release.wait(10)

    def mutate():
        try:
            mutation_started.set()
            ElectiveCourse.objects.filter(pk=world["LX401"].pk).update(credit_hours=2)
            mutation_done.set()
        finally:
            connections.close_all()

    monkeypatch.setattr(service, "_lock_validation_inputs", hold_publication)
    with ThreadPoolExecutor(max_workers=2) as pool:
        publication = pool.submit(replace, "LX401")
        assert validation_locked.wait(10)
        mutation = pool.submit(mutate)
        try:
            assert mutation_started.wait(5)
            assert not mutation_done.wait(0.3), "catalogue changed between validation and commit"
        finally:
            release.set()
        assert publication.result(timeout=10)["ok"]
        mutation.result(timeout=10)
    assert mutation_done.is_set()
    # A subsequent catalogue edit cannot authorize stale planner options.
    assert service.ElectiveSelection("LOCK", "1448", 1).resolve("LE1")[0] == "INVALID_MAPPING"
