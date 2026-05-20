# AutoTerra Django API

Local development backend for the Flutter app.

## Run

```bash
cd backend
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python manage.py runserver 127.0.0.1:8000
```

## Endpoints

- `GET /api/health/`
- `POST /api/login/`
- `GET /api/auth/me/`
- `GET /api/dashboard/`
- `GET /api/stores/`
- `GET /api/orders/`
- `POST /api/orders/create/`

## Demo Client Login

- Phone: `+7 (916) 123-45-67`
- Password: `123456`

The Flutter app uses `http://127.0.0.1:8000/api` by default. Override it with:

```bash
flutter run --dart-define=API_BASE_URL=http://127.0.0.1:8000/api
```
