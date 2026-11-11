import pytest

from factory.client import ReadOnlyViolation
from factory.config import FactoryConfig
from factory.plan import desired_ruleset, plan_repo, ruleset_diff
from factory.render import render_codeowners, render_template

from .conftest import FakeClient, repo_routes

FULL = "touchlineHQ/app"


def actions(plan):
    return {(c.item, c.action, c.target) for c in plan.changes}


def test_fresh_repo_plans_everything_and_writes_nothing(cfg, baseline):
    client = FakeClient(repo_routes(vuln=404))
    plan = plan_repo(client, cfg, baseline, FULL)

    assert client.writes == []
    got = actions(plan)
    assert ("settings", "update", "repository settings") in got
    assert ("secret-scanning", "enable", "secret scanning") in got
    assert ("vulnerability-alerts", "enable", "Dependabot vulnerability alerts") in got
    assert ("ruleset", "create", "factory-main") in got
    assert ("labels", "create", "skip-changelog") in got
    assert ("labels", "create", "cutting edge") in got
    for path in (
        ".github/pull_request_template.md",
        ".github/ISSUE_TEMPLATE/task.yml",
        ".github/CODEOWNERS",
        ".github/dependabot.yml",
        ".github/workflows/codeql.yml",
        ".github/workflows/changelog.yml",
        "CHANGELOG.md",
    ):
        assert path in plan.files
    assert ("files", "open-pr", "factory/sync") in got
    assert not plan.errors

    settings = next(c for c in plan.changes if c.item == "settings").payload
    assert settings["allow_merge_commit"] is False and settings["allow_rebase_merge"] is False
    assert settings["delete_branch_on_merge"] is True
    assert "allow_auto_merge" not in settings  # no required checks, and it is already off
    assert "allow_squash_merge" not in settings  # already true


def test_shipped_baseline_has_no_bypass_actor(baseline):
    """Nobody bypasses the shipped ruleset, the owner included.

    The live `factory-main` rulesets carry no bypass actor and the maintainer
    decided they are right (adamsuk/software-factory#323). An owner bypass in
    baseline/ruleset.yml would have `gh factory apply` weaken every one of them.

    The baseline fixture loads the shipped baseline/ruleset.yml, so this pins
    what is really applied.
    """
    actors = desired_ruleset(baseline, 0, [])["bypass_actors"]
    assert actors == []
    # Never `always`: direct pushes to the default branch stay blocked.
    assert all(a["bypass_mode"] != "always" for a in actors)


def test_shipped_baseline_matches_the_live_pull_request_and_check_rules(baseline):
    """The shipped baseline encodes what is live on the enrolled repos (adamsuk/software-factory#323)."""
    rules = {r["type"]: r for r in desired_ruleset(baseline, FactoryConfig().approvals, ["test"])["rules"]}
    assert rules["pull_request"]["parameters"] == {
        "required_approving_review_count": 1,
        "dismiss_stale_reviews_on_push": False,
        "require_code_owner_review": True,
        "require_last_push_approval": True,
        "required_review_thread_resolution": False,
        "allowed_merge_methods": ["squash"],
    }
    assert rules["required_status_checks"]["parameters"]["strict_required_status_checks_policy"] is True
    assert set(rules) == {"deletion", "non_fast_forward", "pull_request", "required_status_checks"}


def test_live_ruleset_shape_is_not_drift(cfg, baseline):
    """A ruleset as GitHub returns it for the live repos plans no ruleset change.

    GitHub also returns `require_extra_approval_for_unattributed_changes`, which the
    factory does not model; an extra key on the live side is ignored.
    """
    live = {
        "id": 7,
        "name": "factory-main",
        "target": "branch",
        "source_type": "Repository",
        "enforcement": "active",
        "bypass_actors": [],
        "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
        "rules": [
            {"type": "deletion"},
            {"type": "non_fast_forward"},
            {
                "type": "pull_request",
                "parameters": {
                    "allowed_merge_methods": ["squash"],
                    "dismiss_stale_reviews_on_push": False,
                    "require_code_owner_review": True,
                    "require_extra_approval_for_unattributed_changes": True,
                    "require_last_push_approval": True,
                    "required_approving_review_count": 1,
                    "required_review_thread_resolution": False,
                },
            },
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": True,
                    "required_status_checks": [{"context": "changelog"}, {"context": "test"}],
                },
            },
        ],
    }
    files = {".github/factory.yml": "required_checks: [test, changelog]\n"}
    plan = plan_repo(FakeClient(repo_routes(rulesets=[live], files=files)), cfg, baseline, FULL)
    assert [c for c in plan.changes if c.item == "ruleset"] == []


def test_repo_overrides_still_relax_the_stricter_defaults(cfg, baseline):
    override = "approvals: 0\nrequire_code_owner_review: false\n"
    plan = plan_repo(FakeClient(repo_routes(files={".github/factory.yml": override})), cfg, baseline, FULL)
    change = next(c for c in plan.changes if c.item == "ruleset")
    pr = {r["type"]: r for r in change.payload["body"]["rules"]}["pull_request"]["parameters"]
    assert pr["required_approving_review_count"] == 0
    assert pr["require_code_owner_review"] is False


def test_malformed_bypass_actors_are_dropped(baseline):
    """A partial entry means nothing to the API, so it is not sent."""
    rs = dict(baseline.ruleset)
    rs["bypass_actors"] = [
        {"actor_type": "User", "actor_id": 1, "bypass_mode": "pull_request"},
        {"actor_type": "User", "actor_id": 2},  # no bypass_mode
        {"actor_id": 3, "bypass_mode": "always"},  # no actor_type
        "nonsense",
    ]
    desired = desired_ruleset(baseline.__class__(**{**baseline.__dict__, "ruleset": rs}), 0, [])
    assert desired["bypass_actors"] == [{"actor_type": "User", "actor_id": 1, "bypass_mode": "pull_request"}]


def test_bypass_actor_drift_is_reported_both_ways(baseline):
    """Drift is a difference from the baseline, not merely "any actor present".

    The old rule reported any bypass actor as drift, which is what made a
    bypass impossible to express. One added by hand must still be corrected.

    The shipped baseline has no bypass actor, so this one is added to the
    fixture to keep both directions covered.
    """
    rs = {**baseline.ruleset, "bypass_actors": [{"actor_type": "User", "actor_id": 25231418, "bypass_mode": "pull_request"}]}
    desired = desired_ruleset(baseline.__class__(**{**baseline.__dict__, "ruleset": rs}), 0, [])

    matching = {"id": 7, **desired, "source_type": "Repository"}
    assert "bypass_actors" not in ruleset_diff(matching, desired)

    stripped = {**matching, "bypass_actors": []}
    assert "bypass_actors" in ruleset_diff(stripped, desired)

    extra = {
        **matching,
        "bypass_actors": [
            *desired["bypass_actors"],
            {"actor_type": "User", "actor_id": 999, "bypass_mode": "always"},
        ],
    }
    assert "bypass_actors" in ruleset_diff(extra, desired)


def test_bypass_actor_order_does_not_count_as_drift(baseline):
    desired = desired_ruleset(baseline, 0, [])
    desired["bypass_actors"] = [
        {"actor_type": "User", "actor_id": 1, "bypass_mode": "pull_request"},
        {"actor_type": "User", "actor_id": 2, "bypass_mode": "pull_request"},
    ]
    reversed_order = {"id": 7, **desired, "bypass_actors": list(reversed(desired["bypass_actors"]))}
    assert "bypass_actors" not in ruleset_diff(reversed_order, desired)


def test_dry_run_client_refuses_writes():
    client = FakeClient({})
    with pytest.raises(ReadOnlyViolation):
        client.post("/repos/x/y/labels", {})


def test_compliant_repo_is_a_no_op(cfg, baseline):
    desired = desired_ruleset(baseline, cfg.approvals, [])
    ruleset = {"id": 7, **desired, "source_type": "Repository"}
    labels = [{"name": lbl["name"], "color": lbl["color"], "description": lbl["description"]} for lbl in baseline.labels]
    files = {
        ".github/pull_request_template.md": render_template(baseline, cfg, ".github/pull_request_template.md", "app"),
        ".github/ISSUE_TEMPLATE/task.yml": render_template(baseline, cfg, ".github/ISSUE_TEMPLATE/task.yml", "app"),
        ".github/CODEOWNERS": render_codeowners(cfg, ["@adamsuk"]),
        ".github/workflows/changelog.yml": render_template(baseline, cfg, ".github/workflows/changelog.yml", "app"),
        "CHANGELOG.md": "# Changelog\n\nOur own history, never overwritten.\n",
        ".coderabbit.yaml": render_template(baseline, cfg, ".coderabbit.yaml", "app"),
    }
    settings = {
        **baseline.settings,
        "allow_auto_merge": False,  # no required checks configured
        "security_and_analysis": {"secret_scanning": {"status": "enabled"}, "secret_scanning_push_protection": {"status": "enabled"}},
    }
    routes = repo_routes(settings=settings, labels=labels, rulesets=[ruleset], files=files, paths=("README.md",))
    plan = plan_repo(FakeClient(routes), cfg, baseline, FULL)
    # README only: no CodeQL languages besides Actions workflows -> actions; dependabot is new.
    writes = {c.target for c in plan.changes if c.writes}
    assert writes == {".github/dependabot.yml", ".github/workflows/codeql.yml", "factory/sync"}


def test_unmarked_file_is_drift_and_marked_file_is_updated(cfg, baseline):
    files = {
        ".github/workflows/changelog.yml": "name: Changelog\n# hand made\n",
        ".github/pull_request_template.md": "<!-- factory-managed: old -->\nold\n",
    }
    plan = plan_repo(FakeClient(repo_routes(files=files)), cfg, baseline, FULL)
    got = actions(plan)
    assert ("changelog-check", "drift", ".github/workflows/changelog.yml") in got
    assert ".github/workflows/changelog.yml" not in plan.files
    assert ("pr-template", "update", ".github/pull_request_template.md") in got


def test_existing_changelog_is_never_touched(cfg, baseline):
    plan = plan_repo(FakeClient(repo_routes(files={"CHANGELOG.md": "# mine\n"})), cfg, baseline, FULL)
    assert "CHANGELOG.md" not in plan.files
    assert not [c for c in plan.changes if c.target == "CHANGELOG.md"]


def test_repo_overrides_skip_and_checks(cfg, baseline):
    override = "skip: [labels, codeql, settings]\napprovals: 1\nrequired_checks: [test, lint]\ncodeowners: ['@touchlineHQ/devs']\n"
    plan = plan_repo(FakeClient(repo_routes(files={".github/factory.yml": override})), cfg, baseline, FULL)
    got = actions(plan)
    assert ("labels", "skipped", "labels") in got
    assert ("settings", "skipped", "settings") in got
    assert ".github/workflows/codeql.yml" not in plan.files
    assert "@touchlineHQ/devs" in plan.files[".github/CODEOWNERS"]
    body = next(c for c in plan.changes if c.item == "ruleset").payload["body"]
    rules = {r["type"]: r for r in body["rules"]}
    assert rules["pull_request"]["parameters"]["required_approving_review_count"] == 1
    contexts = [c["context"] for c in rules["required_status_checks"]["parameters"]["required_status_checks"]]
    assert contexts == ["test", "lint"]


def test_invalid_override_stops_that_repo(cfg, baseline):
    plan = plan_repo(FakeClient(repo_routes(files={".github/factory.yml": "skp: [labels]\n"})), cfg, baseline, FULL)
    assert len(plan.errors) == 1 and not plan.has_writes


def test_no_required_checks_by_default(cfg, baseline):
    body = desired_ruleset(baseline, 0, [])
    types = [r["type"] for r in body["rules"]]
    assert types == ["deletion", "non_fast_forward", "pull_request"]
    assert body["conditions"]["ref_name"]["include"] == ["~DEFAULT_BRANCH"]


def test_ruleset_update_in_place_ignores_extra_api_fields(cfg, baseline):
    desired = desired_ruleset(baseline, 0, [])
    existing = {"id": 3, **desired}
    existing["rules"] = [dict(r) for r in desired["rules"]]
    existing["rules"][2] = {
        "type": "pull_request",
        "parameters": {**desired["rules"][2]["parameters"], "automatic_copilot_code_review_enabled": False},
    }
    assert ruleset_diff(existing, desired) == []
    existing["rules"] = existing["rules"][1:]  # deletion rule removed by hand
    assert ruleset_diff(existing, desired) == ["add rule deletion"]

    routes = repo_routes(rulesets=[{**existing, "name": "factory-main"}, {"id": 9, "name": "someone-elses", "rules": []}])
    plan = plan_repo(FakeClient(routes), cfg, baseline, FULL)
    change = next(c for c in plan.changes if c.item == "ruleset")
    assert change.action == "update" and change.payload["id"] == 3


def test_private_repo_features_reported_unavailable(cfg, baseline):
    routes = repo_routes(visibility="private", settings={"security_and_analysis": {}})
    routes[("GET", f"/repos/{FULL}/rulesets")] = (403, {"message": "Upgrade to GitHub Pro"})
    plan = plan_repo(FakeClient(routes), cfg, baseline, FULL)
    got = {(c.item, c.action) for c in plan.changes}
    assert ("secret-scanning", "unavailable") in got
    assert ("ruleset", "unavailable") in got
    assert ("codeql", "unavailable") in got
    assert not plan.errors


def test_label_update_when_colour_differs(cfg, baseline):
    labels = [{"name": "Skip-Changelog", "color": "000000", "description": "x"}]
    plan = plan_repo(FakeClient(repo_routes(labels=labels)), cfg, baseline, FULL)
    change = next(c for c in plan.changes if c.target == "skip-changelog")
    assert change.action == "update"
    assert change.payload["current"] == "Skip-Changelog"


def test_open_pr_is_refreshed_not_duplicated(cfg, baseline):
    plan = plan_repo(FakeClient(repo_routes(prs=[{"number": 12}])), cfg, baseline, FULL)
    assert ("files", "update-pr", "PR #12") in actions(plan)


def test_empty_repo(cfg, baseline):
    routes = repo_routes()
    routes[("GET", f"/repos/{FULL}/git/trees/main")] = (409, {"message": "Git Repository is empty."})
    plan = plan_repo(FakeClient(routes), cfg, baseline, FULL)
    assert ("files", "unavailable", "files") in actions(plan)
    assert not plan.errors


def test_without_admin_rights_admin_only_items_are_unknown(cfg, baseline):
    routes = repo_routes(settings={"permissions": {"admin": False, "pull": True}})
    del routes[("GET", f"/repos/{FULL}/vulnerability-alerts")]
    client = FakeClient(routes)
    plan = plan_repo(client, cfg, baseline, FULL)
    got = {(c.item, c.action) for c in plan.changes}
    assert {("settings", "unknown"), ("secret-scanning", "unknown"), ("vulnerability-alerts", "unknown")} <= got
    assert not any(p.endswith("vulnerability-alerts") for _, p, _ in client.calls)
    assert ("ruleset", "create") in got and not plan.errors


def _settings_change(plan):
    return next((c for c in plan.changes if c.item == "settings"), None)


def test_auto_merge_stays_off_without_required_checks(cfg, baseline):
    plan = plan_repo(FakeClient(repo_routes()), cfg, baseline, FULL)
    change = _settings_change(plan)
    assert "allow_auto_merge" not in change.payload  # fixture has it off already


def test_auto_merge_is_turned_off_without_required_checks(cfg, baseline):
    plan = plan_repo(FakeClient(repo_routes(settings={"allow_auto_merge": True})), cfg, baseline, FULL)
    change = _settings_change(plan)
    assert change.payload["allow_auto_merge"] is False
    assert "no required checks" in change.detail


def test_auto_merge_is_enabled_with_a_required_check(cfg, baseline):
    files = {".github/factory.yml": "required_checks: [test]\n"}
    plan = plan_repo(FakeClient(repo_routes(files=files)), cfg, baseline, FULL)
    change = _settings_change(plan)
    assert change.payload["allow_auto_merge"] is True
    assert "no required checks" not in change.detail


def test_codeowner_paths_and_required_code_owner_review(cfg, baseline):
    override = "codeowner_paths: ['/AGENTS.md', '/CLAUDE.md', '/.claude/']\nrequire_code_owner_review: true\n"
    plan = plan_repo(FakeClient(repo_routes(files={".github/factory.yml": override})), cfg, baseline, FULL)
    owners = plan.files[".github/CODEOWNERS"].splitlines()
    assert owners[1:] == ["/AGENTS.md @adamsuk", "/CLAUDE.md @adamsuk", "/.claude/ @adamsuk", "/.github/ @adamsuk"]
    assert not any(line.startswith("* ") for line in owners)
    change = next(c for c in plan.changes if c.item == "ruleset")
    rules = {r["type"]: r for r in change.payload["body"]["rules"]}
    assert rules["pull_request"]["parameters"]["require_code_owner_review"] is True
    assert "code-owner review" in change.detail


def test_code_owner_review_is_on_by_default(cfg, baseline):
    plan = plan_repo(FakeClient(repo_routes()), cfg, baseline, FULL)
    assert plan.files[".github/CODEOWNERS"].splitlines()[1:] == ["* @adamsuk", "/.github/ @adamsuk"]
    rules = {r["type"]: r for r in desired_ruleset(baseline, 0, [])["rules"]}
    assert rules["pull_request"]["parameters"]["require_code_owner_review"] is True


def test_existing_ruleset_without_code_owner_review_is_updated(cfg, baseline):
    existing = {"id": 3, **desired_ruleset(baseline, 0, [], False)}
    assert ruleset_diff(existing, desired_ruleset(baseline, 0, [], True)) == ["rule pull_request"]


def test_code_owner_review_without_paths_warns(cfg, baseline):
    plan = plan_repo(FakeClient(repo_routes(files={".github/factory.yml": "require_code_owner_review: true\n"})), cfg, baseline, FULL)
    warnings = [c for c in plan.changes if c.action == "warning"]
    assert [(c.item, c.target) for c in warnings] == [("ruleset", "require_code_owner_review")]
    assert "codeowner_paths" in warnings[0].detail and not warnings[0].writes


def test_code_owner_review_with_paths_does_not_warn(cfg, baseline):
    override = "codeowner_paths: ['/AGENTS.md']\nrequire_code_owner_review: true\n"
    plan = plan_repo(FakeClient(repo_routes(files={".github/factory.yml": override})), cfg, baseline, FULL)
    assert not [c for c in plan.changes if c.action == "warning"]


def test_coderabbit_config_is_created_and_can_be_skipped(cfg, baseline):
    plan = plan_repo(FakeClient(repo_routes()), cfg, baseline, FULL)
    assert ("coderabbit", "create", ".coderabbit.yaml") in actions(plan)
    assert plan.files[".coderabbit.yaml"].startswith(f"# {cfg.marker}")

    plan = plan_repo(FakeClient(repo_routes(files={".github/factory.yml": "skip: [coderabbit]\n"})), cfg, baseline, FULL)
    assert ("coderabbit", "skipped", ".coderabbit.yaml") in actions(plan)
    assert ".coderabbit.yaml" not in plan.files


def test_hand_made_coderabbit_config_is_left_alone(cfg, baseline):
    plan = plan_repo(FakeClient(repo_routes(files={".coderabbit.yaml": "reviews:\n  profile: assertive\n"})), cfg, baseline, FULL)
    assert ("coderabbit", "drift", ".coderabbit.yaml") in actions(plan)
    assert ".coderabbit.yaml" not in plan.files
