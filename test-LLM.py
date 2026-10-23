import requests

# $env:LINEAGE_LLM_ENDPOINT = "https://llm.maqsoftware.net/v1/chat/completions"
# $env:LINEAGE_LLM_API_KEY = "sk-V9LRZNgLYCM2LPR1mlrqOA"
# $env:LINEAGE_LLM_MODEL = "qwen-3.8-27b"

LLM_HOST_URL = "https://llm.maqsoftware.net/"
LLM_API_KEY = "sk-V9LRZNgLYCM2LPR1mlrqOA"
LLM_MODEL = "qwen-3.8-27b"

url = "https://llm.maqsoftware.net/v1/chat/completions"

headers = {
    "Authorization": f"Bearer {LLM_API_KEY}",
    "Content-Type": "application/json"
}

payload = {
    "model": LLM_MODEL,
    "messages": [
        {"role": "user", "content": "Say hello in one sentence."}
    ],
    "max_tokens": 50
}

try:
    response = requests.post(
        url,
        headers=headers,
        json=payload,
        timeout=30
    )

    response.raise_for_status()
    result = response.json()

    answer = result["choices"][0]["message"]["content"]

    if answer:
        print("SUCCESS: LLM returned a response.")
        print("Response:", answer)
    else:
        print("The LLM returned an empty response.")

except requests.exceptions.Timeout:
    print("FAILED: Request timed out.")

except requests.exceptions.HTTPError as e:
    print(f"FAILED: HTTP error: {e}")
    print("Details:", response.text)

except Exception as e:
    print(f"FAILED: {type(e).__name__}: {e}")