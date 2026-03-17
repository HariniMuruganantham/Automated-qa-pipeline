import os
import json
import base64
import datetime
import requests
from openai import OpenAI

# ── env ───────────────────────────────────────────────────────────────────────
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
REPO         = os.environ["TARGET_REPO"]   # e.g. "HariniMuruganantham/aws-cloud-resume-challenge"
OPENAI_KEY   = os.environ["OPENAI_API_KEY"]

GH      = "https://api.github.com"
HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


# ── 1. collect raw signals ────────────────────────────────────────────────────

def gh_get(path):
    r = requests.get(f"{GH}{path}", headers=HEADERS)
    return r.json() if r.status_code == 200 else None


def get_file(path):
    data = gh_get(f"/repos/{REPO}/contents/{path}")
    if data and isinstance(data, dict) and "content" in data:
        return base64.b64decode(data["content"]).decode(errors="ignore")
    return None


def get_dir_listing(path=""):
    """Return list of {name, type} dicts for a directory."""
    data = gh_get(f"/repos/{REPO}/contents/{path}")
    if isinstance(data, list):
        return [{"name": f["name"], "type": f["type"]} for f in data]
    return []


def collect_signals():
    signals = {}

    # Language breakdown
    signals["languages"] = gh_get(f"/repos/{REPO}/languages") or {}

    # Root file + directory listing
    root_entries = get_dir_listing("")
    signals["root_files"] = [e["name"] for e in root_entries if e["type"] == "file"]
    signals["root_dirs"]  = [e["name"] for e in root_entries if e["type"] == "dir"]

    # Repo topics
    topics = gh_get(f"/repos/{REPO}/topics")
    signals["topics"] = (topics or {}).get("names", [])

    # Dependency file — picked by top language
    top_lang = max(
        signals["languages"], key=signals["languages"].get, default=""
    ).lower()

    dep_candidates = {
        "typescript":  ["package.json"],
        "javascript":  ["package.json"],
        "python":      ["requirements.txt", "pyproject.toml", "setup.py"],
        "java":        ["pom.xml", "build.gradle", "build.gradle.kts"],
        "kotlin":      ["build.gradle.kts", "build.gradle"],
        "go":          ["go.mod"],
        "ruby":        ["Gemfile"],
        "php":         ["composer.json"],
        "rust":        ["Cargo.toml"],
        "c#":          ["*.csproj"],
    }

    for candidate in dep_candidates.get(top_lang, ["package.json"]):
        if candidate.startswith("*"):
            continue
        content = get_file(candidate)
        if content:
            signals["dep_file"] = {"name": candidate, "content": content[:4000]}
            break

    # Check for backend directory dependency file (handles fullstack repos)
    if "backend" in signals["root_dirs"]:
        backend_entries = get_dir_listing("backend")
        backend_files   = [e["name"] for e in backend_entries if e["type"] == "file"]
        signals["backend_files"] = backend_files
        for bf in ["requirements.txt", "package.json", "go.mod", "pom.xml"]:
            if bf in backend_files:
                content = get_file(f"backend/{bf}")
                if content:
                    signals["backend_dep_file"] = {"name": bf, "content": content[:2000]}
                    break

    # Check for infra directory
    for infra_dir in ["terraform", "infra", "cdk", "pulumi", "iac"]:
        if infra_dir in signals["root_dirs"]:
            signals["infra_dir_found"] = infra_dir
            break

    # Config files that reveal build tool / test runner
    config_files_to_check = [
        "vite.config.ts", "vite.config.js",
        "jest.config.ts", "jest.config.js",
        "vitest.config.ts", "vitest.config.js",
        "webpack.config.js",
        "next.config.js", "next.config.ts",
        "nuxt.config.js", "nuxt.config.ts",
        "angular.json",
        "vercel.json",
        "netlify.toml",
        ".nvmrc",
        "Dockerfile",
        "docker-compose.yml", "docker-compose.yaml",
    ]
    signals["config_files_present"] = [
        f for f in config_files_to_check if f in signals["root_files"]
    ]

    # README first 80 lines
    readme = get_file("README.md") or get_file("readme.md") or ""
    signals["readme_preview"] = "\n".join(readme.splitlines()[:80])

    return signals


# ── 2. prompt ─────────────────────────────────────────────────────────────────

SCHEMA = {
    "type": "object",
    "required": [
        # identity
        "primary_language", "all_languages",
        # frontend
        "framework", "build_tool", "runtime", "runtime_version",
        "has_frontend", "frontend_framework", "frontend_dir",
        # backend
        "backend_runtime", "backend_dir", "backend_framework",
        # testing
        "test_runner", "test_runner_config", "existing_test_dir",
        # api
        "api_style", "openapi_spec", "api_prefix",
        # infra / deployment
        "infra_tool", "infra_dir", "deployment_target",
        "database", "cdn", "cache",
        "has_docker", "has_docker_compose",
        # auth
        "auth_type",
        # test plan
        "test_types_recommended",
        "skip_e2e", "skip_visual", "skip_performance",
    ],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You are a senior software architect and QA engineer.
Analyse the repository signals provided and return ONLY a valid JSON object.
No markdown. No explanation. No code fences. Raw JSON only.

Critical detection rules — follow these exactly:
- If vite.config.ts or vite.config.js is in root files → build_tool: "vite", test_runner: "vitest"
- If jest.config.ts or jest.config.js is in root files → test_runner: "jest"
- If next.config.js or next.config.ts is present → framework: "nextjs"
- If angular.json is present → framework: "angular", frontend_framework: "angular"
- If nuxt.config.js is present → framework: "nuxt", frontend_framework: "vue"
- If backend/ directory has requirements.txt → backend_runtime: "python"
- If backend/ directory has package.json → backend_runtime: "node"
- If HCL language is in all_languages OR terraform/ dir exists → infra_tool: "terraform"
- If DynamoDB or Lambda or S3 or CloudFront detected anywhere → deployment_target: "aws"
- If vercel.json is present → deployment_target includes "vercel"
- CloudFront is a CDN — put it in cdn field, NOT in cache field
- cache field is only for in-memory caches like Redis or Memcached
- test_types_recommended must NEVER be empty — always include at least ["unit", "smoke", "regression"]
- If has_frontend is true → always add "e2e", "visual", "a11y", "performance" to test_types_recommended
- If api_style is REST or GraphQL → always add "api" to test_types_recommended
- skip_e2e: only true if has_frontend is false
- skip_visual: only true if has_frontend is false
- Use null for unknown string fields, false for unknown booleans, [] for unknown arrays
- Every field in the schema is required — never omit any field"""


def build_user_prompt(signals):
    backend_dep = signals.get("backend_dep_file", {})
    backend_files = signals.get("backend_files", [])

    return f"""Analyse this repository and return the full stack detection JSON.

## Language breakdown (bytes — higher = more code in that language)
{json.dumps(signals["languages"], indent=2)}

## Root-level FILES (exact filenames — use these to detect build tool and test runner)
{json.dumps(signals["root_files"], indent=2)}

## Root-level DIRECTORIES (use these to detect monorepo structure)
{json.dumps(signals["root_dirs"], indent=2)}

## Config files present (critical for detecting build tool / test runner / deployment)
{json.dumps(signals["config_files_present"], indent=2)}

## Backend directory files (if backend/ exists)
{json.dumps(backend_files, indent=2)}

## Repository topics (user-defined tags)
{json.dumps(signals["topics"], indent=2)}

## Primary dependency file ({signals.get("dep_file", {}).get("name", "not found")})
{signals.get("dep_file", {}).get("content", "not available")}

## Backend dependency file ({backend_dep.get("name", "not found")})
{backend_dep.get("content", "not available")}

## Infra directory detected
{signals.get("infra_dir_found", "none")}

## README preview (first 80 lines)
{signals.get("readme_preview", "")}

## Required JSON output schema
{json.dumps(SCHEMA, indent=2)}

Return ONLY the JSON object. Every required field must be present.
Apply all critical detection rules from your instructions."""


# ── 3. GPT-4o call ────────────────────────────────────────────────────────────

def call_gpt(signals):
    client = OpenAI(api_key=OPENAI_KEY)
    response = client.chat.completions.create(
        model="gpt-4o",
        response_format={"type": "json_object"},  # forces valid JSON — no prose
        temperature=0,                             # deterministic output
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": build_user_prompt(signals)},
        ],
    )
    raw = response.choices[0].message.content
    return json.loads(raw)


# ── 4. validate + enrich ──────────────────────────────────────────────────────

SAFE_DEFAULTS = {
    "primary_language":      "unknown",
    "all_languages":         [],
    "framework":             None,
    "build_tool":            None,
    "runtime":               None,
    "runtime_version":       None,
    "has_frontend":          False,
    "frontend_framework":    None,
    "frontend_dir":          None,
    "backend_runtime":       None,
    "backend_dir":           None,
    "backend_framework":     None,
    "test_runner":           None,
    "test_runner_config":    None,
    "existing_test_dir":     None,
    "api_style":             "REST",
    "openapi_spec":          None,
    "api_prefix":            None,
    "infra_tool":            None,
    "infra_dir":             None,
    "deployment_target":     None,
    "database":              None,
    "cdn":                   None,
    "cache":                 None,
    "has_docker":            False,
    "has_docker_compose":    False,
    "auth_type":             None,
    "test_types_recommended": ["unit", "smoke", "regression"],
    "skip_e2e":              True,
    "skip_visual":           True,
    "skip_performance":      False,
}


def validate_and_enrich(raw, signals):
    from jsonschema import validate, ValidationError

    # Fill any missing fields with safe defaults before validation
    for k, v in SAFE_DEFAULTS.items():
        if k not in raw:
            print(f"[WARN] Missing field '{k}' — applying default: {v}")
            raw[k] = v

    try:
        validate(instance=raw, schema=SCHEMA)
        print("[validate] Schema validation passed ✓")
    except ValidationError as e:
        print(f"[WARN] Schema validation issue: {e.message}")

    # ── post-processing safety rules ──────────────────────────────────────────

    # 1. If CloudFront is in cache, move it to cdn
    if raw.get("cache") and "cloudfront" in str(raw["cache"]).lower():
        raw["cdn"]   = raw["cache"]
        raw["cache"] = None

    # 2. If has_frontend is true, skip flags must be false
    if raw.get("has_frontend"):
        raw["skip_e2e"]         = False
        raw["skip_visual"]      = False
        raw["skip_performance"] = False

    # 3. test_types_recommended must never be empty
    if not raw.get("test_types_recommended"):
        raw["test_types_recommended"] = ["unit", "smoke", "regression"]
        if raw.get("has_frontend"):
            raw["test_types_recommended"] += ["e2e", "visual", "a11y", "performance"]
        if raw.get("api_style") in ("REST", "GraphQL", "gRPC"):
            raw["test_types_recommended"].append("api")

    # 4. Override-detect test_runner from config files present
    config_files = signals.get("config_files_present", [])
    if not raw.get("test_runner"):
        if any("vitest" in f for f in config_files):
            raw["test_runner"] = "vitest"
            raw["test_runner_config"] = next(
                (f for f in config_files if "vitest" in f), None
            )
        elif any("vite" in f for f in config_files):
            raw["test_runner"] = "vitest"  # vite projects default to vitest
        elif any("jest" in f for f in config_files):
            raw["test_runner"] = "jest"
            raw["test_runner_config"] = next(
                (f for f in config_files if "jest" in f), None
            )

    # 5. Override-detect infra_tool from signals
    if not raw.get("infra_tool") and signals.get("infra_dir_found"):
        d = signals["infra_dir_found"]
        raw["infra_tool"] = (
            "terraform" if d == "terraform" else
            "pulumi"    if d == "pulumi"    else
            "cdk"       if d == "cdk"       else d
        )
        raw["infra_dir"] = d

    # ── add metadata ──────────────────────────────────────────────────────────
    raw["repo"]        = REPO
    raw["detected_at"] = datetime.datetime.utcnow().isoformat() + "Z"
    raw["sha"]         = os.environ.get("GITHUB_SHA", "unknown")

    # Confidence: fraction of key fields that are non-null
    key_fields = [
        "primary_language", "framework", "runtime",
        "test_runner", "api_style", "deployment_target",
        "database", "infra_tool",
    ]
    filled = sum(1 for f in key_fields if raw.get(f) not in (None, "unknown"))
    raw["confidence"] = round(filled / len(key_fields), 2)

    return raw


# ── 5. main ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"[detect_stack] Analysing {REPO} ...")

    signals = collect_signals()

    top = max(signals["languages"], key=signals["languages"].get, default="unknown")
    print(f"[detect_stack] Top language: {top}")
    print(f"[detect_stack] Root dirs:    {signals['root_dirs']}")
    print(f"[detect_stack] Config files: {signals['config_files_present']}")
    print(f"[detect_stack] Infra dir:    {signals.get('infra_dir_found', 'none')}")

    raw_manifest = call_gpt(signals)
    print("[detect_stack] GPT responded ✓")

    manifest = validate_and_enrich(raw_manifest, signals)
    print(f"[detect_stack] Confidence: {manifest['confidence']}")

    with open("manifest.json", "w") as f:
        json.dump(manifest, f, indent=2)

    print("[detect_stack] manifest.json written ✓")
    print(json.dumps(manifest, indent=2))