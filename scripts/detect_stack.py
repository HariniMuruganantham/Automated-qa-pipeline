import os
import json
import base64
import datetime
import requests
from openai import OpenAI
from jsonschema import validate, ValidationError

# ── ENV ──────────────────────────────────────────────────────────────────────
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
REPO         = os.environ["TARGET_REPO"]
OPENAI_KEY   = os.environ["OPENAI_API_KEY"]

GH = "https://api.github.com"

HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
}

# ── HELPERS ──────────────────────────────────────────────────────────────────
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


# ── 1. SIGNAL COLLECTION ─────────────────────────────────────────────────────
def collect_signals():
    signals = {}

    signals["languages"] = gh_get(f"/repos/{REPO}/languages") or {}

    root_entries = get_dir_listing("")
    signals["root_files"] = [e["name"] for e in root_entries if e["type"] == "file"]
    signals["root_dirs"]  = [e["name"] for e in root_entries if e["type"] == "dir"]

    signals["topics"] = (gh_get(f"/repos/{REPO}/topics") or {}).get("names", [])

    # Dependency detection
    dep_files = ["package.json", "requirements.txt", "pyproject.toml"]
    for f in dep_files:
        content = get_file(f)
        if content:
            signals["dep_file"] = {"name": f, "content": content[:5000]}
            break

    # Backend detection
    if "backend" in signals["root_dirs"]:
        backend_files = get_dir_listing("backend")
        signals["backend_files"] = [e["name"] for e in backend_files]

        for f in ["requirements.txt", "package.json"]:
            content = get_file(f"backend/{f}")
            if content:
                signals["backend_dep_file"] = {"name": f, "content": content[:3000]}

    # src files
    if "src" in signals["root_dirs"]:
        signals["src_files"] = [e["name"] for e in get_dir_listing("src")]
    else:
        signals["src_files"] = []

    # Infra detection
    for d in ["terraform", "infra", "cdk"]:
        if d in signals["root_dirs"]:
            signals["infra_dir_found"] = d

    # Config files
    CONFIGS = [
        "vite.config.ts", "vite.config.js",
        "jest.config.js", "vitest.config.ts",
        "next.config.js", "angular.json",
        "Dockerfile", "docker-compose.yml",
        "vercel.json"
    ]

    signals["config_files_present"] = [
        f for f in CONFIGS if f in signals["root_files"]
    ]

    # README
    readme = get_file("README.md") or ""
    signals["readme_preview"] = "\n".join(readme.splitlines()[:100])

    return signals


# ── 2. GPT CALL ──────────────────────────────────────────────────────────────
def call_gpt(signals):
    client = OpenAI(api_key=OPENAI_KEY)

    response = client.chat.completions.create(
        model="gpt-4o",
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": "Return JSON only."},
            {"role": "user", "content": json.dumps(signals)},
        ],
    )

    return json.loads(response.choices[0].message.content)


# ── 3. PRODUCTION INFERENCE ENGINE ───────────────────────────────────────────
def post_process(raw, signals):
    dep_content  = signals.get("dep_file", {}).get("content", "").lower()
    backend_dep  = signals.get("backend_dep_file", {}).get("content", "").lower()
    readme       = signals.get("readme_preview", "").lower()
    root_files   = signals.get("root_files", [])
    root_dirs    = signals.get("root_dirs", [])
    src_files    = signals.get("src_files", [])
    config_files = signals.get("config_files_present", [])
    languages    = signals.get("languages", {})

    top_lang = max(languages, key=languages.get, default="").lower()

    # ── RUNTIME ────────────────────────────────────────────────────────────
    runtime_map = {
        "python": "python",
        "typescript": "node",
        "javascript": "node",
    }
    raw["runtime"] = raw.get("runtime") or runtime_map.get(top_lang)

    # ── FRONTEND ───────────────────────────────────────────────────────────
    if any(f.lower().endswith((".tsx", ".jsx")) for f in src_files):
        raw["frontend_framework"] = "react"
        raw["has_frontend"] = True

    if "package.json" in root_files:
        if "react" in dep_content:
            raw["frontend_framework"] = "react"
            raw["has_frontend"] = True

    # ── BACKEND ────────────────────────────────────────────────────────────
    if top_lang == "python":
        raw["backend_runtime"] = "python"

        combined = dep_content + backend_dep + readme

        if "django" in combined:
            raw["backend_framework"] = "django"
        elif "fastapi" in combined:
            raw["backend_framework"] = "fastapi"
        elif "flask" in combined:
            raw["backend_framework"] = "flask"
        elif "boto3" in combined:
            raw["backend_framework"] = "aws-lambda"

        raw["backend_dir"] = "backend" if "backend" in root_dirs else "root"

    # ── API STYLE ──────────────────────────────────────────────────────────
    if raw.get("backend_framework"):
        raw["api_style"] = "REST"

    # ── INFRA ──────────────────────────────────────────────────────────────
    if "terraform" in root_dirs:
        raw["infra_tool"] = "terraform"
        raw["infra_dir"] = "terraform"

    if "aws" in readme:
        raw["deployment_target"] = ["aws"]

    # ── DATABASE ───────────────────────────────────────────────────────────
    if "dynamodb" in readme:
        raw["database"] = "DynamoDB"

    # ── TEST RUNNER ────────────────────────────────────────────────────────
    if "pytest" in dep_content:
        raw["test_runner"] = "pytest"

    # ── AI DETECTION ───────────────────────────────────────────────────────
    if any(k in readme for k in ["rag", "faiss", "llm"]):
        raw["test_types_recommended"] = [
            "unit", "smoke", "regression",
            "model", "data", "prompt"
        ]

    # ── NORMALIZATION ──────────────────────────────────────────────────────
    if not raw.get("deployment_target"):
        raw["deployment_target"] = None

    return raw


# ── 4. VALIDATION ────────────────────────────────────────────────────────────
def validate_and_enrich(raw):
    raw["detected_at"] = datetime.datetime.utcnow().isoformat() + "Z"
    return raw


# ── MAIN ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"[detect] {REPO}")

    signals = collect_signals()
    raw     = call_gpt(signals)

    raw     = post_process(raw, signals)
    result  = validate_and_enrich(raw)

    with open("manifest.json", "w") as f:
        json.dump(result, f, indent=2)

    print(json.dumps(result, indent=2))