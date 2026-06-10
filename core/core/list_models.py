from .gemini_model_registry import AntigravityModelRegistry
import os

# Ensure the API key is set if needed for the registry to work
# Assuming it's already in the environment based on previous turns

reg = AntigravityModelRegistry()
catalog = reg.get_catalog(force_refresh=True)
print("All Models:", catalog.all_models)
