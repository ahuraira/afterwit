"""config.save(): edit scalars in place without touching anything else."""

import pytest

from afterwit import config
from tests.conftest import toml_config


@pytest.fixture
def cfg_file(tmp_path, monkeypatch):
    p = tmp_path / "config.toml"
    p.write_text(
        "# my notes, keep them\n"
        + toml_config(wiki_root=tmp_path / "wiki")
        + "floor = 0.35\n"
        "\n"
        "[[databases]]\n"
        'url = "postgres://localhost/app"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("AFTERWIT_CONFIG", str(p))
    (tmp_path / "wiki").mkdir()
    return p


def test_edits_in_place_and_keeps_comments_and_tables(cfg_file):
    config.save({"floor": 0.5, "distill_model": "gpt-5.6-sol"})
    text = cfg_file.read_text(encoding="utf-8")
    assert "# my notes, keep them" in text
    assert "floor = 0.5" in text and "floor = 0.35" not in text
    loaded = config.load(cfg_file)
    assert loaded.floor == 0.5
    assert loaded.distill_model == "gpt-5.6-sol"
    # the new key must land ABOVE the table — appended at EOF it would have
    # become databases.distill_model and silently stopped being read
    assert text.index("distill_model") < text.index("[[databases]]")
    assert loaded.databases == [{"url": "postgres://localhost/app"}]


def test_none_deletes_the_key_so_the_default_returns(cfg_file):
    config.save({"floor": 0.9})
    assert config.load(cfg_file).floor == 0.9
    config.save({"floor": None})
    assert "floor" not in cfg_file.read_text(encoding="utf-8")
    assert config.load(cfg_file).floor == 0.35


def test_backs_up_before_writing(cfg_file):
    config.save({"floor": 0.4})
    backups = list(cfg_file.parent.glob("config.toml.afterwit-bak-*"))
    assert len(backups) == 1
    assert "floor = 0.35" in backups[0].read_text(encoding="utf-8")


def test_unknown_key_is_refused(cfg_file):
    with pytest.raises(config.ConfigError):
        config.save({"rm_rf": "/"})
    assert "rm_rf" not in cfg_file.read_text(encoding="utf-8")


def test_windows_paths_survive_the_round_trip(tmp_path, monkeypatch):
    p = tmp_path / "config.toml"
    monkeypatch.setenv("AFTERWIT_CONFIG", str(p))
    config.save({"distill_model": r"C:\Users\me\bin\model"})  # backslashes must escape
    assert config.load(p).distill_model == r"C:\Users\me\bin\model"


def test_coerce_rejects_out_of_range_and_bad_drivers():
    assert config.coerce("inject_max_cards", "2") == 2
    with pytest.raises(config.ConfigError):
        config.coerce("inject_max_cards", "9")      # Manifesto P3 hard cap
    with pytest.raises(config.ConfigError):
        config.coerce("distill_driver", "gpt-9")
    with pytest.raises(config.ConfigError):
        config.coerce("wiki_root", "relative/path")
    with pytest.raises(config.ConfigError):
        config.coerce("distill_driver", "")         # required
    assert config.coerce("distill_model", "  ") is None
    assert config.coerce("auto_review", "true") is True


# ------------------------------------------------------- project aliases (ADR-039)

def test_a_project_slug_can_differ_from_its_folder_name(tmp_path):
    """Without this a slug IS a folder name — `project_from_cwd` returns
    `rel.parts[0]` verbatim — so a project cannot be renamed without moving its
    directory. Both directions matter: queries need folder -> slug, and anything
    touching the working tree (git anchoring, drift resolution) needs slug -> folder.
    """
    from afterwit.config import project_dir_name, project_from_cwd
    root = tmp_path / "Projects"
    (root / "harness_helper").mkdir(parents=True)
    (root / "acme_flow").mkdir()
    aliases = {"harness_helper": "afterwit"}

    assert project_from_cwd(root / "harness_helper", root, aliases) == "afterwit"
    assert project_from_cwd(root / "harness_helper" / "src" / "deep", root, aliases) == "afterwit"
    assert project_dir_name("afterwit", aliases) == "harness_helper"

    # an unaliased project is untouched in both directions
    assert project_from_cwd(root / "acme_flow", root, aliases) == "acme_flow"
    assert project_dir_name("acme_flow", aliases) == "acme_flow"
    # and no alias map at all keeps the old behaviour exactly
    assert project_from_cwd(root / "harness_helper", root) == "harness_helper"
    assert project_dir_name("afterwit") == "afterwit"
    # outside projects_root is still global, alias map or not
    assert project_from_cwd(tmp_path / "elsewhere", root, aliases) == "global"


def test_git_anchoring_follows_the_folder_not_the_slug(tmp_path):
    """`gitmeta.anchor` is the ONE place a card is stamped with its commit. It
    resolves `projects_root / <slug>`, so an aliased project would anchor against
    a directory that does not exist and every card would silently lose its commit
    — which is what makes drift detection work at all."""
    import subprocess

    from afterwit import gitmeta
    root = tmp_path / "Projects"
    repo = root / "harness_helper"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "init"], check=True)

    sha_unaliased, _ = gitmeta.anchor(root, "afterwit")
    assert sha_unaliased is None, "control: without the alias there is no such folder"
    sha, _ = gitmeta.anchor(root, "afterwit", aliases={"harness_helper": "afterwit"})
    assert sha and len(sha) >= 7


def test_config_round_trips_project_aliases(tmp_path, monkeypatch):
    from afterwit import config
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text('wiki_root = "/w"\n\n[project_aliases]\nharness_helper = "afterwit"\n')
    monkeypatch.setenv("AFTERWIT_CONFIG", str(cfg_file))
    assert config.load().project_aliases == {"harness_helper": "afterwit"}
    # absent table is an empty map, never None — callers index it directly
    cfg_file.write_text('wiki_root = "/w"\n')
    assert config.load().project_aliases == {}


def test_saving_settings_from_the_ui_does_not_drop_the_alias_table(tmp_path, monkeypatch):
    """`save()` rewrites config.toml surgically. If it dropped an unknown table the
    way it rejects an unknown key, one click in the Settings tab would silently
    un-rename the project — and nothing would report it, because the failure looks
    exactly like a project with no history."""
    from afterwit import config
    p = tmp_path / "config.toml"
    p.write_text('floor = 0.35\nprojects_root = "/p"\n\n# keep me\n'
                 '[project_aliases]\nharness_helper = "afterwit"\n')
    monkeypatch.setenv("AFTERWIT_CONFIG", str(p))
    config.save({"floor": 0.5}, path=p)
    assert config.load().project_aliases == {"harness_helper": "afterwit"}
    assert config.load().floor == 0.5
    assert "# keep me" in p.read_text()


def test_coerce_run_time_validates_24h_clock():
    assert config.coerce("run_time", "04:05") == "04:05"
    assert config.coerce("run_time", " 23:59 ") == "23:59"
    import pytest as _pytest
    for bad in ("24:00", "2:30", "02:60", "noon"):
        with _pytest.raises(config.ConfigError):
            config.coerce("run_time", bad)
    with _pytest.raises(config.ConfigError):
        config.coerce("run_time", "")  # required — empty may not unset it
