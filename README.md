# RedX Numbers Safe — API Provider Manager

Standalone admin-only Telegram bot for storing and testing generic HTTPS API providers. The original RedX Numbers source is not modified or copied here.

## Features
- Admin-only menu to add, list, and test API providers.
- Bearer, X-API-Key, and custom authentication headers.
- Configurable GET paths for services and balance.
- API keys encrypted at rest using Fernet.
- Rejects HTTP, localhost/private IP destinations, and redirects.
- No Telegram login-code collection, OTP retrieval, account-session creation, or authorization management.

## Setup
1. Python 3.11+
2. `pip install -r requirements.txt`
3. Set `TELEGRAM_BOT_TOKEN` to a NEW bot token from BotFather.
4. Set `ADMIN_IDS` to your numeric Telegram user ID(s), comma-separated.
5. Generate a key: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
6. Set it as `CONFIG_ENCRYPTION_KEY`.
7. Run `python main.py`.

On Railway, set the environment variables in the service Variables tab. Attach a persistent volume for `redx_providers.db`.

## Scope
This is a separate API-management bot, not yet integrated with customer checkout/order fulfillment. It only tests configured GET endpoints and does not retrieve login codes or create account sessions.
