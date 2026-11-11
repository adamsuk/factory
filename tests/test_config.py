from pathlib import Path

import pytest

from factory.config import BUILTIN_BASELINE, ConfigError, load_baseline, resolve_config, user_config_path

ROOT = Path(__file__).resolve().parent.parent


def never():
    raise AssertionError("login should not be needed")


def env_for(tmp_path):
    return {"HOME": str(tmp_path), "XDG_CONFIG_HOME": str(tmp_path / "xdg")}


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_defaults_use_the_authenticated_user(tmp_path):
    cfg = resolve_config(None, None, None, lambda: "octocat", env_for(tmp_path))
    assert cfg.owners == ["octocat"]
    assert cfg.codeowners == ["@octocat"]
    assert cfg.topic == "factory" and cfg.approvals == 1 and cfg.required_checks == []
    assert cfg.baseline is None and cfg.source == "built-in defaults"


def test_user_config_is_read_from_xdg(tmp_path):
    env = env_for(tmp_path)
    write(user_config_path(env), "owners: [touchlineHQ, adamsuk]\ncodeowners: ['@adamsuk']\ntopic: golden\n")
    cfg = resolve_config(None, None, None, never, env)
    assert cfg.owners == ["touchlineHQ", "adamsuk"] and cfg.topic == "golden"


def test_env_override_and_flags_win(tmp_path):
    p = write(tmp_path / "custom.yml", "owners: [a]\ncodeowners: ['@a']\n")
    env = {**env_for(tmp_path), "GH_FACTORY_CONFIG": str(p)}
    cfg = resolve_config(None, ["b", "c"], None, never, env)
    assert cfg.owners == ["b", "c"] and cfg.codeowners == ["@a"]


def test_explicit_config_replaces_user_config(tmp_path):
    env = env_for(tmp_path)
    write(user_config_path(env), "owners: [ignored]\n")
    p = write(tmp_path / "team.yml", "owners: [team]\n")
    cfg = resolve_config(str(p), None, None, lambda: "me", env)
    assert cfg.owners == ["team"] and cfg.codeowners == ["@me"]


def test_bad_configs(tmp_path):
    env = env_for(tmp_path)
    with pytest.raises(ConfigError, match="not found"):
        resolve_config(str(tmp_path / "missing.yml"), None, None, never, env)
    p = write(tmp_path / "bad.yml", "ownrs: [x]\n")
    with pytest.raises(ConfigError, match="unknown keys"):
        resolve_config(str(p), None, None, never, env)
    p = write(tmp_path / "bad2.yml", "approvals: lots\n")
    with pytest.raises(ConfigError, match="approvals"):
        resolve_config(str(p), None, None, never, env)
    with pytest.raises(ConfigError, match="baseline directory"):
        resolve_config(None, ["x"], str(tmp_path / "nope"), lambda: "me", env)


def test_relative_baseline_is_relative_to_the_config_file(tmp_path):
    (tmp_path / "conf" / "std").mkdir(parents=True)
    p = write(tmp_path / "conf" / "config.yml", "owners: [x]\ncodeowners: ['@x']\nbaseline: std\n")
    cfg = resolve_config(str(p), None, None, never, env_for(tmp_path))
    assert cfg.baseline == tmp_path / "conf" / "std"


def test_custom_baseline_falls_back_to_builtin(tmp_path):
    write(tmp_path / "labels.yml", "- name: only-mine\n  color: '000000'\n  description: mine\n")
    write(tmp_path / "files" / ".github" / "pull_request_template.md", "<!-- factory-managed -->\nours\n")
    b = load_baseline(tmp_path)
    assert [lbl["name"] for lbl in b.labels] == ["only-mine"]
    assert b.ruleset["name"] == "factory-main"  # built-in
    assert b.template(".github/pull_request_template.md").endswith("ours\n")
    assert "Acceptance criteria" in b.template(".github/ISSUE_TEMPLATE/task.yml")  # built-in


def test_example_config_is_valid(tmp_path):
    cfg = resolve_config(str(ROOT / "examples" / "config.yml"), None, None, never, env_for(tmp_path))
    assert cfg.owners == ["touchlineHQ", "adamsuk"]


def test_builtin_baseline_ships_in_repo():
    assert (BUILTIN_BASELINE / "settings.yml").is_file()


def test_codeowners_fall_back_to_first_owner_without_a_user_token(tmp_path):
    def no_user():
        raise RuntimeError("GITHUB_TOKEN cannot read /user")

    cfg = resolve_config(None, ["touchlineHQ", "adamsuk"], None, no_user, env_for(tmp_path))
    assert cfg.codeowners == ["@touchlineHQ"]
    with pytest.raises(RuntimeError):
        resolve_config(None, None, None, no_user, env_for(tmp_path))


@pytest.mark.parametrize(
    "text, match",
    [
        ("codeowner_paths: []\n", "at least one path"),
        ("codeowner_paths: ['/a b']\n", "single CODEOWNERS patterns"),
        ("codeowner_paths: [3]\n", "list of strings"),
        ("require_code_owner_review: 'yes'\n", "true or false"),
    ],
)
def test_codeowner_override_validation(text, match):
    from factory.config import parse_overrides

    with pytest.raises(ConfigError, match=match):
        parse_overrides(text)


def test_codeowner_overrides_default_to_unset():
    from factory.config import parse_overrides

    ov = parse_overrides("codeowner_paths: ['/AGENTS.md']\nrequire_code_owner_review: false\n")
    assert ov.codeowner_paths == ["/AGENTS.md"] and ov.require_code_owner_review is False
    empty = parse_overrides("skip: []\n")
    assert empty.codeowner_paths is None and empty.require_code_owner_review is None
