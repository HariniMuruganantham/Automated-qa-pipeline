import os
import json
import base64
import datetime
import requests
from openai import OpenAI
from jsonschema import validate

# ── ENV ───────────────────────────────────────────────────────────────
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
REPO         = os.environ["TARGET_REPO"]
OPENAI_KEY   = os.environ["OPENAI_API_KEY"]

GH = "https://api.github.com"

HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
}

# ── TECH CLASSIFIER (CORE ENGINE) ─────────────────────────────────────
TECH_CLASSIFIER = {
    # Backend frameworks
    "django": "backend_framework",
    "fastapi": "backend_framework",
    "flask": "backend_framework",
    "express": "backend_framework",
    "nestjs": "backend_framework",

    # Frontend frameworks
    "react": "frontend_framework",
    "vue": "frontend_framework",
    "angular": "frontend_framework",

    # UI runtimes
    "streamlit": "ui_runtime",

    # AI / ML
    "langchain": "ai_orchestration",
    "transformers": "ai_model",
    "huggingface": "ai_model",

    # Vector DB
    "faiss": "vector_store",
    "pinecone": "vector_store",
    "weaviate": "vector_store",

    # Databases
    "postgres": "database",
    "mongodb": "database",
    "dynamodb": "database",

    # Cache
    "redis": "cache",

    # Infra
    "terraform": "infra_tool",
}

# ── HELPERS ───────────────────────────────────────────────────────────
def gh_get(path):
    r = requests.get(f"{GH}{path}", headers=HEADERS)
    return r.json() if r.status_code == 200 else None


def get_file(path):
    data = gh_get(f"/repos/{REPO}/contents/{path}")
    if data and isinstance(data, dict) and "content" in data:
        return base64.b64decode(data["content"]).decode(errors="ignore")
    return ""


def get_dir(path=""):
    data = gh_get(f"/repos/{REPO}/contents/{path}")
    if isinstance(data, list):
        return [f["name"] for f in data]
    return []


# ── SIGNAL COLLECTION ─────────────────────────────────────────────────
def collect_signals():
    signals = {}

    signals["languages"] = gh_get(f"/repos/{REPO}/languages") or {}
    signals["root_files"] = get_dir("")
    signals["root_dirs"]  = [
        f for f in signals["root_files"]
        if gh_get(f"/repos/{REPO}/contents/{f}") and
           isinstance(gh_get(f"/repos/{REPO}/contents/{f}"), list)
    ]

    signals["readme"] = get_file("README.md")[:5000]

    dep = get_file("package.json") or get_file("requirements.txt")
    signals["deps"] = dep.lower()

    signals["src_files"] = get_dir("src") if "src" in signals["root_dirs"] else []

    return signals


# ── GPT (OPTIONAL SUPPORT) ────────────────────────────────────────────
def call_gpt(signals):
    client = OpenAI(api_key=OPENAI_KEY)

    res = client.chat.completions.create(
        model="gpt-4o",
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": "Return JSON only"},
            {"role": "user", "content": json.dumps(signals)},
        ],
    )

    return json.loads(res.choices[0].message.content)


# ── CLASSIFICATION ENGINE ─────────────────────────────────────────────
def classify_technologies(signals):
    detected = {}

    text = (
        signals.get("deps", "") +
        signals.get("readme", "").lower()
    )

    for tech, role in TECH_CLASSIFIER.items():
        if tech in text:
            detected.setdefault(role, []).append(tech)

    return detected


# ── MAIN INFERENCE ENGINE ─────────────────────────────────────────────
def post_process(raw, signals):
    detected = classify_technologies(signals)
    readme   = signals.get("readme", "").lower()
    languages = signals.get("languages", {})

    top_lang = max(languages, key=languages.get, default="unknown").lower()

    # ── RUNTIME ───────────────────────────────────────────────────────
    runtime_map = {
        "python": "python",
        "typescript": "node",
        "javascript": "node",
    }
    raw["runtime"] = runtime_map.get(top_lang)

    # ── FRONTEND ──────────────────────────────────────────────────────
    if "frontend_framework" in detected:
        raw["frontend_framework"] = detected["frontend_framework"][0]
        raw["has_frontend"] = True

    if "ui_runtime" in detected:
        raw["frontend_framework"] = detected["ui_runtime"][0] + "-ui"
        raw["has_frontend"] = True

    # ── BACKEND ───────────────────────────────────────────────────────
    if "backend_framework" in detected:
        raw["backend_framework"] = detected["backend_framework"][0]
        raw["backend_runtime"] = raw["runtime"]

    # ── AI LAYER ──────────────────────────────────────────────────────
    if "ai_orchestration" in detected:
        raw["ai_layer"] = detected["ai_orchestration"][0]

    # ── VECTOR STORE ──────────────────────────────────────────────────
    if "vector_store" in detected:
        raw["vector_store"] = detected["vector_store"][0]
        raw["database"] = None

    # ── DATABASE ──────────────────────────────────────────────────────
    if "database" in detected:
        raw["database"] = detected["database"][0]

    # ── INFRA ─────────────────────────────────────────────────────────
    if "infra_tool" in detected:
        raw["infra_tool"] = detected["infra_tool"][0]

    if "aws" in readme:
        raw["deployment_target"] = ["aws"]

    # ── API STYLE ─────────────────────────────────────────────────────
    if raw.get("backend_framework"):
        raw["api_style"] = "REST"

    # ── TEST STRATEGY ─────────────────────────────────────────────────
    tests = ["unit", "smoke", "sanity", "regression"]

    if raw.get("has_frontend"):
        tests += ["e2e", "visual", "a11y"]

    if raw.get("backend_framework"):
        tests.append("api")

    if raw.get("database"):
        tests.append("load")

    if raw.get("vector_store"):
        tests += ["model", "data", "prompt", "evaluation"]

    raw["test_types_recommended"] = list(set(tests))

    # ── NORMALIZATION ─────────────────────────────────────────────────
    if not raw.get("deployment_target"):
        raw["deployment_target"] = None

    return raw


# ── MAIN ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"[detect] {REPO}")

    signals = collect_signals()
    raw     = call_gpt(signals)

    result  = post_process(raw, signals)

    result["detected_at"] = datetime.datetime.utcnow().isoformat() + "Z"

    with open("manifest.json", "w") as f:
        json.dump(result, f, indent=2)

    print(json.dumps(result, indent=2))