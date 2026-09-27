import urllib.request
import time
import os

urls = [
    "https://nx-executor.onrender.com",
    "https://librechat-rag-api-jhvl.onrender.com",
    "https://librechat-xqjg.onrender.com"
]

interval = int(os.environ.get("KEEPALIVE_INTERVAL_SECONDS", "600"))

def ping_once():
    for url in urls:
        try:
            urllib.request.urlopen(url, timeout=10)
            print(f"OK: {url}")
        except Exception as e:
            print(f"FAIL: {url} - {e}")

if __name__ == "__main__":
    # Run once per interval to keep services warm continuously
    while True:
        ping_once()
        time.sleep(interval)
