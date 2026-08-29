import os

import pytest

from scripts import download_modis_lst as downloader
from scripts.validate_heat_local import check_gate


def test_water_gap_requires_data_unavailable_not_retired_era5():
    verdict, detail = check_gate(
        "Data unavailable (water pixel)",
        {"score": None, "label": "Data unavailable", "source": None},
    )
    assert verdict == "PASS"
    assert "data-unavailable:Y" in detail

    verdict, _ = check_gate(
        "Data unavailable (water pixel)",
        {"score": 55, "label": "Moderate Heat", "source": "era5"},
    )
    assert verdict == "FAIL"


def test_hot_end_gate_accepts_extreme_label_when_only_score_is_pinned():
    verdict, _ = check_gate(
        "score<=40",
        {"score": 18, "label": "Extreme Heat", "source": "modis"},
    )
    assert verdict == "PASS"


def test_refresh_has_no_unfingerprinted_skip_existing_mode():
    with pytest.raises(SystemExit):
        downloader.build_parser().parse_args(["--skip-existing"])


def test_day_and_night_tile_identity_must_both_be_complete():
    downloader.validate_tile_sets(
        {"h29v12", "h30v12"},
        {"h29v12", "h30v12"},
        {"h29v12", "h30v12"},
    )
    with pytest.raises(RuntimeError, match="active generation was not changed"):
        downloader.validate_tile_sets(
            {"h29v12", "h30v12"},
            {"h29v12", "h30v12"},
            {"h29v12"},
        )


def test_verified_generation_switches_one_pointer_atomically(monkeypatch,
                                                             tmp_path):
    releases = tmp_path / "releases"
    releases.mkdir()
    old_release = releases / "old"
    old_release.mkdir()
    (old_release / "marker").write_text("old", encoding="utf-8")
    active = tmp_path / "current"
    active.symlink_to(os.path.relpath(old_release, tmp_path))

    stage = releases / ".staging-new"
    stage.mkdir()
    for name in (
        downloader.DAY_VRT_NAME,
        downloader.NIGHT_VRT_NAME,
        downloader.METADATA_NAME,
    ):
        (stage / name).write_text("new", encoding="utf-8")

    monkeypatch.setattr(downloader, "RELEASES_DIR", str(releases))
    monkeypatch.setattr(downloader, "ACTIVE_LINK", str(active))

    final = downloader.publish_release(str(stage), "new")

    assert final == str(releases / "new")
    assert active.is_symlink()
    assert active.resolve() == releases / "new"
    assert (old_release / "marker").read_text(encoding="utf-8") == "old"
