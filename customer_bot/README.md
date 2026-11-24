# طلباتي نمبر — بوت العملاء

Customer-facing Telegram bot for browsing published services, submitting orders for admin review, and tracking submitted requests.

## Railway variables
- `TELEGRAM_BOT_TOKEN`: token for @TalabatiNumberBot (set only on this service)
- `DATABASE_URL`: Railway reference to the shared PostgreSQL service
- `ADMIN_IDS`: comma-separated Telegram admin IDs

The bot creates `provider_services` and `customer_orders` tables in PostgreSQL. Orders are stored as `pending_review`; this version does not automatically charge users or submit provider orders until the provider API schema and payment flow are configured.

Start command: `python main.py`
