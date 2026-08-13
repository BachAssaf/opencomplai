"""Config and endpoint hint extraction."""

from __future__ import annotations

import re
from pathlib import Path

from opencomplai_core.scanner.feature_types import ConfigRef
from opencomplai_core.scanner.inventory import RepoInventory

CONFIG_PATTERNS = [
    (re.compile(r"OPENAI_API_KEY", re.I), "config_key"),
    (re.compile(r"ANTHROPIC_API_KEY", re.I), "config_key"),
    (re.compile(r"GEMINI_API_KEY", re.I), "config_key"),
    (re.compile(r"GOOGLE_API_KEY", re.I), "config_key"),
    (re.compile(r"AZURE_OPENAI_ENDPOINT", re.I), "config_key"),
    (re.compile(r"BEDROCK", re.I), "config_key"),
    (re.compile(r"VERTEX_AI", re.I), "config_key"),
    (re.compile(r"HF_TOKEN", re.I), "config_key"),
    (re.compile(r"api\.openai\.com", re.I), "endpoint"),
    (re.compile(r"api\.anthropic\.com", re.I), "endpoint"),
    (re.compile(r"bedrock-runtime", re.I), "endpoint"),
    (re.compile(r"aiplatform\.googleapis\.com", re.I), "endpoint"),
    (re.compile(r"generativelanguage\.googleapis\.com", re.I), "endpoint"),
    (re.compile(r"generateContent", re.I), "endpoint"),
    (re.compile(r"gemini-[\d]", re.I), "endpoint"),
    (re.compile(r"localhost:11434", re.I), "endpoint"),
    # SCAN-COVERAGE: the list above covered 7 of the 22 providers in
    # ai_signals.json's `ai_sdks`, so a service using Cohere, Mistral, Groq or
    # any of the rest produced no config evidence at all. Each entry below is
    # the provider's documented credential env var and its API host.
    (re.compile(r"CO_API_KEY|COHERE_API_KEY", re.I), "config_key"),
    (re.compile(r"MISTRAL_API_KEY", re.I), "config_key"),
    (re.compile(r"GROQ_API_KEY", re.I), "config_key"),
    (re.compile(r"TOGETHER_API_KEY", re.I), "config_key"),
    (re.compile(r"REPLICATE_API_TOKEN", re.I), "config_key"),
    (re.compile(r"FIREWORKS_API_KEY", re.I), "config_key"),
    (re.compile(r"PERPLEXITY_API_KEY", re.I), "config_key"),
    (re.compile(r"AI21_API_KEY", re.I), "config_key"),
    (re.compile(r"STABILITY_(?:API_)?KEY", re.I), "config_key"),
    (re.compile(r"ELEVEN(?:_?LABS)?_API_KEY", re.I), "config_key"),
    (re.compile(r"DEEPGRAM_API_KEY", re.I), "config_key"),
    (re.compile(r"ASSEMBLYAI_API_KEY", re.I), "config_key"),
    (re.compile(r"WATSONX_API_KEY|WATSONX_APIKEY", re.I), "config_key"),
    (re.compile(r"HUGGINGFACE(?:HUB)?_API_(?:KEY|TOKEN)", re.I), "config_key"),
    (re.compile(r"AZURE_OPENAI_API_KEY", re.I), "config_key"),
    (re.compile(r"api\.cohere\.(?:ai|com)", re.I), "endpoint"),
    (re.compile(r"api\.mistral\.ai", re.I), "endpoint"),
    (re.compile(r"api\.groq\.com", re.I), "endpoint"),
    (re.compile(r"api\.together\.(?:ai|xyz)", re.I), "endpoint"),
    (re.compile(r"api\.replicate\.com", re.I), "endpoint"),
    (re.compile(r"api\.fireworks\.ai", re.I), "endpoint"),
    (re.compile(r"api\.perplexity\.ai", re.I), "endpoint"),
    (re.compile(r"api\.ai21\.com", re.I), "endpoint"),
    (re.compile(r"api\.stability\.ai", re.I), "endpoint"),
    (re.compile(r"api\.elevenlabs\.io", re.I), "endpoint"),
    (re.compile(r"api\.deepgram\.com", re.I), "endpoint"),
    (re.compile(r"api\.assemblyai\.com", re.I), "endpoint"),
    (re.compile(r"api-inference\.huggingface\.co", re.I), "endpoint"),
    (re.compile(r"\.openai\.azure\.com", re.I), "endpoint"),
    (re.compile(r"ml\.cloud\.ibm\.com", re.I), "endpoint"),
]

TEXT_EXTENSIONS = {
    ".py",
    ".yaml",
    ".yml",
    ".json",
    ".toml",
    ".txt",
    ".md",
    ".ts",
    ".js",
    ".tsx",
    ".jsx",
    ".env",
    ".properties",
}
ENV_FILENAMES = {".env", ".env.local", ".env.example"}


def extract_config_features(inventory: RepoInventory) -> list[ConfigRef]:
    results: list[ConfigRef] = []
    for entry in inventory.entries:
        if entry.is_binary:
            continue
        suffix = Path(entry.rel_path).suffix.lower()
        basename = Path(entry.rel_path).name.lower()
        if suffix not in TEXT_EXTENSIONS and basename not in ENV_FILENAMES:
            continue
        try:
            text = Path(entry.path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            for pattern, kind in CONFIG_PATTERNS:
                match = pattern.search(line)
                if match:
                    results.append(
                        ConfigRef(
                            key=match.group(0).lower(),
                            location=f"{entry.rel_path}:{i}",
                            scope=entry.scope,
                            kind=kind,
                        )
                    )
    return results
