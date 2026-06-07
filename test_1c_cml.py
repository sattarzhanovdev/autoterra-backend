import requests
import os

BASE_URL = "http://localhost:8001/api/integration/1c/exchange/"
TOKEN = "f4a8c9b2d1e" # Same token as before

def test_cml_flow():
    # 1. Check Auth
    print("\n[1/4] Testing checkauth...")
    resp = requests.get(f"{BASE_URL}?mode=checkauth&token={TOKEN}")
    print(f"Response: {resp.text.strip()}")
    if "success" not in resp.text:
        print("Auth failed!")
        return

    # 2. Init
    print("\n[2/4] Testing init...")
    resp = requests.get(f"{BASE_URL}?mode=init&token={TOKEN}")
    print(f"Response: {resp.text.strip()}")

    # 3. File Upload (import.xml)
    print("\n[3/4] Testing file upload (import.xml)...")
    import_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <КоммерческаяИнформация ВерсияСхемы="2.09" ДатаФормирования="2026-06-07">
        <Каталог>
            <Ид>1</Ид>
            <Наименование>Основной каталог</Наименование>
            <Товары>
                <Товар>
                    <Ид>CML-PROD-001</Ид>
                    <Наименование>Краска Тестовая 1С</Наименование>
                    <Артикул>TEST-CML-01</Артикул>
                </Товар>
            </Товары>
        </Каталог>
    </КоммерческаяИнформация>
    """
    resp = requests.post(
        f"{BASE_URL}?mode=file&filename=import.xml&token={TOKEN}",
        data=import_xml.encode('utf-8')
    )
    print(f"Upload Result: {resp.text.strip()}")

    # 4. Import (Process file)
    print("\n[4/4] Testing import processing...")
    resp = requests.get(f"{BASE_URL}?mode=import&filename=import.xml&token={TOKEN}")
    print(f"Process Result: {resp.text.strip()}")

    # 5. Stock Update (offers.xml)
    print("\n[Bonus] Testing stock upload (offers.xml)...")
    offers_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <КоммерческаяИнформация ВерсияСхемы="2.09" ДатаФормирования="2026-06-07">
        <ПакетПредложений>
            <Предложения>
                <Предложение>
                    <Ид>CML-PROD-001</Ид>
                    <Количество>42</Количество>
                    <Цены>
                        <Цена>
                            <ЦенаЗаЕдиницу>1250.50</ЦенаЗаЕдиницу>
                        </Цена>
                    </Цены>
                </Предложение>
            </Предложения>
        </ПакетПредложений>
    </КоммерческаяИнформация>
    """
    requests.post(f"{BASE_URL}?mode=file&filename=offers.xml&token={TOKEN}", data=offers_xml.encode('utf-8'))
    resp = requests.get(f"{BASE_URL}?mode=import&filename=offers.xml&token={TOKEN}")
    print(f"Offers Process Result: {resp.text.strip()}")

if __name__ == "__main__":
    test_cml_flow()
