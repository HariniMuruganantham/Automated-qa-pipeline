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

# ── constants ─────────────────────────────────────────────────────────────────
MAX_RECURSIVE_FILES = 300   # stop recursion after this many files (avoids huge monorepos)
MAX_FILE_CONTENT    = 3000  # max chars per file sent to GPT

DEP_FILENAMES = {
    "package.json", "requirements.txt", "pyproject.toml", "setup.py",
    "Pipfile", "pom.xml", "build.gradle", "build.gradle.kts",
    "go.mod", "Gemfile", "composer.json", "Cargo.toml",
    "Package.swift", "pubspec.yaml", "mix.exs",
}

ENTRY_POINT_NAMES = {
    "app.py", "main.py", "server.py", "index.py", "run.py", "wsgi.py", "asgi.py",
    "app.js", "main.js", "server.js", "index.js",
    "app.ts", "main.ts", "server.ts", "index.ts",
    "main.go", "main.rs", "Application.java", "Main.java",
    "app.rb", "config.ru", "lib/main.dart",
}

CONFIG_FILENAMES = {
    "vite.config.ts", "vite.config.js", "jest.config.ts", "jest.config.js",
    "vitest.config.ts", "vitest.config.js", "webpack.config.js",
    "next.config.js", "next.config.ts", "nuxt.config.js", "nuxt.config.ts",
    "angular.json", "svelte.config.js", "tailwind.config.js",
    "tsconfig.json", "vercel.json", "netlify.toml", "railway.toml",
    "fly.toml", "render.yaml", "app.yaml", "Dockerfile",
    "docker-compose.yml", "docker-compose.yaml",
    ".nvmrc", ".python-version", ".ruby-version", ".tool-versions",
    "Makefile", "justfile", "pyproject.toml",
}

SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", ".pytest_cache",
    "dist", "build", ".next", ".nuxt", "coverage",
    ".venv", "venv", "env", ".env",
    "vendor", "target", "bin", "obj",
}


# ── 1. GitHub helpers ─────────────────────────────────────────────────────────

def gh_get(path):
    r = requests.get(f"{GH}{path}", headers=HEADERS)
    return r.json() if r.status_code == 200 else None


def get_file(path):
    data = gh_get(f"/repos/{REPO}/contents/{path}")
    if data and isinstance(data, dict) and "content" in data:
        return base64.b64decode(data["content"]).decode(errors="ignore")
    return None


# ── 2. UPGRADE 1: recursive repo traversal ───────────────────────────────────

def walk_repo(path="", _count=None):
    """
    Recursively walk the entire repo tree.
    Returns list of full file paths e.g. ["backend/requirements.txt", "src/App.tsx"]
    Stops after MAX_RECURSIVE_FILES to handle large monorepos gracefully.
    """
    if _count is None:
        _count = [0]

    items = gh_get(f"/repos/{REPO}/contents/{path}")
    if not isinstance(items, list):
        return []

    all_files = []
    for item in items:
        if _count[0] >= MAX_RECURSIVE_FILES:
            break

        name = item.get("name", "")
        item_type = item.get("type", "")
        item_path = item.get("path", "")

        if item_type == "file":
            all_files.append(item_path)
            _count[0] += 1
        elif item_type == "dir" and name not in SKIP_DIRS:
            all_files.extend(walk_repo(item_path, _count))

    return all_files


# ── 3. collect raw signals ────────────────────────────────────────────────────

def collect_signals():
    signals = {}

    # Language breakdown
    signals["languages"] = gh_get(f"/repos/{REPO}/languages") or {}

    # Repo topics
    topics            = gh_get(f"/repos/{REPO}/topics")
    signals["topics"] = (topics or {}).get("names", [])

    # UPGRADE 1+2: full recursive file list
    print("[collect] Walking full repo tree recursively ...")
    all_files                = walk_repo()
    signals["all_files"]     = all_files
    signals["total_files"]   = len(all_files)

    # Root-level for quick reference
    root_files = [f for f in all_files if "/" not in f]
    root_dirs  = list({f.split("/")[0] for f in all_files if "/" in f})
    signals["root_files"] = root_files
    signals["root_dirs"]  = sorted(root_dirs)

    print(f"[collect] Found {len(all_files)} files, {len(root_dirs)} root dirs")

    # UPGRADE 2: find dep files ANYWHERE in the repo (not just root)
    signals["dep_files"] = {}
    for filepath in all_files:
        filename = filepath.split("/")[-1]
        if filename in DEP_FILENAMES:
            content = get_file(filepath)
            if content:
                signals["dep_files"][filepath] = content[:MAX_FILE_CONTENT]

    print(f"[collect] Dep files found: {list(signals['dep_files'].keys())}")

    # Entry point files (full content — highest signal priority)
    signals["entry_points"] = {}
    for filepath in all_files:
        filename = filepath.split("/")[-1]
        if filename in ENTRY_POINT_NAMES:
            content = get_file(filepath)
            if content:
                # First 120 lines — enough to see all imports and framework usage
                signals["entry_points"][filepath] = "\n".join(content.splitlines()[:120])

    print(f"[collect] Entry points found: {list(signals['entry_points'].keys())}")

    # Config files (full content)
    signals["config_files"] = {}
    for filepath in all_files:
        filename = filepath.split("/")[-1]
        if filename in CONFIG_FILENAMES:
            content = get_file(filepath)
            if content:
                signals["config_files"][filepath] = content[:1500]

    # .env.example — reveals external services and auth pattern
    for env_file in [".env.example", ".env.sample", ".env.template"]:
        if env_file in root_files:
            content = get_file(env_file)
            if content:
                signals["env_example"] = content[:800]
                break
    else:
        signals["env_example"] = ""

    # Existing test files — tells GPT what testing already exists
    test_files = [
        f for f in all_files
        if any(seg in f for seg in ["/test/", "/tests/", "/__tests__/", "/spec/", "/e2e/"])
        or f.endswith(("_test.go", "_test.py", ".test.ts", ".test.js", ".spec.ts", ".spec.js"))
    ]
    signals["existing_test_files"] = test_files[:30]   # cap at 30

    # Existing CI/CD workflows
    workflow_files = [f for f in all_files if ".github/workflows" in f]
    signals["existing_workflows"] = workflow_files

    # README (more lines = richer context for GPT)
    readme                    = get_file("README.md") or get_file("readme.md") or ""
    signals["readme_preview"] = "\n".join(readme.splitlines()[:150])

    return signals


# ── 4. UPGRADE 3: heuristic pre-detection ────────────────────────────────────
# Lightweight rule engine that runs BEFORE GPT.
# Produces high-confidence hints fed directly into the GPT prompt.
# Reduces hallucination by anchoring GPT to verified facts.

def heuristic_detection(signals):
    hints     = {}
    conflicts = []

    # Concatenate all dep file contents for scanning
    all_deps = " ".join(signals["dep_files"].values()).lower()

    # Concatenate all entry point contents for scanning
    all_entry = " ".join(signals["entry_points"].values()).lower()

    # ── backend framework (priority: entry point > deps) ─────────────────────
    # UPGRADE 4: entry-point code beats dependency declarations
    if "from fastapi" in all_entry or "import fastapi" in all_entry:
        hints["backend_framework"] = "fastapi"
        hints["api_style"]         = "REST"
    elif "from flask" in all_entry or "import flask" in all_entry:
        hints["backend_framework"] = "flask"
        hints["api_style"]         = "REST"
    elif "from django" in all_entry or "import django" in all_entry:
        hints["backend_framework"] = "django"
        hints["api_style"]         = "REST"
    elif "import streamlit" in all_entry or "import streamlit as st" in all_entry:
        hints["backend_framework"] = "streamlit"
        hints["framework"]         = "streamlit"
        hints["has_frontend"]      = True
    elif "import gradio" in all_entry or "import gradio as gr" in all_entry:
        hints["framework"]    = "gradio"
        hints["has_frontend"] = True
    elif "express()" in all_entry or "require('express')" in all_entry:
        hints["backend_framework"] = "express"
        hints["api_style"]         = "REST"
    elif "from nestjs" in all_entry or "@nestjs" in all_entry:
        hints["backend_framework"] = "nestjs"
        hints["api_style"]         = "REST"
    # Fallback to deps if entry point doesn't reveal it
    elif "fastapi" in all_deps:
        hints["backend_framework"] = "fastapi"
        hints["api_style"]         = "REST"
    elif "django" in all_deps:
        hints["backend_framework"] = "django"
        hints["api_style"]         = "REST"
    elif "flask" in all_deps:
        hints["backend_framework"] = "flask"
        hints["api_style"]         = "REST"
    elif "streamlit" in all_deps:
        hints["framework"]    = "streamlit"
        hints["has_frontend"] = True

    # ── AI / orchestration layer ──────────────────────────────────────────────
    if "langchain" in all_deps or "from langchain" in all_entry:
        hints["ai_framework"] = "langchain"
    elif "llamaindex" in all_deps or "llama_index" in all_deps:
        hints["ai_framework"] = "llamaindex"

    # ── frontend framework ────────────────────────────────────────────────────
    # Check package.json deps directly
    for path, content in signals["dep_files"].items():
        if path.endswith("package.json"):
            try:
                pkg  = json.loads(content)
                deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
                if "react" in deps or "@types/react" in deps:
                    hints["frontend_framework"] = "react"
                    hints["has_frontend"]        = True
                elif "vue" in deps:
                    hints["frontend_framework"] = "vue"
                    hints["has_frontend"]        = True
                elif "svelte" in deps:
                    hints["frontend_framework"] = "svelte"
                    hints["has_frontend"]        = True
                elif "@angular/core" in deps:
                    hints["frontend_framework"] = "angular"
                    hints["has_frontend"]        = True
                # Meta-frameworks
                if "next" in deps:
                    hints["framework"] = "nextjs"
                elif "@nestjs/core" in deps:
                    hints["framework"] = "nestjs"
                elif "express" in deps:
                    hints.setdefault("framework", "express")
            except json.JSONDecodeError:
                pass

    # ── test runner ───────────────────────────────────────────────────────────
    config_names = list(signals["config_files"].keys())
    if any("vitest" in c for c in config_names):
        hints["test_runner"] = "vitest"
    elif any("jest" in c for c in config_names):
        hints["test_runner"] = "jest"
    elif "pytest" in all_deps or "python" in " ".join(signals["languages"].keys()).lower():
        if not hints.get("test_runner"):
            hints["test_runner"] = "pytest"

    # ── database / vector store ───────────────────────────────────────────────
    db_map = {
        "faiss": "faiss", "chromadb": "chromadb", "pinecone": "pinecone",
        "weaviate": "weaviate", "psycopg2": "postgresql", "asyncpg": "postgresql",
        "pymysql": "mysql", "pymongo": "mongodb", "redis": "redis",
        "boto3": "dynamodb",  # common in AWS Lambda + DynamoDB setups
    }
    for keyword, db_name in db_map.items():
        if keyword in all_deps:
            hints.setdefault("database", db_name)
            break

    # ── auth type ─────────────────────────────────────────────────────────────
    env = signals.get("env_example", "").lower()
    if any(k in env for k in ["groq", "openai", "anthropic", "hf_token", "huggingface", "api_key"]):
        hints["auth_type"] = "api_key"
    elif "jwt" in env or "jwt_secret" in env:
        hints["auth_type"] = "jwt"
    elif "session_secret" in env or "secret_key" in env:
        hints["auth_type"] = "session"

    # ── infrastructure ────────────────────────────────────────────────────────
    infra_dirs = {"terraform", "cdk", "pulumi", "iac", "infra"}
    for d in infra_dirs:
        if d in signals["root_dirs"]:
            hints["infra_tool"] = d if d not in ("iac", "infra") else "terraform"
            hints["infra_dir"]  = d
            break
    if "hcl" in [l.lower() for l in signals["languages"]]:
        hints.setdefault("infra_tool", "terraform")

    # ── deployment ────────────────────────────────────────────────────────────
    deployment = []
    if "boto3" in all_deps or "dynamodb" in all_deps or "aws_access_key" in env:
        deployment.append("aws")
    if "vercel.json" in signals["root_files"] or "vercel" in env:
        deployment.append("vercel")
    if "fly.toml" in signals["root_files"]:
        deployment.append("fly.io")
    if "railway.toml" in signals["root_files"]:
        deployment.append("railway")
    if "netlify.toml" in signals["root_files"]:
        deployment.append("netlify")
    if deployment:
        hints["deployment_target"] = deployment

    # ── docker ────────────────────────────────────────────────────────────────
    if "Dockerfile" in signals["root_files"]:
        hints["has_docker"] = True
    if "docker-compose.yml" in signals["root_files"] or "docker-compose.yaml" in signals["root_files"]:
        hints["has_docker_compose"] = True

    # ── UPGRADE 5: conflict detection ─────────────────────────────────────────
    if "flask" in all_deps and "from fastapi" in all_entry:
        conflicts.append("Flask in requirements.txt but FastAPI imported in entry point — FastAPI is likely correct")
    if "django" in all_deps and "from fastapi" in all_entry:
        conflicts.append("Django in requirements.txt but FastAPI imported in entry point — FastAPI is likely correct")
    if "react" in all_deps and "vue" in all_deps:
        conflicts.append("Both React and Vue in package.json — check which is actually used in components")

    return hints, conflicts


# ── 5. schema ─────────────────────────────────────────────────────────────────

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


# ── 6. GPT Pass 1 — deep reasoning ───────────────────────────────────────────

# UPGRADE 4 + 6: signal priority weighting in system prompt
PASS1_SYSTEM = """You are a principal software engineer doing a deep code review of an unfamiliar repository.

SIGNAL PRIORITY (most reliable → least reliable):
1. Entry point file imports and code (DEFINITIVE — if framework is imported here, it IS used)
2. Dependency files (package.json, requirements.txt) — confirms what is installed
3. Config files (vite.config.ts, jest.config.ts, etc.) — confirms tooling
4. Directory structure — confirms architecture patterns
5. README, topics, .env.example — supplementary context only

STRICT RULES:
- If a framework is imported in an entry point → treat as 100% confirmed
- If only in dependencies but not imported → treat as installed but possibly unused
- NEVER guess frameworks not explicitly visible in signals
- Prefer actual code usage over declared dependencies when they conflict
- Do NOT invent deployment targets — only state what you can see in config files or README

Your task: write a detailed technical analysis covering:
1. Primary language and all detected languages
2. Exact framework(s) — name and version if visible
3. Build tool and bundler
4. Runtime and version
5. Frontend: framework, entry point, component dir, structure
6. Backend: framework, entry point, API style, routes/endpoints visible in code
7. AI/ML layer if any (LangChain, LlamaIndex, HuggingFace, etc.)
8. Test setup: runner, config file, existing test dirs and files
9. Database: type, client library, ORM
10. Caching layer
11. Auth: JWT, sessions, API keys, OAuth — evidence from code or env
12. Infra/IaC: Terraform, CDK, Pulumi — evidence from dirs or file extensions
13. Deployment: cloud provider or platform — evidence from config files only
14. CDN and Docker
15. Recommended test types for THIS specific repo and why
16. UPGRADE 7 — Confidence per field:
    - 0.95+ = seen in entry point imports
    - 0.80-0.94 = confirmed in dependency file
    - 0.60-0.79 = inferred from config or structure
    - <0.60 = inferred from README or weak signals only

Be specific. Quote actual file names and code lines you can see."""


def build_pass1_prompt(signals, hints, conflicts):
    dep_section = "\n".join(
        f"\n### {path}\n{content}" for path, content in signals["dep_files"].items()
    ) or "None found"

    entry_section = "\n".join(
        f"\n### {path} (first 120 lines)\n{content}" for path, content in signals["entry_points"].items()
    ) or "None found"

    config_section = "\n".join(
        f"\n### {path}\n{content[:500]}" for path, content in signals["config_files"].items()
    ) or "None found"

    hints_section = "\n".join(
        f"- {k}: {v}" for k, v in hints.items()
    ) or "None"

    conflicts_section = "\n".join(
        f"- {c}" for c in conflicts
    ) or "None detected"

    return f"""Analyse repository: {REPO}

## Language breakdown (bytes)
{json.dumps(signals["languages"], indent=2)}

## Full repo file tree ({signals['total_files']} files total — truncated at {MAX_RECURSIVE_FILES})
{json.dumps(signals["all_files"][:150], indent=2)}

## Root directories
{json.dumps(signals["root_dirs"])}

## Repository topics
{json.dumps(signals["topics"])}

## Heuristic pre-detection hints (high-confidence, rule-based — trust these)
{hints_section}

## Conflicts detected (resolve these explicitly in your analysis)
{conflicts_section}

## Dependency / manifest files (FULL CONTENT — found anywhere in repo)
{dep_section}

## Application entry points (first 120 lines — HIGHEST PRIORITY SIGNAL)
{entry_section}

## Configuration files
{config_section}

## .env.example
{signals.get("env_example", "Not found")}

## Existing test files
{json.dumps(signals.get("existing_test_files", []))}

## Existing CI/CD workflows
{json.dumps(signals.get("existing_workflows", []))}

## README (first 150 lines)
{signals.get("readme_preview", "Not found")}

Provide your complete technical analysis. Follow the signal priority rules strictly."""


# ── 7. GPT Pass 2 — structured mapping ───────────────────────────────────────

PASS2_SYSTEM = """You are converting a technical analysis into a precise JSON manifest.

Source of truth priority:
1. Heuristic hints (rule-based, high confidence) — use these unless analysis contradicts
2. Entry point analysis from Pass 1
3. Dependency analysis from Pass 1
4. Everything else

Output rules:
- Return ONLY valid JSON — no markdown, no explanation, no code fences
- null for unknown strings, false for unknown booleans, [] for unknown arrays
- Every required field must be present

Field-specific rules:
- framework: the PRIMARY framework (React, Streamlit, NestJS, Django, etc.)
- backend_framework: the backend/API framework if different from framework (Flask, FastAPI, Express, aws-lambda, LangChain)
- runtime: lowercase string — "node", "python", "go", "jvm", "rust", "ruby", "php"
- api_style: "REST" | "GraphQL" | "gRPC" | "tRPC" | "none" | null
- test_runner: always "pytest" for Python if no other runner detected
- frontend_dir: always set when has_frontend is true — use actual dir name (src, pages, app, client, frontend)
- skip_e2e / skip_visual: true ONLY if has_frontend is false
- skip_performance: true if no public URL detected (local Streamlit, Gradio without hosting, etc.)
- load tests: only include in test_types_recommended if there is a running HTTP server or external API
- test_types_recommended: base = [unit, smoke, sanity, regression]. Add e2e/visual/a11y if has_frontend. Add api if REST/GraphQL/gRPC. Add load if HTTP server detected. Add performance if public URL exists.
- confidence: 0.0–1.0 reflecting your actual certainty
- confidence_notes: one sentence — what you are certain about vs uncertain about"""


def build_pass2_prompt(signals, hints, pass1_analysis):
    return f"""Convert this technical analysis into the required JSON manifest.

## Heuristic hints (rule-based, high confidence — use as anchor)
{json.dumps(hints, indent=2)}

## Technical analysis (Pass 1 output)
{pass1_analysis}

## Repository quick reference
- Repo: {REPO}
- Languages: {json.dumps(signals["languages"])}
- Root dirs: {json.dumps(signals["root_dirs"])}
- Dep files found: {list(signals["dep_files"].keys())}
- Entry points found: {list(signals["entry_points"].keys())}
- .env.example: {signals.get("env_example", "")[:200]}

## Required JSON schema (every field is required)
{json.dumps(SCHEMA, indent=2)}

Return ONLY the JSON object."""


# ── 8. GPT Pass 3 — self-validation ──────────────────────────────────────────
# UPGRADE 8: GPT reviews its own output and corrects mistakes

PASS3_SYSTEM = """You are a QA engineer reviewing a stack detection manifest for correctness.

You will receive:
1. A manifest JSON
2. The original repository signals
3. The heuristic hints

Your task: verify the manifest and return a corrected version.

Check for these common mistakes:
- test_runner is null for a Python repo → should be "pytest"
- frontend_dir is null but has_frontend is true → set it from the analysis
- backend_framework is the same as framework → backend_framework should be the API/server layer only
- load tests included for a local-only app (FAISS, SQLite, no HTTP server) → remove "load"
- skip_performance is false but no public URL exists → should be true
- api_style is null but backend_framework is FastAPI/Flask/Express → should be "REST"
- deployment_target is null but vercel.json exists → should include "vercel"
- has_docker is false but Dockerfile is in the file list → should be true
- confidence above 0.9 but several fields are null → lower the confidence

Return ONLY the corrected JSON. If everything is correct, return the same JSON unchanged."""


def build_pass3_prompt(manifest, signals, hints):
    return f"""Review and correct this manifest.

## Manifest to verify
{json.dumps(manifest, indent=2)}

## Heuristic hints (ground truth for key fields)
{json.dumps(hints, indent=2)}

## All repo files (for verification)
{json.dumps(signals["all_files"][:100])}

## Dep files found
{list(signals["dep_files"].keys())}

## Entry points found
{list(signals["entry_points"].keys())}

## .env.example
{signals.get("env_example", "")[:300]}

## Required schema (same as before)
{json.dumps(SCHEMA, indent=2)}

Return ONLY the corrected JSON manifest."""


# ── 9. three-pass GPT detection ───────────────────────────────────────────────

def detect_with_gpt(signals, hints, conflicts):
    # Pass 1 — free-form deep analysis
    print("[gpt] Pass 1 — deep reasoning ...")
    p1 = client.chat.completions.create(
        model="gpt-4o",
        temperature=0,
        messages=[
            {"role": "system", "content": PASS1_SYSTEM},
            {"role": "user",   "content": build_pass1_prompt(signals, hints, conflicts)},
        ],
    )
    pass1_analysis = p1.choices[0].message.content
    print(f"[gpt] Pass 1 complete ✓ ({len(pass1_analysis)} chars)")

    # Pass 2 — structured mapping
    print("[gpt] Pass 2 — structured mapping ...")
    p2 = client.chat.completions.create(
        model="gpt-4o",
        response_format={"type": "json_object"},
        temperature=0,
        messages=[
            {"role": "system", "content": PASS2_SYSTEM},
            {"role": "user",   "content": build_pass2_prompt(signals, hints, pass1_analysis)},
        ],
    )
    manifest = json.loads(p2.choices[0].message.content)
    print("[gpt] Pass 2 complete ✓")

    # Pass 3 — self-validation and correction
    print("[gpt] Pass 3 — self-validation ...")
    p3 = client.chat.completions.create(
        model="gpt-4o",
        response_format={"type": "json_object"},
        temperature=0,
        messages=[
            {"role": "system", "content": PASS3_SYSTEM},
            {"role": "user",   "content": build_pass3_prompt(manifest, signals, hints)},
        ],
    )
    corrected_manifest = json.loads(p3.choices[0].message.content)
    print("[gpt] Pass 3 complete ✓")

    return corrected_manifest, pass1_analysis


# ── 10. validate + enrich (Python — zero detection logic) ────────────────────

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


def validate_and_enrich(raw, signals, hints, pass1_analysis):
    from jsonschema import validate, ValidationError

    # Apply heuristic hints as safety net for critical fields
    # Only fills nulls — never overwrites GPT's confident detections
    for key, value in hints.items():
        if key in SAFE_DEFAULTS and raw.get(key) in (None, [], False, "unknown"):
            raw[key] = value

    # Fill any remaining missing fields with safe defaults
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

    # Metadata
    raw["repo"]           = REPO
    raw["detected_at"]    = datetime.datetime.utcnow().isoformat() + "Z"
    raw["sha"]            = os.environ.get("GITHUB_SHA", "unknown")
    raw["pass1_analysis"] = pass1_analysis
    raw["hints_used"]     = hints

    # Clamp confidence
    try:
        raw["confidence"] = round(max(0.0, min(1.0, float(raw["confidence"]))), 2)
    except (TypeError, ValueError):
        raw["confidence"] = 0.0

    return raw


# ── 11. main ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"[detect_stack] ── Analysing {REPO} ──")

    # Step 1: collect all signals (recursive)
    signals = collect_signals()

    # Step 2: heuristic pre-detection
    hints, conflicts = heuristic_detection(signals)
    print(f"[detect_stack] Hints    : {hints}")
    print(f"[detect_stack] Conflicts: {conflicts}")

    # Step 3: three-pass GPT detection
    raw_manifest, pass1_analysis = detect_with_gpt(signals, hints, conflicts)

    # Step 4: validate + enrich (Python fills gaps, never overrides)
    manifest = validate_and_enrich(raw_manifest, signals, hints, pass1_analysis)

    print(f"[detect_stack] Confidence : {manifest['confidence']}")
    print(f"[detect_stack] Notes      : {manifest['confidence_notes']}")

    # Write clean manifest (for downstream pipelines)
    clean = {k: v for k, v in manifest.items() if k not in ("pass1_analysis", "hints_used")}
    with open("manifest.json", "w") as f:
        json.dump(clean, f, indent=2)

    # Write debug manifest (includes pass1 analysis + hints for troubleshooting)
    with open("manifest_debug.json", "w") as f:
        json.dump(manifest, f, indent=2)

    print("[detect_stack] manifest.json written ✓")
    print("[detect_stack] manifest_debug.json written ✓")
    print(json.dumps(clean, indent=2))