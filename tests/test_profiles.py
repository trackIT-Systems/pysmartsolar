import pytest

from smartsolar import profiles
from smartsolar.errors import InvalidWriteError, ProfileError
from smartsolar.profiles import Profile

CURRENT = [
    {"name": "float_voltage", "value": 13.5, "unit": "V"},
    {"name": "absorption_voltage", "value": 14.5, "unit": "V"},
    {"name": "battery_max_current", "value": 15.0, "unit": "A"},
]


def test_the_profile_settings_are_the_numeric_writable_registers_only():
    assert profiles.PROFILE_PARAMETERS == ("float_voltage", "absorption_voltage", "battery_max_current")  # not device_mode, not the load


def test_parse_normalises_values():
    p = profiles.parse_profile("values:\n  float_voltage: '13.6'\n  battery_max_current: 15\n", default_name="x")
    assert p.name == "x" and p.values == {"float_voltage": 13.6, "battery_max_current": 15.0}


@pytest.mark.parametrize(
    "text, message",
    [
        ("values:\n  device_mode: off\n", "not a profile setting"),  # a profile never switches the charger
        ("values:\n  load_output_control: off\n", "not a profile setting"),
        ("values:\n  battery_voltage: 12\n", "not a profile setting"),
        ("values: {}\n", "non-empty"),
        ("name: x\n", "non-empty"),
        ("- a\n", "expected a mapping"),
        ("values: [", "invalid YAML"),
        ("values:\n  float_voltage: 13.6\nextra: 1\n", "unknown key"),
    ],
)
def test_parse_rejects(text, message):
    with pytest.raises(ProfileError, match=message):
        profiles.parse_profile(text)


def test_bad_values_name_the_source():
    with pytest.raises(InvalidWriteError, match=r"f\.yml: float_voltage: 99 is outside"):
        profiles.parse_profile("values:\n  float_voltage: 99\n", source="f.yml")
    with pytest.raises(InvalidWriteError, match="not a number"):
        profiles.parse_profile("values:\n  float_voltage: high\n")


def test_save_and_load_round_trip(tmp_path):
    p = profiles.profile_from_values(CURRENT, name="mine", description="test")
    text = profiles.dump_profile(p, device="AA:BB")
    assert text.startswith("# smartsolar profile, saved ") and "# from device AA:BB" in text
    path = tmp_path / "mine.yml"
    path.write_text(text, encoding="utf-8")
    loaded = profiles.load_profile(str(path))
    assert (loaded.name, loaded.description, loaded.values) == ("mine", "test", p.values)
    assert p.values == {"float_voltage": 13.5, "absorption_voltage": 14.5, "battery_max_current": 15.0}


def test_profile_from_values_ignores_other_registers_and_missing_ones():
    p = profiles.profile_from_values([{"name": "battery_voltage", "value": 13.3}, {"name": "float_voltage", "value": 13.5}, {"name": "absorption_voltage", "value": None}], name="x")
    assert p.values == {"float_voltage": 13.5}
    with pytest.raises(ProfileError):
        profiles.profile_from_values([{"name": "battery_voltage", "value": 13.3}], name="x")


def test_load_by_name_and_path(tmp_path):
    assert profiles.load_profile("lifepo4-12v").values == {"absorption_voltage": 14.2, "float_voltage": 13.5}
    with pytest.raises(ProfileError, match="available: agm-spiral-cell-12v"):
        profiles.load_profile("nope")
    with pytest.raises(ProfileError, match="cannot read profile"):
        profiles.load_profile(str(tmp_path / "missing.yml"))
    (tmp_path / "lifepo4-12v").write_text("values:\n  float_voltage: 12\n")  # a plain name is never a file
    assert profiles.load_profile("lifepo4-12v").values["float_voltage"] == 13.5


# the table of the Victron manual (12 V; the 24 V profiles are twice that)
MANUAL = {
    "gel-long-life": (14.1, 13.8),
    "gel-deep-discharge": (14.3, 13.8),
    "victron-default": (14.4, 13.8),
    "agm-spiral-cell": (14.7, 13.8),
    "pzs-tubular-1": (14.9, 13.8),
    "pzs-tubular-2": (15.1, 13.8),
    "pzs-tubular-3": (15.3, 13.8),
    "lifepo4": (14.2, 13.5),
}


def test_the_premade_profiles_match_the_manual_for_12_and_24_volt():
    premade = {p.name: p for p in profiles.list_premade()}
    assert set(premade) == {f"{stem}-{v}v" for stem in MANUAL for v in (12, 24)}
    for stem, (absorb, flt) in MANUAL.items():
        for volts, k in ((12, 1), (24, 2)):
            p = premade[f"{stem}-{volts}v"]
            assert p.values == {"absorption_voltage": round(absorb * k, 2), "float_voltage": round(flt * k, 2)}, p.name
            assert p.description and f"{volts} V" in p.description


def test_diff_lists_differences_in_register_order_and_tolerates_noise():
    p = Profile("p", {"float_voltage": 13.8, "absorption_voltage": 14.5, "battery_max_current": 15.0})
    assert [(c.name, c.old, c.new, c.unit) for c in profiles.diff(p, CURRENT)] == [("float_voltage", 13.5, 13.8, "V")]
    noisy = Profile("p", {"float_voltage": 13.501})
    assert profiles.diff(noisy, CURRENT) == []
    missing = Profile("p", {"float_voltage": 13.5})
    assert [c.old for c in profiles.diff(missing, [])] == [None]
