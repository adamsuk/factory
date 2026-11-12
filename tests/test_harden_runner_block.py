"""gh-factory's own workflows block runner egress, except where no audit log exists yet.

Every `step-security/harden-runner` step in `.github/workflows/*.yml` (the copies under
`baseline/` are other repos' and are not read here) must run with `egress-policy: block` and an
`allowed-endpoints` list taken from that job's audit-mode log. A workflow with no log of its
real path may stay on `audit` only if it is named in `AUDIT_UNTIL_LOGGED` below (empty now), so
the exemption cannot grow without this file changing.
"""

import re

import pytest

from factory._vendor import yaml

from .conftest import ROOT

WORKFLOWS = ROOT / ".github" / "workflows"

# Still `audit`: no audit-mode log of the job's real path exists yet (adamsuk/software-factory#305).
# Empty since release.yml and mutation.yml were logged (adamsuk/software-factory#321).
AUDIT_UNTIL_LOGGED: set[str] = set()
# `block` with nothing allowed: the audit-mode log shows the job makes no outbound call.
NO_EGRESS = {"pr-title.yml"}
# Calls release.yml and has no step of its own (adamsuk/software-factory#273).
NO_STEP = {"auto-release.yml"}

ENDPOINT = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?:443$")


def _steps():
    found = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        wf = yaml.safe_load(path.read_text())
        for job_id, job in (wf.get("jobs") or {}).items():
            for step in job.get("steps") or []:
                if str(step.get("uses", "")).startswith("step-security/harden-runner@"):
                    found.append((path.name, job_id, step.get("with") or {}))
    return found


STEPS = _steps()
IDS = [f"{name}:{job}" for name, job, _ in STEPS]


def _endpoints(inputs):
    return str(inputs.get("allowed-endpoints") or "").split()


def test_every_workflow_is_accounted_for():
    with_step = {name for name, _, _ in STEPS}
    every = {p.name for p in WORKFLOWS.glob("*.yml")}
    assert every - with_step == NO_STEP
    assert AUDIT_UNTIL_LOGGED | NO_EGRESS <= with_step


@pytest.mark.parametrize(("name", "job", "inputs"), STEPS, ids=IDS)
def test_step_blocks_egress_unless_it_has_no_audit_log(name, job, inputs):
    if name in AUDIT_UNTIL_LOGGED:
        assert inputs.get("egress-policy") == "audit", f"{name}:{job} has no audit log to build a block list from"
        assert "allowed-endpoints" not in inputs
        return
    assert inputs.get("egress-policy") == "block", f"{name}:{job} must block egress"
    assert "allowed-endpoints" in inputs, f"{name}:{job} must list its allowed endpoints in the workflow file"


@pytest.mark.parametrize(("name", "job", "inputs"), STEPS, ids=IDS)
def test_allowed_endpoints_are_plain_hosts_on_443(name, job, inputs):
    endpoints = _endpoints(inputs)
    if name in AUDIT_UNTIL_LOGGED | NO_EGRESS:
        assert endpoints == []
        return
    assert endpoints, f"{name}:{job} blocks egress but allows no host"
    assert endpoints == sorted(set(endpoints)), f"{name}:{job} endpoints must be sorted and unique"
    for endpoint in endpoints:
        assert ENDPOINT.match(endpoint), f"{name}:{job} has a wildcard, port or malformed endpoint: {endpoint}"
