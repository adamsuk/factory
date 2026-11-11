"""Configuration: built-in defaults < user config file < command-line flags."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import yamlio

# The standard that ships with the extension.
BUILTIN_BASELINE = Path(__file__).resolve().parents[2] / "baseline"

# Items a repo can skip in its .github/factory.yml.
FILE_ITEMS = {
    "pr-template": ".github/pull_request_template.md",
    "issue-template": ".github/ISSUE_TEMPLATE/task.yml",
    "codeowners": ".github/CODEOWNERS",
    "dependabot": ".github/dependabot.yml",
    "codeql": ".github/workflows/codeql.yml",
    "changelog-check": ".github/workflows/changelog.yml",
    "changelog-stub": "CHANGELOG.md",
    "coderabbit": ".coderabbit.yaml",
}
ITEMS = {"settings", "secret-scanning", "vulnerability-alerts", "ruleset", "labels", "files", *FILE_ITEMS}
OVERRIDE_KEYS = {"skip", "approvals", "required_checks", "codeowners", "codeowner_paths", "require_code_owner_review", "agents"}
# Roles the factory agents can take in a repo (see README, "Agents").
AGENT_ROLES = ("intake", "research", "builder", "reviewer")
DEFAULT_AGENT_ROLES = ("builder", "reviewer")
AGENT_KEYS = {"merge", "roles", "issue_label", "review_bot", "test_command"}
DEFAULT_ISSUE_LABEL = "factory"
# Review bots that may review PRs in a repo (`none` means only the factory's PR Reviewer).
REVIEW_BOTS = ("coderabbit", "none")
DEFAULT_REVIEW_BOT = "none"
OVERRIDE_PATH = ".github/factory.yml"
CONFIG_KEYS = {
    "owners",
    "topic",
    "approvals",
    "codeowners",
    "required_checks",
    "sync_branch",
    "marker",
    "workflows_repo",
    "workflows_ref",
    "baseline",
}


class ConfigError(ValueError):
    pass


@dataclass
class FactoryConfig:
    # Empty owners / codeowners mean "the authenticated gh user"; resolve_config fills them in.
    owners: list[str] = field(default_factory=list)
    topic: str = "factory"
    approvals: int = 1
    codeowners: list[str] = field(default_factory=list)
    required_checks: list[str] = field(default_factory=list)
    sync_branch: str = "factory/sync"
    marker: str = "factory-managed"
    # Where delivered files point for reusable workflows. Change it if you fork.
    workflows_repo: str = "adamsuk/gh-factory"
    # Pinned commit of the reusable workflows, with a trailing comment naming what it tracks.
    workflows_ref: str = "283fa179145dab3b40dbb4b2100fcb50910d7eb1"
    workflows_ref_comment: str = "main"
    baseline: Path | None = None
    source: str = "built-in defaults"


@dataclass
class Baseline:
    settings: dict[str, Any]
    security: dict[str, Any]
    ruleset: dict[str, Any]
    labels: list[dict[str, str]]
    dirs: list[Path]  # custom first, then built-in
    actions: dict[str, dict[str, str]] = field(default_factory=dict)

    def pin(self, action: str) -> str:
        """`owner/repo[/path]` -> `owner/repo[/path]@<sha> # <version>`. Raises KeyError if not pinned."""
        repo = "/".join(action.split("/")[:2])
        p = self.actions.get(repo)
        if not p:
            raise KeyError(f"no pin for {repo} in baseline actions.yml")
        return f"{action}@{p['sha']} # {p['version']}"

    @property
    def files_dir(self) -> Path:
        return self.dirs[-1] / "files"

    def template(self, rel: str) -> str:
        for d in self.dirs:
            p = d / "files" / rel
            if p.is_file():
                return p.read_text()
        raise FileNotFoundError(rel)

    def has_template(self, rel: str) -> bool:
        return any((d / "files" / rel).is_file() for d in self.dirs)


@dataclass
class AgentSettings:
    """What the factory agents may do in one repo. Read by the agents, never applied by gh-factory."""

    merge: bool = False  # off unless the repo opts in
    roles: list[str] = field(default_factory=lambda: list(DEFAULT_AGENT_ROLES))
    issue_label: str = DEFAULT_ISSUE_LABEL
    review_bot: str = DEFAULT_REVIEW_BOT  # which review bot also reviews PRs here
    test_command: str | None = None  # what the builder runs before opening a PR; None means read CI

    def as_dict(self) -> dict[str, Any]:
        return {
            "merge": self.merge,
            "roles": list(self.roles),
            "issue_label": self.issue_label,
            "review_bot": self.review_bot,
            "test_command": self.test_command,
        }


@dataclass
class RepoOverrides:
    skip: set[str] = field(default_factory=set)
    approvals: int | None = None
    required_checks: list[str] | None = None
    codeowners: list[str] | None = None
    # Paths owned in CODEOWNERS instead of `*` (`/.github/` is always owned). None keeps `*`.
    codeowner_paths: list[str] | None = None
    # Overrides the baseline ruleset's `require_code_owner_review`. None keeps the baseline value.
    require_code_owner_review: bool | None = None
    agents: AgentSettings = field(default_factory=AgentSettings)

    def skips(self, item: str) -> bool:
        if item in self.skip:
            return True
        return item in FILE_ITEMS and "files" in self.skip


def user_config_path(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    if env.get("GH_FACTORY_CONFIG"):
        return Path(env["GH_FACTORY_CONFIG"]).expanduser()
    base = env.get("XDG_CONFIG_HOME") or str(Path(env.get("HOME", "~")) / ".config")
    return Path(base).expanduser() / "gh-factory" / "config.yml"


def _str_list(raw: dict, key: str, where: str) -> list[str]:
    v = raw.get(key) or []
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
        raise ConfigError(f"{where}: `{key}` must be a list of strings")
    return list(v)


def parse_config(text: str, where: str, base_dir: Path) -> FactoryConfig:
    try:
        raw = yamlio.load(text) or {}
    except yamlio.YAMLError as e:
        raise ConfigError(f"{where} is not valid YAML: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be a mapping")
    unknown = set(raw) - CONFIG_KEYS
    if unknown:
        raise ConfigError(f"{where}: unknown keys {sorted(unknown)}; allowed {sorted(CONFIG_KEYS)}")
    cfg = FactoryConfig(source=where)
    cfg.owners = _str_list(raw, "owners", where)
    cfg.codeowners = _str_list(raw, "codeowners", where)
    cfg.required_checks = _str_list(raw, "required_checks", where)
    if "approvals" in raw:
        a = raw["approvals"]
        if not isinstance(a, int) or isinstance(a, bool) or not 0 <= a <= 10:
            raise ConfigError(f"{where}: `approvals` must be an integer 0-10")
        cfg.approvals = a
    for k in ("topic", "sync_branch", "marker", "workflows_repo", "workflows_ref", "workflows_ref_comment"):
        if k in raw:
            if not isinstance(raw[k], str) or not raw[k].strip():
                raise ConfigError(f"{where}: `{k}` must be a non-empty string")
            setattr(cfg, k, raw[k])
    if raw.get("baseline"):
        p = Path(str(raw["baseline"])).expanduser()
        cfg.baseline = p if p.is_absolute() else (base_dir / p)
    return cfg


def resolve_config(
    config_path: str | None,
    owners: list[str] | None,
    baseline: str | None,
    login: Callable[[], str],
    env: dict[str, str] | None = None,
) -> FactoryConfig:
    """Built-in defaults, then the config file (--config, else the user config if present), then flags.
    `login` returns the authenticated gh user; it is only called when owners or codeowners are unset."""
    path = Path(config_path).expanduser() if config_path else user_config_path(env)
    if path.is_file():
        cfg = parse_config(path.read_text(), str(path), path.parent)
    elif config_path:
        raise ConfigError(f"config file not found: {config_path}")
    else:
        cfg = FactoryConfig()
    if owners:
        cfg.owners = list(owners)
    if baseline:
        cfg.baseline = Path(baseline).expanduser()
    if cfg.baseline is not None and not cfg.baseline.is_dir():
        raise ConfigError(f"baseline directory not found: {cfg.baseline}")
    if not cfg.owners:
        cfg.owners = [login()]
    if not cfg.codeowners:
        try:
            me = login()
        except Exception:
            # Tokens that are not a user (e.g. GITHUB_TOKEN in Actions): fall back to the first owner.
            me = cfg.owners[0]
        cfg.codeowners = [f"@{me}"]
    return cfg


def load_baseline(custom: str | Path | None = None) -> Baseline:
    """Load the standard. A custom directory may hold any subset of the built-in files;
    anything it lacks falls back to the built-in baseline."""
    dirs = [Path(custom)] if custom else []
    dirs.append(BUILTIN_BASELINE)

    def first(name: str) -> Any:
        for d in dirs:
            p = d / name
            if p.is_file():
                return yamlio.load(p.read_text())
        return None

    settings = first("settings.yml") or {}
    return Baseline(
        settings=settings.get("repository") or {},
        security=settings.get("security") or {},
        ruleset=first("ruleset.yml") or {},
        labels=first("labels.yml") or [],
        dirs=dirs,
        actions=first("actions.yml") or {},
    )


def parse_overrides(text: str | None) -> RepoOverrides:
    """Parse and validate a repo's .github/factory.yml. Raises ConfigError."""
    if not text or not text.strip():
        return RepoOverrides()
    try:
        raw = yamlio.load(text)
    except yamlio.YAMLError as e:
        raise ConfigError(f"{OVERRIDE_PATH} is not valid YAML: {e}") from e
    if raw is None:
        return RepoOverrides()
    if not isinstance(raw, dict):
        raise ConfigError(f"{OVERRIDE_PATH} must be a mapping")
    unknown = set(raw) - OVERRIDE_KEYS
    if unknown:
        raise ConfigError(f"{OVERRIDE_PATH}: unknown keys {sorted(unknown)}; allowed {sorted(OVERRIDE_KEYS)}")
    ov = RepoOverrides()
    skip = raw.get("skip") or []
    if not isinstance(skip, list):
        raise ConfigError(f"{OVERRIDE_PATH}: `skip` must be a list")
    bad = set(skip) - ITEMS
    if bad:
        raise ConfigError(f"{OVERRIDE_PATH}: unknown skip items {sorted(bad)}; allowed {sorted(ITEMS)}")
    ov.skip = set(skip)
    if "approvals" in raw:
        a = raw["approvals"]
        if not isinstance(a, int) or isinstance(a, bool) or not 0 <= a <= 10:
            raise ConfigError(f"{OVERRIDE_PATH}: `approvals` must be an integer 0-10")
        ov.approvals = a
    for key in ("required_checks", "codeowners"):
        if key in raw:
            setattr(ov, key, _str_list(raw, key, OVERRIDE_PATH))
    if "codeowner_paths" in raw:
        paths = _str_list(raw, "codeowner_paths", OVERRIDE_PATH)
        if not paths:
            raise ConfigError(f"{OVERRIDE_PATH}: `codeowner_paths` must list at least one path")
        if any(any(c.isspace() for c in p) or p.startswith("#") for p in paths):
            raise ConfigError(f"{OVERRIDE_PATH}: `codeowner_paths` entries must be single CODEOWNERS patterns")
        ov.codeowner_paths = paths
    if "require_code_owner_review" in raw:
        if not isinstance(raw["require_code_owner_review"], bool):
            raise ConfigError(f"{OVERRIDE_PATH}: `require_code_owner_review` must be true or false")
        ov.require_code_owner_review = raw["require_code_owner_review"]
    if "agents" in raw:
        ov.agents = parse_agents(raw["agents"])
    return ov


def parse_agents(raw: Any) -> AgentSettings:
    """Validate the `agents:` section of .github/factory.yml. Every key is optional."""
    where = f"{OVERRIDE_PATH}: `agents`"
    if raw is None:
        return AgentSettings()
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be a mapping")
    unknown = set(raw) - AGENT_KEYS
    if unknown:
        raise ConfigError(f"{where}: unknown keys {sorted(unknown)}; allowed {sorted(AGENT_KEYS)}")
    agents = AgentSettings()
    if "merge" in raw:
        if not isinstance(raw["merge"], bool):
            raise ConfigError(f"{where}.merge must be true or false")
        agents.merge = raw["merge"]
    if "roles" in raw:
        roles = raw["roles"]
        if roles is None:
            roles = []
        if not isinstance(roles, list) or not all(isinstance(r, str) for r in roles):
            raise ConfigError(f"{where}.roles must be a list of role names")
        bad = sorted(set(roles) - set(AGENT_ROLES))
        if bad:
            raise ConfigError(f"{where}.roles: unknown roles {bad}; allowed {list(AGENT_ROLES)}")
        if len(set(roles)) != len(roles):
            raise ConfigError(f"{where}.roles lists a role more than once")
        agents.roles = list(roles)
    if "issue_label" in raw:
        label = raw["issue_label"]
        if not isinstance(label, str) or not label.strip():
            raise ConfigError(f"{where}.issue_label must be a non-empty string")
        agents.issue_label = label
    if "review_bot" in raw:
        bot = raw["review_bot"]
        if bot not in REVIEW_BOTS:
            raise ConfigError(f"{where}.review_bot must be one of {list(REVIEW_BOTS)}")
        agents.review_bot = bot
    if "test_command" in raw:
        cmd = raw["test_command"]
        if cmd is not None:
            if not isinstance(cmd, str) or not cmd.strip():
                raise ConfigError(f"{where}.test_command must be a non-empty string")
            cmd = cmd.strip()
        agents.test_command = cmd
    return agents
