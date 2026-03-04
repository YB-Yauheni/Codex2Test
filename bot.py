import asyncio
import logging
import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import aiohttp
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

WAITING_CONTRACT, WAITING_NETWORK = range(2)


@dataclass
class EvmNetwork:
    key: str
    name: str
    api_url: str
    api_key_env: str
    chain_id: str


EVM_NETWORKS = {
    "ETH": EvmNetwork("ETH", "Ethereum", "https://api.etherscan.io/api", "ETHERSCAN_API_KEY", "ethereum"),
    "BSC": EvmNetwork("BSC", "BNB Smart Chain", "https://api.bscscan.com/api", "BSCSCAN_API_KEY", "bsc"),
    "ARB": EvmNetwork("ARB", "Arbitrum", "https://api.arbiscan.io/api", "ARBISCAN_API_KEY", "arbitrum"),
    "BASE": EvmNetwork("BASE", "Base", "https://api.basescan.org/api", "BASESCAN_API_KEY", "base"),
}
SOLANA_RPC = "https://api.mainnet-beta.solana.com"


def networks_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("BSC", callback_data="network:BSC"), InlineKeyboardButton("SOL", callback_data="network:SOL")],
        [InlineKeyboardButton("ETH", callback_data="network:ETH"), InlineKeyboardButton("ARB", callback_data="network:ARB")],
        [InlineKeyboardButton("BASE", callback_data="network:BASE")],
    ]
    return InlineKeyboardMarkup(rows)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text(
        "Отправь адрес контракта токена, а потом выбери сеть (BSC, SOL, ETH, ARB, BASE)."
    )
    return WAITING_CONTRACT


async def receive_contract(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    contract = update.message.text.strip()
    context.user_data["contract"] = contract
    await update.message.reply_text("Теперь выбери сеть:", reply_markup=networks_keyboard())
    return WAITING_NETWORK


async def receive_network(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    selected = query.data.split(":", maxsplit=1)[1]
    contract = context.user_data.get("contract")

    if not contract:
        await query.edit_message_text("Сначала отправь контракт через /start")
        return ConversationHandler.END

    await query.edit_message_text(f"Ищу транзакции для {contract} в сети {selected}...")

    try:
        if selected == "SOL":
            transfers = await fetch_solana_transfers(contract)
        else:
            transfers = await fetch_evm_transfers(EVM_NETWORKS[selected], contract)
    except Exception as exc:
        logger.exception("Failed to fetch transfers")
        await query.message.reply_text(f"Ошибка при получении данных: {exc}")
        return ConversationHandler.END

    if not transfers:
        await query.message.reply_text("Транзакции не найдены.")
        return ConversationHandler.END

    lines = [f"Последние переводы ({selected}):"]
    for tx in transfers:
        lines.append(
            f"• {tx['to']} | {tx['amount']} {tx['symbol']} (${tx['usd_value']})"
        )

    await query.message.reply_text("\n".join(lines))
    return ConversationHandler.END


async def fetch_evm_transfers(network: EvmNetwork, contract: str, limit: int = 5) -> list[dict[str, str]]:
    api_key = os.getenv(network.api_key_env, "")
    params = {
        "module": "account",
        "action": "tokentx",
        "contractaddress": contract,
        "page": 1,
        "offset": limit,
        "sort": "desc",
        "apikey": api_key,
    }

    async with aiohttp.ClientSession() as session:
        async with session.get(network.api_url, params=params, timeout=20) as resp:
            data = await resp.json()

        result = data.get("result", [])
        if not isinstance(result, list):
            raise RuntimeError(f"Explorer error: {data}")

        price = await fetch_token_price_usd(session, network.chain_id, contract)

    parsed = []
    for item in result:
        decimals = int(item.get("tokenDecimal", 0) or 0)
        raw_value = Decimal(item.get("value", "0"))
        amount = raw_value / (Decimal(10) ** decimals) if decimals else raw_value
        usd_value = (amount * price).quantize(Decimal("0.01"))
        parsed.append(
            {
                "to": item.get("to", "-"),
                "symbol": item.get("tokenSymbol", "TOKEN"),
                "amount": f"{amount.normalize()}",
                "usd_value": f"{usd_value}",
            }
        )
    return parsed


async def fetch_token_price_usd(session: aiohttp.ClientSession, chain: str, contract: str) -> Decimal:
    url = f"https://api.dexscreener.com/latest/dex/tokens/{contract}"
    async with session.get(url, timeout=20) as resp:
        data = await resp.json()

    pairs = data.get("pairs") or []
    chain_pairs = [p for p in pairs if str(p.get("chainId", "")).lower() == chain.lower()]
    candidate = chain_pairs[0] if chain_pairs else (pairs[0] if pairs else None)
    if not candidate:
        return Decimal("0")

    return Decimal(str(candidate.get("priceUsd", "0")))


async def rpc_call(session: aiohttp.ClientSession, method: str, params: list[Any]) -> Any:
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    async with session.post(SOLANA_RPC, json=payload, timeout=25) as resp:
        data = await resp.json()
    if "error" in data:
        raise RuntimeError(str(data["error"]))
    return data.get("result")


async def fetch_solana_transfers(contract: str, limit: int = 5) -> list[dict[str, str]]:
    async with aiohttp.ClientSession() as session:
        signatures = await rpc_call(
            session,
            "getSignaturesForAddress",
            [contract, {"limit": limit}],
        )
        price = await fetch_token_price_usd(session, "solana", contract)

        parsed = []
        for sig_item in signatures:
            tx = await rpc_call(
                session,
                "getTransaction",
                [sig_item["signature"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
            )
            if not tx or not tx.get("meta"):
                continue

            to_addr, amount = parse_solana_token_delta(tx.get("meta", {}), contract)
            if amount <= Decimal("0"):
                continue
            usd_value = (amount * price).quantize(Decimal("0.01"))
            parsed.append(
                {
                    "to": to_addr,
                    "symbol": "SPL",
                    "amount": f"{amount.normalize()}",
                    "usd_value": f"{usd_value}",
                }
            )

    return parsed


def parse_solana_token_delta(meta: dict[str, Any], mint: str) -> tuple[str, Decimal]:
    pre = meta.get("preTokenBalances", [])
    post = meta.get("postTokenBalances", [])

    balances: dict[str, Decimal] = {}

    for entry in pre:
        if entry.get("mint") != mint:
            continue
        owner = entry.get("owner", "unknown")
        amount = Decimal(entry.get("uiTokenAmount", {}).get("uiAmountString", "0"))
        balances[owner] = balances.get(owner, Decimal("0")) - amount

    for entry in post:
        if entry.get("mint") != mint:
            continue
        owner = entry.get("owner", "unknown")
        amount = Decimal(entry.get("uiTokenAmount", {}).get("uiAmountString", "0"))
        balances[owner] = balances.get(owner, Decimal("0")) + amount

    if not balances:
        return "-", Decimal("0")

    to_addr, delta = max(balances.items(), key=lambda x: x[1])
    if delta < 0:
        return to_addr, Decimal("0")
    return to_addr, delta


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Отменено")
    return ConversationHandler.END


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set")

    app = Application.builder().token(token).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            WAITING_CONTRACT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_contract)],
            WAITING_NETWORK: [CallbackQueryHandler(receive_network, pattern=r"^network:")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(conv)
    app.run_polling()


if __name__ == "__main__":
    main()
