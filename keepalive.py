import urllib.request

urls = [
    "https://nx-executor.onrender.com",
    "https://librechat-rag-api-jhvl.onrender.com",
    "https://librechat-xqjg.onrender.com"
]

for url in urls:
    try:
        urllib.request.urlopen(url, timeout=10)
        print(f"OK: {url}")
    except Exception as e:
        print(f"FAIL: {url} - {e}")
