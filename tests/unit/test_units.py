from __future__ import annotations

import pytest

from capa.core.errors import ConfigError
from capa.core.units import canonicalize_unit, parse_unit, units_compatible


class TestUnits:
    def test_celsius_alias(self) -> None:
        assert canonicalize_unit("deg C") == "degree_Celsius"
        assert canonicalize_unit("degC") == "degree_Celsius"
        assert canonicalize_unit("DEG C") == "degree_Celsius"

    def test_compound_units(self) -> None:
        assert "gram" in canonicalize_unit("g/min")
        assert "kilopascal" in canonicalize_unit("kPa")

    def test_compatibility(self) -> None:
        assert units_compatible("V", "volt")
        assert units_compatible("degC", "K")
        assert not units_compatible("V", "kg")
        assert not units_compatible("kPa", "slpm")

    def test_unknown_rejected(self) -> None:
        with pytest.raises(ConfigError):
            parse_unit("not_a_unit_xyz")

    def test_empty_rejected(self) -> None:
        from pydantic import BaseModel

        from capa.core.units import UnitStr

        class M(BaseModel):
            u: UnitStr

        with pytest.raises(Exception):
            M(u="")

    def test_unit_str_pydantic(self) -> None:
        from pydantic import BaseModel

        from capa.core.units import UnitStr

        class M(BaseModel):
            u: UnitStr

        assert M(u="kPa").u == "kPa"
        with pytest.raises(Exception):
            M(u="bogus_unit")
