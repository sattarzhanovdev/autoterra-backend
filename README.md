# AutoTerra Django API

Local development backend for the Flutter app.

## Run

```bash
cd backend
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
# edit .env and set HF_API_TOKEN
.venv/bin/python manage.py runserver 127.0.0.1:8000
```

Optional AI settings:

- `HF_API_TOKEN=hf_your_token_here`
- `HF_CHAT_MODEL=openai/gpt-oss-120b`
- `HF_CHAT_MODEL=openai/gpt-oss-120b:fireworks-ai` to pin a Hugging Face provider
- `HF_CHAT_URL=https://router.huggingface.co/v1/chat/completions`

## Endpoints

- `GET /api/health/`
- `POST /api/login/`
- `GET /api/auth/me/`
- `GET /api/dashboard/`
- `GET /api/stores/`
- `GET /api/orders/`
- `POST /api/orders/create/`
- `POST /api/ai/chat/`

## Demo Client Login

- Phone: `+7 (916) 123-45-67`
- Password: `123456`

The Flutter app uses `http://127.0.0.1:8000/api` by default. Override it with:

```bash
flutter run --dart-define=API_BASE_URL=http://127.0.0.1:8000/api
```
