import requests
import json

def test_http():
    url = "http://localhost:8000/chat"
    payload = {
        "user_id": "test_user",
        "message": "tell me a 5 word joke",
        "session_id": "test_session",
        "source": "cli_test",
        "context": {"project": "wisper"}
    }
    print(f"Sending POST to {url}...")
    try:
        response = requests.post(url, json=payload, timeout=60)
        print(f"Response ({response.status_code}): {response.text}")
    except Exception as e:
        print(f"Failed: {e}")

if __name__ == "__main__":
    test_http()
