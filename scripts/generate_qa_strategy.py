import json
from pathlib import Path


MANIFEST_PATH = Path("manifest.json")
STRATEGY_PATH = Path("qa_strategy.json")
TEST_CASES_PATH = Path("test_cases.json")


HIGH_TRUST_FIELDS = {
    "primary_language",
    "all_languages",
    "has_frontend",
    "has_docker",
    "has_docker_compose",
    "test_runner",
    "existing_test_dir",
}

MEDIUM_TRUST_FIELDS = {
    "framework",
    "backend_framework",
    "frontend_framework",
    "runtime",
    "api_style",
    "infra_tool",
    "deployment_target",
    "database",
    "auth_type",
}


def _load_manifest() -> dict:
    if not MANIFEST_PATH.exists():
        raise FileNotFoundError("manifest.json not found. Run detect_stack.py first.")
    with MANIFEST_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def _collect_reliability_warnings(manifest: dict) -> list[str]:
    warnings = []
    confidence = float(manifest.get("confidence", 0.0) or 0.0)
    if confidence < 0.7:
        warnings.append(
            "Manifest confidence is below 0.70; keep medium-trust fields as advisory."
        )
    for field in sorted(MEDIUM_TRUST_FIELDS):
        if manifest.get(field) in (None, [], "", "unknown"):
            warnings.append(f"Medium-trust field '{field}' is missing or weak.")
    return warnings


def _base_test_groups(manifest: dict) -> list[dict]:
    groups = [
        {
            "name": "unit",
            "enabled": True,
            "priority": "P0",
            "reason": "Core logic validation for fast feedback.",
        },
        {
            "name": "smoke",
            "enabled": True,
            "priority": "P0",
            "reason": "Basic health checks for every change.",
        },
        {
            "name": "regression",
            "enabled": True,
            "priority": "P1",
            "reason": "Prevent reintroduction of known defects.",
        },
    ]

    has_frontend = bool(manifest.get("has_frontend", False))
    api_style = manifest.get("api_style")
    has_docker = bool(manifest.get("has_docker", False))
    database = manifest.get("database")

    groups.append(
        {
            "name": "api",
            "enabled": api_style in {"REST", "GraphQL", "gRPC", "tRPC"},
            "priority": "P0",
            "reason": "API contract and behavior checks.",
        }
    )
    groups.append(
        {
            "name": "integration",
            "enabled": bool(database) or has_docker,
            "priority": "P1",
            "reason": "Service and datastore interaction validation.",
        }
    )
    groups.append(
        {
            "name": "e2e",
            "enabled": has_frontend,
            "priority": "P1",
            "reason": "User journey validation for UI flows.",
        }
    )
    groups.append(
        {
            "name": "visual",
            "enabled": has_frontend,
            "priority": "P2",
            "reason": "UI regression guard for important pages.",
        }
    )
    groups.append(
        {
            "name": "security",
            "enabled": True,
            "priority": "P1",
            "reason": "Auth/input validation and dependency-risk checks.",
        }
    )
    groups.append(
        {
            "name": "performance",
            "enabled": bool(manifest.get("skip_performance") is False),
            "priority": "P2",
            "reason": "Latency/throughput baseline validation when applicable.",
        }
    )
    return groups


def _suggest_tooling(manifest: dict) -> dict:
    lang = (manifest.get("primary_language") or "").lower()
    frontend = (manifest.get("frontend_framework") or "").lower()
    test_runner = (manifest.get("test_runner") or "").lower()

    if lang in {"python"}:
        unit = test_runner if test_runner else "pytest"
        api = "pytest + requests + schemathesis"
    elif lang in {"javascript", "typescript"}:
        unit = test_runner if test_runner else "vitest"
        api = "supertest + contract assertions"
    elif lang in {"java"}:
        unit = "junit"
        api = "rest-assured"
    elif lang in {"go"}:
        unit = "go test"
        api = "httptest"
    else:
        unit = test_runner if test_runner else "language-native test runner"
        api = "HTTP client + assertions"

    if frontend in {"react", "vue", "angular", "svelte", "nextjs"}:
        ui_e2e = "playwright"
        visual = "playwright snapshots"
    elif manifest.get("has_frontend"):
        ui_e2e = "playwright"
        visual = "snapshot testing"
    else:
        ui_e2e = None
        visual = None

    return {
        "unit": unit,
        "api": api,
        "e2e": ui_e2e,
        "visual": visual,
        "security": "dependency scan + auth/input negative tests",
        "performance": "k6 or locust baseline scenario",
    }


def _generate_test_cases(manifest: dict) -> list[dict]:
    has_frontend = bool(manifest.get("has_frontend", False))
    api_style = manifest.get("api_style")
    auth_type = manifest.get("auth_type")

    cases = [
        {
            "id": "TC-UNIT-001",
            "type": "unit",
            "priority": "P0",
            "title": "Core business logic happy-path behavior",
            "objective": "Validate deterministic logic with representative valid inputs.",
        },
        {
            "id": "TC-UNIT-002",
            "type": "unit",
            "priority": "P0",
            "title": "Core business logic boundary conditions",
            "objective": "Validate behavior on edge values and null/empty inputs.",
        },
        {
            "id": "TC-SMOKE-001",
            "type": "smoke",
            "priority": "P0",
            "title": "Application startup and health check",
            "objective": "Ensure app boots and basic health endpoint/command succeeds.",
        },
    ]

    if api_style in {"REST", "GraphQL", "gRPC", "tRPC"}:
        cases.extend(
            [
                {
                    "id": "TC-API-001",
                    "type": "api",
                    "priority": "P0",
                    "title": "Primary API endpoint successful response",
                    "objective": "Validate response code, schema, and critical fields.",
                },
                {
                    "id": "TC-API-002",
                    "type": "api",
                    "priority": "P1",
                    "title": "Invalid payload handling",
                    "objective": "Ensure invalid input returns safe and expected errors.",
                },
            ]
        )

    if auth_type:
        cases.append(
            {
                "id": "TC-SEC-001",
                "type": "security",
                "priority": "P1",
                "title": "Unauthorized access is blocked",
                "objective": "Confirm protected operations fail without valid auth.",
            }
        )

    if has_frontend:
        cases.extend(
            [
                {
                    "id": "TC-E2E-001",
                    "type": "e2e",
                    "priority": "P1",
                    "title": "Critical user flow end-to-end",
                    "objective": "Validate the main user journey from entry to completion.",
                },
                {
                    "id": "TC-VIS-001",
                    "type": "visual",
                    "priority": "P2",
                    "title": "Critical page visual baseline",
                    "objective": "Detect significant UI visual regressions on key screens.",
                },
            ]
        )

    return cases


def build_strategy(manifest: dict) -> dict:
    groups = _base_test_groups(manifest)
    enabled_groups = [g["name"] for g in groups if g["enabled"]]
    warnings = _collect_reliability_warnings(manifest)

    return {
        "version": "1.0",
        "source": "manifest.json",
        "confidence": manifest.get("confidence", 0.0),
        "high_trust_fields": {k: manifest.get(k) for k in sorted(HIGH_TRUST_FIELDS)},
        "medium_trust_fields": {k: manifest.get(k) for k in sorted(MEDIUM_TRUST_FIELDS)},
        "reliability_warnings": warnings,
        "enabled_test_groups": enabled_groups,
        "test_groups": groups,
        "suggested_tooling": _suggest_tooling(manifest),
        "execution_order": ["unit", "smoke", "api", "integration", "e2e", "security", "visual", "performance"],
        "qa_policy": {
            "block_merge_on": ["unit", "smoke"],
            "recommended_for_blocking": ["api", "integration", "e2e", "security"],
            "advisory_only": ["visual", "performance"],
        },
    }


def main() -> None:
    manifest = _load_manifest()
    strategy = build_strategy(manifest)
    test_cases = _generate_test_cases(manifest)

    with STRATEGY_PATH.open("w", encoding="utf-8") as f:
        json.dump(strategy, f, indent=2)
    with TEST_CASES_PATH.open("w", encoding="utf-8") as f:
        json.dump({"test_cases": test_cases}, f, indent=2)

    print(f"[qa_strategy] Wrote {STRATEGY_PATH}")
    print(f"[qa_strategy] Wrote {TEST_CASES_PATH}")
    print(json.dumps(strategy, indent=2))


if __name__ == "__main__":
    main()
