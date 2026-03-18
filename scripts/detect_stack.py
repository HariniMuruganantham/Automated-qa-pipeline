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
    data = gh_get(f"/repos/{REPO}/contents/{path}")
    if isinstance(data, list):
        return [{"name": f["name"], "type": f["type"]} for f in data]
    return []


def collect_signals():
    signals = {}

    # Language breakdown
    signals["languages"] = gh_get(f"/repos/{REPO}/languages") or {}

    # Root file + directory listing
    root_entries          = get_dir_listing("")
    signals["root_files"] = [e["name"] for e in root_entries if e["type"] == "file"]
    signals["root_dirs"]  = [e["name"] for e in root_entries if e["type"] == "dir"]

    # Repo topics
    topics            = gh_get(f"/repos/{REPO}/topics")
    signals["topics"] = (topics or {}).get("names", [])

    # Primary dependency file — picked by top language
    top_lang = max(
        signals["languages"], key=signals["languages"].get, default=""
    ).lower()

    dep_candidates = {
        "typescript": ["package.json"],
        "javascript": ["package.json"],
        "python":     ["requirements.txt", "pyproject.toml", "setup.py"],
        "java":       ["pom.xml", "build.gradle", "build.gradle.kts"],
        "kotlin":     ["build.gradle.kts", "build.gradle"],
        "go":         ["go.mod"],
        "ruby":       ["Gemfile"],
        "php":        ["composer.json"],
        "rust":       ["Cargo.toml"],
        "c#":         ["*.csproj"],
    }

    for candidate in dep_candidates.get(top_lang, ["package.json"]):
        if candidate.startswith("*"):
            continue
        content = get_file(candidate)
        if content:
            signals["dep_file"] = {"name": candidate, "content": content[:4000]}
            break

    # Backend directory — handles fullstack repos (e.g. React + Python Lambda)
    if "backend" in signals["root_dirs"]:
        backend_entries          = get_dir_listing("backend")
        backend_files            = [e["name"] for e in backend_entries if e["type"] == "file"]
        signals["backend_files"] = backend_files
        for bf in ["requirements.txt", "package.json", "go.mod", "pom.xml"]:
            if bf in backend_files:
                content = get_file(f"backend/{bf}")
                if content:
                    signals["backend_dep_file"] = {"name": bf, "content": content[:2000]}
                    break
    else:
        signals["backend_files"] = []

    # pages/ directory — Streamlit multipage apps, Next.js pages router
    if "pages" in signals["root_dirs"]:
        pages_entries          = get_dir_listing("pages")
        signals["pages_files"] = [e["name"] for e in pages_entries if e["type"] == "file"]
    else:
        signals["pages_files"] = []

    # src/ directory — confirms React via App.tsx / main.tsx
    if "src" in signals["root_dirs"]:
        src_entries          = get_dir_listing("src")
        signals["src_files"] = [e["name"] for e in src_entries if e["type"] == "file"]
    else:
        signals["src_files"] = []

    # Infra directory detection
    for infra_dir in ["terraform", "infra", "cdk", "pulumi", "iac"]:
        if infra_dir in signals["root_dirs"]:
            signals["infra_dir_found"] = infra_dir
            break

    # Config files — reveal build tool / test runner / deployment target
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
        "app.py",          # Streamlit / Flask entry point
        "manage.py",       # Django
        "main.py",         # FastAPI / generic Python
        "server.py",       # generic Python server
        "pyproject.toml",  # modern Python projects
        "Makefile",
    ]
    signals["config_files_present"] = [
        f for f in config_files_to_check if f in signals["root_files"]
    ]

    # .env.example — reveals external services and auth type
    env_example = get_file(".env.example") or get_file(".env.sample") or ""
    signals["env_example"] = env_example[:600]

    # README first 80 lines
    readme                    = get_file("README.md") or get_file("readme.md") or ""
    signals["readme_preview"] = "\n".join(readme.splitlines()[:80])

    return signals


# ── 2. schema + prompt ────────────────────────────────────────────────────────

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
    ],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You are a senior software architect and QA engineer.
Analyse the repository signals and return ONLY a valid JSON object.
No markdown, no explanation, no code fences. Raw JSON only.

Detection rules — apply every one:

Node / JS / TS:
- vite.config.ts or vite.config.js present   → build_tool: "vite", test_runner: "vitest"
- jest.config.ts or jest.config.js present   → test_runner: "jest"
- next.config.js or next.config.ts present   → framework: "nextjs"
- angular.json present                       → framework: "angular", frontend_framework: "angular"
- nuxt.config.js present                     → framework: "nuxt", frontend_framework: "vue"
- "react" in package.json deps               → frontend_framework: "react", framework: "react"
- "vue" in package.json deps                 → frontend_framework: "vue"
- "svelte" in package.json deps              → frontend_framework: "svelte"
- App.tsx or main.tsx in src/ files          → frontend_framework: "react"
- TypeScript + package.json                  → runtime: "node"

Python:
- app.py in root files                       → likely Streamlit or Flask
- manage.py in root files                    → framework: "django", test_runner: "pytest"
- main.py in root files                      → likely FastAPI, test_runner: "pytest"
- "streamlit" in requirements.txt            → framework: "streamlit", has_frontend: true, frontend_dir: "pages" if pages/ exists else ".", test_runner: "pytest", api_style: "none"
- "fastapi" in requirements.txt              → framework: "fastapi", test_runner: "pytest", api_style: "REST"
- "flask" in requirements.txt               → framework: "flask", test_runner: "pytest", api_style: "REST"
- "django" in requirements.txt              → framework: "django", test_runner: "pytest", api_style: "REST"
- "gradio" in requirements.txt              → framework: "gradio", has_frontend: true, test_runner: "pytest"
- "langchain" in requirements.txt           → backend_framework: "langchain"
- "llamaindex" or "llama-index" in reqs     → backend_framework: "llamaindex"
- "boto3" in requirements.txt               → backend_framework: "aws-lambda", deployment_target includes "aws"
- "faiss" or "faiss-cpu" in requirements    → database: "faiss"
- "chromadb" in requirements.txt            → database: "chromadb"
- "pinecone" in requirements.txt            → database: "pinecone"
- Python primary language + no framework    → test_runner: "pytest" as default

Backend (any language):
- backend/requirements.txt has boto3        → backend_framework: "aws-lambda"
- backend/requirements.txt has django       → backend_framework: "django"
- backend/requirements.txt has fastapi      → backend_framework: "fastapi"
- backend/requirements.txt has flask        → backend_framework: "flask"

Infrastructure:
- HCL in languages OR terraform/ dir found  → infra_tool: "terraform"
- cdk/ dir found                            → infra_tool: "cdk"
- pulumi/ dir found                         → infra_tool: "pulumi"

Deployment:
- DynamoDB / Lambda / S3 / CloudFront found → deployment_target includes "aws"
- "groq" or "openai" or "anthropic" in reqs → auth_type: "api_key"
- vercel.json present                       → deployment_target includes "vercel"

Caching / CDN:
- CloudFront = CDN → cdn: "CloudFront", NOT in cache field
- cache = only Redis / Memcached / ElastiCache — null if not present

Test recommendations:
- test_types_recommended must NEVER be empty
- Always include: unit, smoke, sanity, regression
- has_frontend true  → also include: e2e, visual, a11y, performance
- api_style is REST/GraphQL/gRPC → also include: api
- database detected → also include: load
- Streamlit/Gradio has_frontend=true but skip_performance=true (no public URL by default)

Skip flags:
- skip_e2e and skip_visual: true ONLY if has_frontend is false
- skip_performance: true if no public deployment URL detectable

Defaults:
- Use null for unknown strings
- false for unknown booleans
- [] for unknown arrays
- Every required field must be present — never omit any"""


def build_user_prompt(signals):
    backend_dep   = signals.get("backend_dep_file", {})
    backend_files = signals.get("backend_files", [])

    return f"""Analyse this repository and return the full stack detection JSON.

## Language breakdown (bytes — higher = more code in that language)
{json.dumps(signals["languages"], indent=2)}

## Root-level FILES
{json.dumps(signals["root_files"], indent=2)}

## Root-level DIRECTORIES
{json.dumps(signals["root_dirs"], indent=2)}

## Config / entry-point files present (CRITICAL for framework detection)
{json.dumps(signals["config_files_present"], indent=2)}

## src/ directory files (App.tsx or main.tsx = React confirmed)
{json.dumps(signals.get("src_files", []), indent=2)}

## pages/ directory files (Streamlit multipage or Next.js pages router)
{json.dumps(signals.get("pages_files", []), indent=2)}

## Backend directory files
{json.dumps(backend_files, indent=2)}

## Repository topics
{json.dumps(signals["topics"], indent=2)}

## .env.example (reveals external services and auth type)
{signals.get("env_example", "not found")}

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

Return ONLY the JSON object. Apply every detection rule. No field may be omitted."""


# ── 3. GPT-4o call ────────────────────────────────────────────────────────────

def call_gpt(signals):
    client   = OpenAI(api_key=OPENAI_KEY)
    response = client.chat.completions.create(
        model="gpt-4o",
        response_format={"type": "json_object"},
        temperature=0,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": build_user_prompt(signals)},
        ],
    )
    return json.loads(response.choices[0].message.content)


# ── 4. deterministic post-processing (Python rules always win over GPT) ───────

def post_process(raw, signals):
    dep_content  = signals.get("dep_file", {}).get("content", "")
    dep_name     = signals.get("dep_file", {}).get("name", "")
    backend_dep  = signals.get("backend_dep_file", {}).get("content", "")
    config_files = signals.get("config_files_present", [])
    src_files    = signals.get("src_files", [])
    root_dirs    = signals.get("root_dirs", [])
    env_example  = signals.get("env_example", "")
    top_lang     = max(
        signals["languages"], key=signals["languages"].get, default=""
    ).lower()

    # ── runtime ───────────────────────────────────────────────────────────────
    if not raw.get("runtime"):
        lang_runtime_map = {
            "typescript": "node", "javascript": "node",
            "python": "python", "go": "go",
            "java": "jvm", "kotlin": "jvm",
            "rust": "rust", "ruby": "ruby", "php": "php",
        }
        raw["runtime"] = lang_runtime_map.get(top_lang)

    # ── Python repo: parse requirements.txt at root ───────────────────────────
    if dep_name in ("requirements.txt", "pyproject.toml", "setup.py") and dep_content:
        reqs = dep_content.lower()

        # framework
        if not raw.get("framework"):
            if "streamlit" in reqs:
                raw["framework"] = "streamlit"
            elif "fastapi" in reqs:
                raw["framework"] = "fastapi"
            elif "flask" in reqs:
                raw["framework"] = "flask"
            elif "django" in reqs:
                raw["framework"] = "django"
            elif "gradio" in reqs:
                raw["framework"] = "gradio"

        # backend framework / AI orchestration layer
        if not raw.get("backend_framework"):
            if "langchain" in reqs:
                raw["backend_framework"] = "langchain"
            elif "llamaindex" in reqs or "llama-index" in reqs or "llama_index" in reqs:
                raw["backend_framework"] = "llamaindex"
            elif "boto3" in reqs or "mangum" in reqs:
                raw["backend_framework"] = "aws-lambda"

        # test runner — always pytest for Python
        if not raw.get("test_runner"):
            raw["test_runner"] = "pytest"

        # database — vector stores and SQL
        if not raw.get("database"):
            if "faiss" in reqs:
                raw["database"] = "faiss"
            elif "chromadb" in reqs or "chroma" in reqs:
                raw["database"] = "chromadb"
            elif "pinecone" in reqs:
                raw["database"] = "pinecone"
            elif "weaviate" in reqs:
                raw["database"] = "weaviate"
            elif "psycopg2" in reqs or "asyncpg" in reqs:
                raw["database"] = "postgresql"
            elif "pymysql" in reqs or "mysqlclient" in reqs:
                raw["database"] = "mysql"
            elif "pymongo" in reqs:
                raw["database"] = "mongodb"

        # auth type from deps
        if not raw.get("auth_type"):
            if "groq" in reqs or "openai" in reqs or "anthropic" in reqs:
                raw["auth_type"] = "api_key"

        # Streamlit specific rules
        if raw.get("framework") == "streamlit":
            raw["has_frontend"] = True
            raw["frontend_framework"] = "streamlit"
            if not raw.get("frontend_dir"):
                raw["frontend_dir"] = "pages" if "pages" in root_dirs else "."
            raw["api_style"]        = "none"
            raw["skip_e2e"]         = False   # Playwright can test Streamlit in browser
            raw["skip_visual"]      = False
            raw["skip_performance"] = True    # no public URL by default

        # Gradio specific rules
        if raw.get("framework") == "gradio":
            raw["has_frontend"]       = True
            raw["frontend_framework"] = "gradio"
            raw["api_style"]          = "REST"   # Gradio exposes REST API
            raw["skip_e2e"]           = False
            raw["skip_performance"]   = True

        # FastAPI / Flask / Django → REST API
        if raw.get("framework") in ("fastapi", "flask", "django") and not raw.get("api_style"):
            raw["api_style"] = "REST"

    # ── auth type from .env.example ───────────────────────────────────────────
    if not raw.get("auth_type") and env_example:
        env_lower = env_example.lower()
        if any(k in env_lower for k in ["groq", "openai", "anthropic", "huggingface", "hf_token"]):
            raw["auth_type"] = "api_key"
        elif "jwt" in env_lower or "jwt_secret" in env_lower:
            raw["auth_type"] = "jwt"
        elif "session_secret" in env_lower or "secret_key" in env_lower:
            raw["auth_type"] = "session"

    # ── Node/TS repo: parse package.json ──────────────────────────────────────
    if dep_name == "package.json" and dep_content:
        try:
            pkg  = json.loads(dep_content)
            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}

            # runtime version from engines field
            if not raw.get("runtime_version"):
                engines = pkg.get("engines", {})
                if "node" in engines:
                    raw["runtime_version"] = (
                        engines["node"]
                        .replace(">=", "").replace("^", "").replace("~", "")
                        .split(".")[0].strip()
                    )

            # frontend framework
            if not raw.get("frontend_framework"):
                if "react" in deps or "@types/react" in deps:
                    raw["frontend_framework"] = "react"
                elif "vue" in deps:
                    raw["frontend_framework"] = "vue"
                elif "svelte" in deps:
                    raw["frontend_framework"] = "svelte"
                elif "@angular/core" in deps:
                    raw["frontend_framework"] = "angular"

            # framework — meta-framework takes priority over UI framework
            if not raw.get("framework"):
                if "next" in deps:
                    raw["framework"] = "nextjs"
                elif "@nestjs/core" in deps:
                    raw["framework"] = "nestjs"
                elif "express" in deps:
                    raw["framework"] = "express"
                elif "fastify" in deps:
                    raw["framework"] = "fastify"
                elif raw.get("frontend_framework"):
                    raw["framework"] = raw["frontend_framework"]

            # has_frontend
            frontend_pkgs = ["react", "vue", "svelte", "@angular/core", "next", "nuxt"]
            if not raw.get("has_frontend") and any(p in deps for p in frontend_pkgs):
                raw["has_frontend"] = True

        except json.JSONDecodeError:
            pass

    # ── confirm React from src/ files (fallback) ──────────────────────────────
    react_entry = {"App.tsx", "App.jsx", "main.tsx", "main.jsx", "index.tsx", "index.jsx"}
    if not raw.get("frontend_framework") and any(f in react_entry for f in src_files):
        raw["frontend_framework"] = "react"
        raw["has_frontend"]       = True

    if not raw.get("framework") and raw.get("frontend_framework"):
        raw["framework"] = raw["frontend_framework"]

    # ── test runner from config files ─────────────────────────────────────────
    if not raw.get("test_runner"):
        if any("vitest" in f for f in config_files):
            raw["test_runner"]        = "vitest"
            raw["test_runner_config"] = next((f for f in config_files if "vitest" in f), None)
        elif any("vite" in f for f in config_files):
            raw["test_runner"] = "vitest"
        elif any("jest" in f for f in config_files):
            raw["test_runner"]        = "jest"
            raw["test_runner_config"] = next((f for f in config_files if "jest" in f), None)
        elif top_lang == "python":
            raw["test_runner"] = "pytest"   # always pytest as Python default

    # ── backend framework from backend/ dep file ──────────────────────────────
    if backend_dep and not raw.get("backend_framework"):
        b = backend_dep.lower()
        if "boto3" in b or "mangum" in b or "aws-lambda" in b:
            raw["backend_framework"] = "aws-lambda"
        elif "django" in b:
            raw["backend_framework"] = "django"
        elif "fastapi" in b:
            raw["backend_framework"] = "fastapi"
        elif "flask" in b:
            raw["backend_framework"] = "flask"
        elif "langchain" in b:
            raw["backend_framework"] = "langchain"

    # ── api_style from backend framework ──────────────────────────────────────
    if not raw.get("api_style") or raw.get("api_style") == "none":
        rest_frameworks = (
            "aws-lambda", "express", "fastapi",
            "flask", "django", "nestjs", "fastify",
        )
        if raw.get("backend_framework") in rest_frameworks:
            raw["api_style"] = "REST"

    # ── infra tool from dir ───────────────────────────────────────────────────
    if not raw.get("infra_tool") and signals.get("infra_dir_found"):
        d = signals["infra_dir_found"]
        raw["infra_tool"] = {"terraform": "terraform", "pulumi": "pulumi", "cdk": "cdk"}.get(d, d)
        raw["infra_dir"]  = d

    # ── cloudfront → cdn not cache ────────────────────────────────────────────
    if raw.get("cache") and "cloudfront" in str(raw["cache"]).lower():
        if not raw.get("cdn"):
            raw["cdn"] = raw["cache"]
        raw["cache"] = None

    # ── cache: [] or "" → null ────────────────────────────────────────────────
    if raw.get("cache") in ([], ""):
        raw["cache"] = None

    # ── skip flags driven by has_frontend ─────────────────────────────────────
    if raw.get("has_frontend"):
        raw["skip_e2e"]    = False
        raw["skip_visual"] = False
        # skip_performance already set correctly for Streamlit/Gradio above
        # only set to False here if not already explicitly set to True
        if raw.get("framework") not in ("streamlit", "gradio"):
            raw["skip_performance"] = False
    else:
        raw["skip_e2e"]    = True
        raw["skip_visual"] = True

    # ── test_types_recommended: build complete list ────────────────────────────
    recommended = list(raw.get("test_types_recommended") or [])

    # always ensure base types exist
    for t in ["unit", "smoke", "sanity", "regression"]:
        if t not in recommended:
            recommended.append(t)

    # frontend types
    if raw.get("has_frontend"):
        for t in ["e2e", "visual", "a11y"]:
            if t not in recommended:
                recommended.append(t)
        # performance only if not a local-only app (Streamlit/Gradio)
        if raw.get("framework") not in ("streamlit", "gradio"):
            if "performance" not in recommended:
                recommended.append("performance")

    # api tests
    if raw.get("api_style") in ("REST", "GraphQL", "gRPC", "tRPC"):
        if "api" not in recommended:
            recommended.append("api")

    # load tests
    if raw.get("database") or raw.get("backend_framework"):
        if "load" not in recommended:
            recommended.append("load")

    raw["test_types_recommended"] = recommended

    return raw


# ── 5. validate + enrich ──────────────────────────────────────────────────────

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
    "test_runner":            None,
    "test_runner_config":     None,
    "existing_test_dir":      None,
    "api_style":              None,
    "openapi_spec":           None,
    "api_prefix":             None,
    "infra_tool":             None,
    "infra_dir":              None,
    "deployment_target":      None,
    "database":               None,
    "cdn":                    None,
    "cache":                  None,
    "has_docker":             False,
    "has_docker_compose":     False,
    "auth_type":              None,
    "test_types_recommended": ["unit", "smoke", "regression"],
    "skip_e2e":               True,
    "skip_visual":            True,
    "skip_performance":       False,
}


def validate_and_enrich(raw, signals):
    from jsonschema import validate, ValidationError

    # Fill any missing fields with safe defaults
    for k, v in SAFE_DEFAULTS.items():
        if k not in raw:
            print(f"[WARN] Missing field '{k}' — applying default: {v}")
            raw[k] = v

    try:
        validate(instance=raw, schema=SCHEMA)
        print("[validate] Schema validation passed ✓")
    except ValidationError as e:
        print(f"[WARN] Schema validation: {e.message}")

    # Metadata
    raw["repo"]        = REPO
    raw["detected_at"] = datetime.datetime.utcnow().isoformat() + "Z"
    raw["sha"]         = os.environ.get("GITHUB_SHA", "unknown")

    # Confidence score
    key_fields = [
        "primary_language", "framework", "runtime",
        "test_runner", "deployment_target",
        "infra_tool", "build_tool",
    ]
    # api_style, frontend_framework, database are optional for pure Python apps
    if raw.get("has_frontend"):
        key_fields += ["frontend_framework"]
    if raw.get("api_style") and raw.get("api_style") != "none":
        key_fields += ["api_style"]
    if raw.get("backend_framework"):
        key_fields += ["backend_framework"]

    filled            = sum(1 for f in key_fields if raw.get(f) not in (None, "unknown", []))
    raw["confidence"] = round(filled / len(key_fields), 2)

    return raw


# ── 6. main ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"[detect_stack] Analysing {REPO} ...")

    signals = collect_signals()

    top = max(signals["languages"], key=signals["languages"].get, default="unknown")
    print(f"[detect_stack] Top language  : {top}")
    print(f"[detect_stack] Root dirs     : {signals['root_dirs']}")
    print(f"[detect_stack] Root files    : {signals['root_files']}")
    print(f"[detect_stack] Config files  : {signals['config_files_present']}")
    print(f"[detect_stack] src/ files    : {signals.get('src_files', [])}")
    print(f"[detect_stack] pages/ files  : {signals.get('pages_files', [])}")
    print(f"[detect_stack] Backend files : {signals.get('backend_files', [])}")
    print(f"[detect_stack] Infra dir     : {signals.get('infra_dir_found', 'none')}")
    print(f"[detect_stack] .env.example  : {'found' if signals.get('env_example') else 'not found'}")

    # Step 1: GPT-4o analysis
    raw_manifest = call_gpt(signals)
    print("[detect_stack] GPT responded ✓")

    # Step 2: deterministic Python overrides (always win over GPT)
    raw_manifest = post_process(raw_manifest, signals)
    print("[detect_stack] Post-processing applied ✓")

    # Step 3: schema validation + metadata
    manifest = validate_and_enrich(raw_manifest, signals)
    print(f"[detect_stack] Confidence    : {manifest['confidence']}")

    with open("manifest.json", "w") as f:
        json.dump(manifest, f, indent=2)

    print("[detect_stack] manifest.json written ✓")
    print(json.dumps(manifest, indent=2))