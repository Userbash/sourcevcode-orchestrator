import subprocess
from mimo_parser import parse_mimo_models_output

def get_models() -> list[dict]:
    try:
        output = subprocess.check_output(["mimo", "models", "--verbose"]).decode('utf-8')
        return parse_mimo_models_output(output)
    except Exception as e:
        print(f"Error fetching models: {e}")
        return []

if __name__ == "__main__":
    models = get_models()
    for m in models:
        print(f"ID: {m['full_id']}, Status: {m['status']}, Context Window: {m['context_window']}")
