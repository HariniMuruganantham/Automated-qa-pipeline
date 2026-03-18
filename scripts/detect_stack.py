import os
import json
import base64
import datetime
import requests
from openai import OpenAI

# ── env ───────────────────────────────────────────────────────────────────────
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
REPO         = os.environ["TARGET_REPO"]
OPENAI_KEY   = os.environ["OPENAI_API_KEY"]

GH      = "https://api.github.com"
HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

client = OpenAI(api_key=OPENAI_KEY)


# ── 1. collect raw signals ────────────────────────────────────────────────────
# Rule: collect everything, filter nothing. GPT decides what matters.

def gh_get(path):
    r = requests.get(f"{GH}{path}", headers=HEADERS)
    return r.json() if r.status_code == 200 else None


def get_file(path):
    data = gh_get(f"/repos/{REPO}/contents/{path}")
    if data and isinstance(data, dict) and "content" in data:
        return base64.b64decode(data["content"]).decode(errors="ignore")
    return None


def get_dir_listing(path=""):
    data = gh_get(f"/repos/{REPO}/contents/{path}")
    if isinstance(data, list):
        return [{"name": f["name"], "type": f["type"]} for f in data]
    return []


def collect_signals():
    signals = {}

    # ── language breakdown ────────────────────────────────────────────────────
    signals["languages"] = gh_get(f"/repos/{REPO}/languages") or {}

    # ── root listing ──────────────────────────────────────────────────────────
    root_entries          = get_dir_listing("")
    signals["root_files"] = [e["name"] for e in root_entries if e["type"] == "file"]
    signals["root_dirs"]  = [e["name"] for e in root_entries if e["type"] == "dir"]

    # ── repo topics ───────────────────────────────────────────────────────────
    topics            = gh_get(f"/repos/{REPO}/topics")
    signals["topics"] = (topics or {}).get("names", [])

    # ── ALL dependency / manifest files (full content, not just names) ────────
    # We try every known dep file regardless of detected language
    dep_files_to_try = [
        # JS / TS
        "package.json",
        # Python
        "requirements.txt", "pyproject.toml", "setup.py", "Pipfile",
        # Java / Kotlin
        "pom.xml", "build.gradle", "build.gradle.kts",
        # Go
        "go.mod",
        # Ruby
        "Gemfile",
        # PHP
        "composer.json",
        # Rust
        "Cargo.toml",
        # .NET
        "*.csproj",
        # Swift
        "Package.swift",
        # Dart / Flutter
        "pubspec.yaml",
    ]

    signals["dep_files"] = {}
    for dep in dep_files_to_try:
        if dep.startswith("*"):
            continue
        if dep in signals["root_files"]:
            content = get_file(dep)
            if content:
                # Truncate large files — 3000 chars is enough for any dep file
                signals["dep_files"][dep] = content[:3000]

    # ── entry point files (full content) ──────────────────────────────────────
    # These tell GPT exactly what the app does and what framework it uses
    entry_points = [
        "app.py", "main.py", "server.py", "index.py", "run.py",
        "app.js", "main.js", "server.js", "index.js",
        "app.ts", "main.ts", "server.ts", "index.ts",
        "main.go", "cmd/main.go",
        "main.rs", "src/main.rs",
        "Application.java", "Main.java",
        "app.rb", "config.ru",
        "lib/main.dart",
    ]

    signals["entry_points"] = {}
    for ep in entry_points:
        if ep in signals["root_files"]:
            content = get_file(ep)
            if content:
                # First 100 lines is enough to identify framework
                signals["entry_points"][ep] = "\n".join(content.splitlines()[:100])

    # ── config files (full content for key ones) ──────────────────────────────
    config_files = [
        "vite.config.ts", "vite.config.js",
        "jest.config.ts", "jest.config.js",
        "vitest.config.ts", "vitest.config.js",
        "webpack.config.js", "rollup.config.js",
        "next.config.js", "next.config.ts",
        "nuxt.config.js", "nuxt.config.ts",
        "angular.json", "svelte.config.js",
        "tailwind.config.js", "tailwind.config.ts",
        "tsconfig.json",
        "vercel.json", "netlify.toml", "railway.toml",
        "fly.toml", "render.yaml", "app.yaml",
        "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
        ".nvmrc", ".python-version", ".ruby-version", ".tool-versions",
        "Makefile", "justfile",
        "pyproject.toml",
    ]

    signals["config_files"] = {}
    for cf in config_files:
        if cf in signals["root_files"]:
            content = get_file(cf)
            if content:
                signals["config_files"][cf] = content[:1000]

    # ── .env.example — reveals services, APIs, auth ───────────────────────────
    env = get_file(".env.example") or get_file(".env.sample") or get_file(".env.template") or ""
    signals["env_example"] = env[:800]

    # ── subdirectory listings ─────────────────────────────────────────────────
    # Check common dirs — gives GPT structural understanding
    interesting_dirs = [
        "src", "app", "pages", "backend", "frontend", "api",
        "lib", "pkg", "cmd", "internal", "server", "client",
        "components", "routes", "controllers", "models", "services",
        "tests", "test", "__tests__", "spec", "e2e",
        "terraform", "infra", "cdk", "pulumi", "iac", "deploy",
    ]

    signals["dir_contents"] = {}
    for d in interesting_dirs:
        if d in signals["root_dirs"]:
            entries = get_dir_listing(d)
            files   = [e["name"] for e in entries if e["type"] == "file"]
            subdirs = [e["name"] for e in entries if e["type"] == "dir"]
            signals["dir_contents"][d] = {"files": files, "subdirs": subdirs}

    # ── existing CI/CD workflows ───────────────────────────────────────────────
    if ".github" in signals["root_dirs"]:
        wf_entries = get_dir_listing(".github/workflows")
        signals["existing_workflows"] = [e["name"] for e in wf_entries if e["type"] == "file"]
    else:
        signals["existing_workflows"] = []

    # ── README (more lines = more context for GPT) ────────────────────────────
    readme                    = get_file("README.md") or get_file("readme.md") or ""
    signals["readme_preview"] = "\n".join(readme.splitlines()[:150])

    return signals


# ── 2. schema definition ──────────────────────────────────────────────────────

SCHEMA = {
    "type": "object",
    "required": [
        "primary_language", "all_languages",
        "framework", "build_tool", "runtime", "runtime_version",
        "has_frontend", "frontend_framework", "frontend_dir",
        "backend_runtime", "backend_dir", "backend_framework",
        "test_runner", "test_runner_config", "existing_test_dir",
        "api_style", "openapi_spec", "api_prefix",
        "infra_tool", "infra_dir", "deployment_target",
        "database", "cdn", "cache",
        "has_docker", "has_docker_compose",
        "auth_type",
        "test_types_recommended",
        "skip_e2e", "skip_visual", "skip_performance",
        "confidence", "confidence_notes",
    ],
    "additionalProperties": False,
}


# ── 3. GPT Pass 1 — free-form reasoning ───────────────────────────────────────
# No schema. No constraints. GPT thinks freely about what the repo is.

PASS1_SYSTEM = """You are a principal software engineer doing a thorough code review of a repository you've never seen before.

Your task: deeply analyse all the signals provided and write a detailed technical description of this repository.

Cover ALL of these in your analysis:
1. Primary language and all detected languages
2. Framework(s) — be specific (e.g. "Streamlit 1.x multipage app" not just "Python web app")
3. Build tool and bundler if any
4. Runtime and version
5. Frontend details — framework, entry point, component structure
6. Backend details — framework, entry point, API style (REST/GraphQL/gRPC/none), routes/endpoints visible
7. Test setup — runner, existing test files, test directories
8. Database — type, ORM/client library
9. Caching layer if any
10. Authentication pattern — JWT, sessions, API keys, OAuth, none
11. Infrastructure and IaC — Terraform, CDK, Pulumi, etc.
12. Deployment target — cloud provider, platform (Vercel, Railway, Fly.io, HuggingFace Spaces, etc.)
13. CDN if any
14. Docker usage
15. What test types make sense for this specific repo and why
16. Your confidence level for each major detection and why

Be specific. Reference actual file names and content you can see.
Your analysis will be used to generate a structured manifest — accuracy is critical."""


def build_pass1_prompt(signals):
    # Format all dep files
    dep_section = ""
    for name, content in signals["dep_files"].items():
        dep_section += f"\n### {name}\n{content}\n"

    # Format entry points
    entry_section = ""
    for name, content in signals["entry_points"].items():
        entry_section += f"\n### {name} (first 100 lines)\n{content}\n"

    # Format config files
    config_section = ""
    for name, content in signals["config_files"].items():
        config_section += f"\n### {name}\n{content}\n"

    # Format dir contents
    dir_section = ""
    for dirname, contents in signals["dir_contents"].items():
        dir_section += f"\n{dirname}/  →  files: {contents['files']}"
        if contents["subdirs"]:
            dir_section += f"  subdirs: {contents['subdirs']}"

    return f"""Analyse this repository thoroughly and describe its complete technical stack.

## Repository: {REPO}

## Language breakdown (bytes)
{json.dumps(signals["languages"], indent=2)}

## Root-level files
{json.dumps(signals["root_files"])}

## Root-level directories
{json.dumps(signals["root_dirs"])}

## Repository topics
{json.dumps(signals["topics"])}

## Dependency / manifest files (FULL CONTENT)
{dep_section if dep_section else "None found"}

## Application entry points (first 100 lines each)
{entry_section if entry_section else "None found"}

## Configuration files
{config_section if config_section else "None found"}

## Directory structure
{dir_section if dir_section else "No interesting directories found"}

## Existing CI/CD workflows
{json.dumps(signals["existing_workflows"])}

## .env.example (reveals external services and auth)
{signals.get("env_example", "Not found")}

## README (first 150 lines)
{signals.get("readme_preview", "Not found")}

Based on ALL of the above, provide your detailed technical analysis.
Be specific about versions, frameworks, and patterns you can see in the actual file content."""


# ── 4. GPT Pass 2 — structured mapping ───────────────────────────────────────
# Feed Pass 1 reasoning back in, now ask for exact schema output.

PASS2_SYSTEM = """You are a senior software architect converting a technical analysis into a structured JSON manifest.

You will be given:
1. Raw repository signals
2. A detailed technical analysis already written about this repo

Your job: map that analysis into the exact JSON schema provided.

Rules:
- Use the analysis as your primary source of truth
- Return ONLY valid JSON — no markdown, no explanation, no code fences
- null for unknown/not-applicable strings
- false for unknown booleans  
- [] for unknown/empty arrays
- Every required field must be present
- For test_types_recommended: always include unit, smoke, sanity, regression as base. Add e2e/visual/a11y/performance if has_frontend. Add api if REST/GraphQL/gRPC detected. Add load if database or backend detected.
- skip_e2e and skip_visual: true ONLY if has_frontend is false
- skip_performance: true if no public deployment URL detected (local apps, Streamlit without hosting, etc.)
- confidence: 0.0-1.0 float — your honest assessment of how accurate the manifest is
- confidence_notes: brief string explaining what you're certain about vs unsure of
- Python projects: if no test runner is specified, default test_runner to "pytest"
- frontend_dir: always set when has_frontend is true — use the actual directory name from the analysis
- backend_framework: if the app uses LangChain/LlamaIndex/an AI orchestration layer, that is the backend_framework — the UI framework (Streamlit/Gradio) goes in framework and frontend_framework only
- load tests: only include if there is a running HTTP server or external API endpoint — FAISS, SQLite, local vector stores do not qualify"""

def build_pass2_prompt(signals, pass1_analysis):
    return f"""Convert the following technical analysis into a structured JSON manifest.

## Technical analysis (your source of truth)
{pass1_analysis}

## Additional raw signals for reference
Repository: {REPO}
Languages: {json.dumps(signals["languages"])}
Root files: {json.dumps(signals["root_files"])}
Root dirs: {json.dumps(signals["root_dirs"])}
Dep files found: {list(signals["dep_files"].keys())}
Entry points found: {list(signals["entry_points"].keys())}
Config files found: {list(signals["config_files"].keys())}
.env.example: {signals.get("env_example", "not found")[:300]}

## Required JSON schema (every field is required)
{json.dumps(SCHEMA, indent=2)}

Return ONLY the JSON object matching this schema exactly."""


# ── 5. two-pass GPT detection ─────────────────────────────────────────────────

def detect_with_gpt(signals):
    print("[detect_stack] GPT Pass 1 — free-form analysis ...")

    pass1_response = client.chat.completions.create(
        model="gpt-4o",
        temperature=0,
        messages=[
            {"role": "system", "content": PASS1_SYSTEM},
            {"role": "user",   "content": build_pass1_prompt(signals)},
        ],
    )
    pass1_analysis = pass1_response.choices[0].message.content
    print("[detect_stack] Pass 1 complete ✓")
    print(f"[detect_stack] Analysis preview: {pass1_analysis[:300]}...")

    print("[detect_stack] GPT Pass 2 — structured mapping ...")

    pass2_response = client.chat.completions.create(
        model="gpt-4o",
        response_format={"type": "json_object"},  # forces valid JSON
        temperature=0,
        messages=[
            {"role": "system", "content": PASS2_SYSTEM},
            {"role": "user",   "content": build_pass2_prompt(signals, pass1_analysis)},
        ],
    )
    manifest = json.loads(pass2_response.choices[0].message.content)
    print("[detect_stack] Pass 2 complete ✓")

    return manifest, pass1_analysis


# ── 6. validate + enrich (Python — zero detection logic) ─────────────────────
# Python's ONLY job here: fill missing schema fields, add metadata.
# It never overrides GPT detections.

SAFE_DEFAULTS = {
    "primary_language":       "unknown",
    "all_languages":          [],
    "framework":              None,
    "build_tool":             None,
    "runtime":                None,
    "runtime_version":        None,
    "has_frontend":           False,
    "frontend_framework":     None,
    "frontend_dir":           None,
    "backend_runtime":        None,
    "backend_dir":            None,
    "backend_framework":      None,
    "test_runner":             None,
    "test_runner_config":     None,
    "existing_test_dir":      None,
    "api_style":              None,
    "openapi_spec":           None,
    "api_prefix":             None,
    "infra_tool":             None,
    "infra_dir":              None,
    "deployment_target":      [],
    "database":               None,
    "cdn":                    None,
    "cache":                  None,
    "has_docker":             False,
    "has_docker_compose":     False,
    "auth_type":              None,
    "test_types_recommended": ["unit", "smoke", "sanity", "regression"],
    "skip_e2e":               True,
    "skip_visual":            True,
    "skip_performance":       False,
    "confidence":             0.0,
    "confidence_notes":       "Unable to assess",
}


def validate_and_enrich(raw, signals, pass1_analysis):
    from jsonschema import validate, ValidationError

    # Fill any missing fields with safe defaults — never override GPT values
    for k, v in SAFE_DEFAULTS.items():
        if k not in raw:
            print(f"[WARN] Missing field '{k}' — applying safe default")
            raw[k] = v

    # Schema validation
    try:
        validate(instance=raw, schema=SCHEMA)
        print("[validate] Schema validation passed ✓")
    except ValidationError as e:
        print(f"[WARN] Schema validation: {e.message}")

    # Metadata — added by Python, never by GPT
    raw["repo"]             = REPO
    raw["detected_at"]      = datetime.datetime.utcnow().isoformat() + "Z"
    raw["sha"]              = os.environ.get("GITHUB_SHA", "unknown")
    raw["pass1_analysis"]   = pass1_analysis   # stored for debugging / audit

    # Clamp confidence to valid range
    try:
        raw["confidence"] = round(max(0.0, min(1.0, float(raw["confidence"]))), 2)
    except (TypeError, ValueError):
        raw["confidence"] = 0.0

    return raw


# ── 7. main ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"[detect_stack] Analysing {REPO} ...")

    # Collect all signals
    signals = collect_signals()

    top = max(signals["languages"], key=signals["languages"].get, default="unknown")
    print(f"[detect_stack] Top language     : {top}")
    print(f"[detect_stack] Root dirs        : {signals['root_dirs']}")
    print(f"[detect_stack] Dep files found  : {list(signals['dep_files'].keys())}")
    print(f"[detect_stack] Entry points     : {list(signals['entry_points'].keys())}")
    print(f"[detect_stack] Config files     : {list(signals['config_files'].keys())}")
    print(f"[detect_stack] .env.example     : {'found' if signals.get('env_example') else 'not found'}")
    print(f"[detect_stack] Subdirs scanned  : {list(signals['dir_contents'].keys())}")

    # Two-pass GPT detection
    raw_manifest, pass1_analysis = detect_with_gpt(signals)

    # Validate + add metadata (no detection logic)
    manifest = validate_and_enrich(raw_manifest, signals, pass1_analysis)

    print(f"[detect_stack] Confidence       : {manifest['confidence']}")
    print(f"[detect_stack] Confidence notes : {manifest['confidence_notes']}")

    # Write manifest (without pass1_analysis for the clean version)
    clean_manifest = {k: v for k, v in manifest.items() if k != "pass1_analysis"}
    with open("manifest.json", "w") as f:
        json.dump(clean_manifest, f, indent=2)

    # Write full debug version including pass1 analysis
    with open("manifest_debug.json", "w") as f:
        json.dump(manifest, f, indent=2)

    print("[detect_stack] manifest.json written ✓")
    print("[detect_stack] manifest_debug.json written ✓ (includes Pass 1 analysis)")
    print(json.dumps(clean_manifest, indent=2))