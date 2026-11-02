"""Security assertions for the non-publishing and privileged workflow boundary."""

import re
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/distribution.yml"


def workflow():
    return yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)


def test_distribution_defaults_to_manual_nonpublishing_validation():
    config = workflow()
    assert config["on"]["workflow_dispatch"]["inputs"]["dry_run"]["default"] == "true"
    assert config["on"]["workflow_dispatch"]["inputs"]["channel"]["default"] == "nightly"
    assert config["on"]["workflow_run"]["workflows"] == ["deterministic-ci"]
    assert config["on"]["workflow_run"]["types"] == ["completed"]
    publisher = config["jobs"]["publish"]
    assert "needs.prepare.outputs.dry_run == 'false'" in publisher["if"]
    assert "refs/heads/main" in publisher["if"]
    assert "release-{0}" in publisher["environment"]["name"]
    assert config["jobs"]["build"]["permissions"] == {"contents": "read"}
    assert publisher["permissions"] == {
        "contents": "write",
        "attestations": "write",
        "id-token": "write",
        "actions": "read",
    }
    for job in config["jobs"].values():
        for step in job["steps"]:
            if "uses" in step:
                assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", step["uses"])


def test_draft_is_checked_before_publication_and_final_target_is_read_back():
    steps = workflow()["jobs"]["publish"]["steps"]
    names = [step.get("name", "") for step in steps]
    assert names.index("Attest complete bundle") < names.index("Stage draft release")
    assert names.index("Verify staged bytes and attestations") < names.index(
        "Publish complete release"
    )
    assert names.index("Publish complete release") < names.index("Read back published release")
    stage = steps[names.index("Stage draft release")]["run"]
    assert "--draft" in stage and '--target "$SOURCE_SHA"' in stage
    assert "--clobber" not in stage
    verify = steps[names.index("Verify staged bytes and attestations")]["run"]
    assert "gh release download" in verify and "--validate" in verify
    assert "gh attestation verify" in verify and "--cert-identity" in verify
    assert "--deny-self-hosted-runners" in verify
    assert "gh api" in steps[names.index("Read back published release")]["run"]
