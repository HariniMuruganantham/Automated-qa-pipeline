import os, json, base64, requests
from openai import OpenAI

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
REPO         = os.environ["TARGET_REPO"]          # e.g. "myorg/my-app"
OPENAI_KEY   = os.environ["OPENAI_API_KEY"]

GH = "https://api.github.com"
HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28"
}

# ── 1. collect raw signals ──────────────────────────────────────────────────

def gh_get(path):
    r = requests.get(f"{GH}{path}", headers=HEADERS)
    return r.json() if r.status_code == 200 else None

def get_file_content(path):
    data = gh_get(f"/repos/{REPO}/contents/{path}")
    if data and "content" in data:
        return base64.b64decode(data["content"]).decode(errors="ignore")
    return None

def collect_signals():
    signals = {}

    # Language breakdown
    signals["languages"] = gh_get(f"/repos/{REPO}/languages") or {}

    # Root file listing (names only — tells GPT what exists without downloading all)
    root = gh_get(f"/repos/{REPO}/contents/")
    signals["root_files"] = [f["name"] for f in (root or []) if isinstance(f, dict)]

    # Repo topics (e.g. ["nestjs", "postgresql", "docker"])
    topics = gh_get(f"/repos/{REPO}/topics")
    signals["topics"] = (topics or {}).get("names", [])

    # Dependency file — pick based on top language
    top_lang = max(signals["languages"], key=signals["languages"].get, default="").lower()
    dep_file_map = {
        "typescript": "package.json", "javascript": "package.json",
        "python": "requirements.txt", "java": "pom.xml",
        "kotlin": "build.gradle.kts", "go": "go.mod",
        "ruby": "Gemfile", "php": "composer.json",
        "rust": "Cargo.toml", "c#": "*.csproj",
    }
    dep_file = dep_file_map.get(top_lang)
    if dep_file and not dep_file.startswith("*"):
        content = get_file_content(dep_file)
        if content:
            # Truncate to 3000 chars — enough for GPT, avoids token bloat
            signals["dep_file"] = {"name": dep_file, "content": content[:3000]}

    # README first 80 lines
    readme = get_file_content("README.md") or get_file_content("readme.md") or ""
    signals["readme_preview"] = "\n".join(readme.splitlines()[:80])

    return signals


# ── 2. GPT-4o call ──────────────────────────────────────────────────────────

SCHEMA = {
    "type": "object",
    "required": [
        "primary_language","all_languages","framework","runtime",
        "runtime_version","test_runner","test_runner_config",
        "existing_test_dir","api_style","openapi_spec","api_prefix",
        "has_frontend","frontend_framework","frontend_dir",
        "database","cache","has_docker","has_docker_compose",
        "auth_type","test_types_recommended",
        "skip_e2e","skip_visual","skip_performance"
    ],
    "additionalProperties": False
}

SYSTEM_PROMPT = """You are a senior software architect.
Analyse the repository signals provided and return ONLY a valid JSON object.
No markdown. No explanation. No code fences. Raw JSON only.
Every field in the schema is required. Use null for unknown string fields,
false for unknown booleans, and empty arrays for unknown arrays.
Be as accurate as possible — downstream CI jobs depend on this output."""

def build_user_prompt(signals):
    return f"""Analyse this repository and return the stack detection JSON.

## Repository signals

### Language breakdown (bytes)
{json.dumps(signals['languages'], indent=2)}

### Root-level files
{json.dumps(signals['root_files'])}

### Repository topics
{json.dumps(signals['topics'])}

### Dependency file ({signals.get('dep_file', {}).get('name', 'not found')})
{signals.get('dep_file', {}).get('content', 'not available')}

### README preview
{signals.get('readme_preview', '')}

## Required output schema
{json.dumps(SCHEMA, indent=2)}

Return ONLY the JSON. Every required field must be present."""


def call_gpt(signals):
    client = OpenAI(api_key=OPENAI_KEY)
    response = client.chat.completions.create(
        model="gpt-4o",
        response_format={"type": "json_object"},   # ← forces valid JSON output
        temperature=0,                              # ← deterministic
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": build_user_prompt(signals)},
        ]
    )
    return json.loads(response.choices[0].message.content)


# ── 3. validate + enrich ────────────────────────────────────────────────────

import datetime, math

def validate_and_enrich(raw, signals):
    from jsonschema import validate, ValidationError
    try:
        validate(instance=raw, schema=SCHEMA)
    except ValidationError as e:
        print(f"[WARN] Schema validation issue: {e.message} — applying defaults")
        # Fill any missing required fields with safe defaults rather than crashing
        defaults = {
            "primary_language": "unknown", "all_languages": [],
            "framework": None, "runtime": None, "runtime_version": None,
            "test_runner": "jest", "test_runner_config": None,
            "existing_test_dir": None, "api_style": "REST",
            "openapi_spec": None, "api_prefix": "/api",
            "has_frontend": False, "frontend_framework": None, "frontend_dir": None,
            "database": None, "cache": None, "has_docker": False,
            "has_docker_compose": False, "auth_type": None,
            "test_types_recommended": ["unit","smoke","regression","api"],
            "skip_e2e": True, "skip_visual": True, "skip_performance": False
        }
        for k, v in defaults.items():
            raw.setdefault(k, v)

    # Enrich with metadata that GPT doesn't need to guess
    raw["repo"]         = REPO
    raw["detected_at"]  = datetime.datetime.utcnow().isoformat() + "Z"
    raw["sha"]          = os.environ.get("GITHUB_SHA", "unknown")

    # Confidence score: fraction of non-null required string fields
    str_fields = ["primary_language","framework","runtime","test_runner","api_style","auth_type"]
    filled     = sum(1 for f in str_fields if raw.get(f) not in (None, "unknown"))
    raw["confidence"] = round(filled / len(str_fields), 2)

    # Safety: if no frontend detected, skip UI tests automatically
    if not raw.get("has_frontend"):
        raw["skip_e2e"]        = True
        raw["skip_visual"]     = True
        raw["skip_performance"] = True

    return raw


# ── 4. main ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"[detect_stack] Analysing {REPO}...")

    signals = collect_signals()
    print(f"[detect_stack] Signals collected — top language: "
          f"{max(signals['languages'], key=signals['languages'].get, default='unknown')}")

    raw_manifest = call_gpt(signals)
    print("[detect_stack] GPT responded ✓")

    manifest = validate_and_enrich(raw_manifest, signals)
    print(f"[detect_stack] Confidence: {manifest['confidence']}")

    with open("manifest.json", "w") as f:
        json.dump(manifest, f, indent=2)

    print("[detect_stack] manifest.json written ✓")
    print(json.dumps(manifest, indent=2))