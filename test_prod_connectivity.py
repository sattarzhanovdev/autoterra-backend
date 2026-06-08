import requests

def test_production_api():
    base_url = "http://sigmaadil.pythonanywhere.com/api"
    print(f"Testing connectivity to: {base_url}")
    
    try:
        # Testing health or docs endpoint which are usually public
        endpoints = ["/health/", "/docs/", "/regions/"]
        for ep in endpoints:
            url = f"{base_url}{ep}"
            print(f"\nChecking {url}...")
            resp = requests.get(url, timeout=10)
            print(f"Status: {resp.status_code}")
            if resp.status_code == 200:
                print("Content: OK (first 100 chars):", resp.text[:100])
            else:
                print(f"Server responded with error: {resp.text[:100]}")
                
    except Exception as e:
        print(f"CRITICAL: Could not connect to production server. Error: {e}")

if __name__ == "__main__":
    test_production_api()
