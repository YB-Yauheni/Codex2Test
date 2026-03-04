# Telegram bot: отслеживание транзакций по контракту

Бот принимает адрес контракта и сеть (`BSC`, `SOL`, `ETH`, `ARB`, `BASE`), получает последние транзакции токена и присылает:
- адрес получателя,
- количество токенов,
- примерную стоимость в USD по текущей цене.

## Как работает
1. Пользователь запускает `/start`.
2. Отправляет контракт токена.
3. Выбирает сеть кнопкой.
4. Бот получает последние переводы:
   - EVM-сети: через API блокчейн-обозревателей (Etherscan/BscScan/Arbiscan/BaseScan).
   - Solana: через публичный Solana RPC.
5. Цена берётся из Dexscreener и умножается на объём перевода.

## Запуск
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# заполните .env
python bot.py
```

## Переменные окружения
- `TELEGRAM_BOT_TOKEN`
- `ETHERSCAN_API_KEY`
- `BSCSCAN_API_KEY`
- `ARBISCAN_API_KEY`
- `BASESCAN_API_KEY`

> Для Solana отдельный API key не нужен, используется публичный RPC.
