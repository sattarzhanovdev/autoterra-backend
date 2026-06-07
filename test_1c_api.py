import os
import json
import requests

# Config
BASE_URL = "http://localhost:8000/api"
TOKEN = "f4a8c9b2d1e"

def test_stock_update(payload, dry_run=False):
    url = f"{BASE_URL}/integration/erp/stock-update/"
    if dry_run:
        url += "?dry_run=true"
    
    headers = {
        "X-Integration-Token": TOKEN,
        "Content-Type": "application/json"
    }
    
    print(f"\n--- Testing {'DRY RUN' if dry_run else 'LIVE'} update ---")
    
    try:
        response = requests.post(url, headers=headers, json=payload)
        print(f"Status Code: {response.status_code}")
        print(f"Response: {json.dumps(response.json(), indent=2, ensure_ascii=False)}")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    # Test Payload - using SKUs that exist in the seeded DB (e.g. LAK-3-01)
    
    # 1. Successful Dry Run (Valid and Invalid SKUs)
    test_stock_update([
        {"sku": "LAK-3-01", "quantity": 99, "price": 2600}, # Valid
        {"sku": "NON-EXISTENT", "quantity": 10}             # Invalid
    ], dry_run=True)

    # 2. Live Update (Partial Success)
    test_stock_update([
        {"sku": "GRN-3-02", "quantity": 25},                # Valid
        {"sku": "WRONG-SKU", "quantity": 5, "price": 100}   # Invalid
    ], dry_run=False)

    # 3. Missing Fields
    test_stock_update([
        {"sku": "LAK-3-01"} # Missing quantity
    ], dry_run=True)
