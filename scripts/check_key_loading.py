import os

from core.core.env_loader import load_env_file
from core.core.provider_credentials import credential_snapshot

load_env_file(".env")
load_env_file(".env.bridge", override=True)
load_env_file(".env.gemini.local", override=True)

for label, env_names in {
    "openai": ("OPENAI_API_KEY",),
    "mistral": ("MISTRAL_API_KEY",),
    "antigravity": ("ANTIGRAVITY_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "github": ("GITHUB_API", "GITHUB_API_KEY", "GITHUB_TOKEN", "GH_TOKEN", "HOST_BRIDGE_GH_TOKEN"),
}.items():
    snapshot = credential_snapshot(env_names)
    print(f"{label}: configured={snapshot['configured']} usable={snapshot['usable']} placeholder={snapshot['placeholder']} env={snapshot['env_var']} preview={snapshot['preview']}")
