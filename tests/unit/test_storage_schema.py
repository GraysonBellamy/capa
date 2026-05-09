from __future__ import annotations

import pytest

from capa.storage.schema import (
    BUNDLE_SCHEMA_VERSION,
    MIGRATIONS,
    BundleSchemaError,
    current_version,
    migrate,
)


class TestSchema:
    def test_current_is_v2(self) -> None:
        assert BUNDLE_SCHEMA_VERSION == 2
        assert current_version() == 2

    def test_migrate_already_current_is_passthrough(self) -> None:
        m = {"bundle_schema_version": 2, "extra": "kept"}
        out = migrate(m)
        assert out["bundle_schema_version"] == 2
        assert out["extra"] == "kept"

    def test_missing_version_raises(self) -> None:
        with pytest.raises(BundleSchemaError):
            migrate({})

    def test_newer_than_supported_raises(self) -> None:
        with pytest.raises(BundleSchemaError):
            migrate({"bundle_schema_version": 99})

    def test_unregistered_step_raises(self) -> None:
        # No v1 → v2 migration registered: v1 was unshipped, so we reject
        # v1 manifests loudly rather than silently upgrading.
        with pytest.raises(BundleSchemaError):
            migrate({"bundle_schema_version": 1})

    def test_registry_chains_in_order(self) -> None:
        # A synthetic v1 -> v2 migration to prove the chain works.
        try:
            MIGRATIONS[1] = lambda d: {**d, "added_in_v2": True}
            out = migrate({"bundle_schema_version": 1})
            assert out["bundle_schema_version"] == 2
            assert out["added_in_v2"] is True
        finally:
            MIGRATIONS.pop(1, None)
