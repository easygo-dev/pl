import asyncio
import aiohttp
import time
import logging
from logging.handlers import RotatingFileHandler
import os
from eth_account import Account
from eth_account.messages import encode_typed_data
from web3 import Web3
from decimal import Decimal
from typing import List, Dict, Optional
from dataclasses import dataclass
from telegram import Bot

# config
from config import PRIVATE_KEY, POLYGON_RPC_URL, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

@dataclass
class BookParams:
    token_id: str
    side: str = None

@dataclass
class Market:
    condition_id: str
    question_id: str
    tokens: List[Dict[str, str]]
    rewards: Dict
    minimum_order_size: str
    minimum_tick_size: str
    description: str
    category: str
    end_date_iso: str
    game_start_time: str
    question: str
    market_slug: str
    min_incentive_size: str
    max_incentive_spread: str
    active: bool
    closed: bool
    seconds_delay: int
    icon: str
    fpmm: str

class PolymarketClient:
    def __init__(self, private_key: str, polygon_rpc_url: str):
        self.w3 = Web3(Web3.HTTPProvider(polygon_rpc_url))
        self.account = Account.from_key(private_key)
        self.address = self.account.address
        self.base_url = "https://clob.polymarket.com"
        self.session = aiohttp.ClientSession()

    async def close(self):
        await self.session.close()

    def _get_auth_headers(self):
        timestamp = int(time.time() * 1000)
        nonce = 0  # Implement nonce management if required

        domain = {
            "name": "Polymarket CLOB",
            "version": "1",
            "chainId": 137,
        }
        message = {
            "action": "Auth",
            "timestamp": timestamp,
            "nonce": nonce
        }
        data = {
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                ],
                "Auth": [
                    {"name": "action", "type": "string"},
                    {"name": "timestamp", "type": "uint256"},
                    {"name": "nonce", "type": "uint256"},
                ],
            },
            "domain": domain,
            "primaryType": "Auth",
            "message": message,
        }

        signed_message = self.account.sign_message(encode_typed_data(data))

        return {
            "POLY-ADDRESS": self.address,
            "POLY-SIGNATURE": signed_message.signature.hex(),
            "POLY-TIMESTAMP": str(timestamp),
            "POLY-NONCE": str(nonce),
        }

    async def get_markets(self, next_cursor: str = "") -> Dict:
        url = f"{self.base_url}/markets"
        params = {"next_cursor": next_cursor}
        async with self.session.get(url, params=params, headers=self._get_auth_headers()) as response:
            return await response.json()

    async def get_market(self, condition_id: str) -> Market:
        url = f"{self.base_url}/markets/{condition_id}"
        async with self.session.get(url, headers=self._get_auth_headers()) as response:
            data = await response.json()
            return Market(**data["market"])

    async def get_order_book(self, token_id: str) -> Dict:
        url = f"{self.base_url}/book"
        params = {"token_id": token_id}
        async with self.session.get(url, params=params, headers=self._get_auth_headers()) as response:
            return await response.json()

    async def get_spread(self, token_id: str) -> Decimal:
        url = f"{self.base_url}/spread"
        params = {"token_id": token_id}
        async with self.session.get(url, params=params, headers=self._get_auth_headers()) as response:
            data = await response.json()
            return Decimal(data["spread"])

class OrderbookMonitor:
    def __init__(self, private_key: str, polygon_rpc_url: str, telegram_bot_token: str, telegram_chat_id: str):
        self.client = PolymarketClient(private_key, polygon_rpc_url)
        self.telegram_bot = Bot(telegram_bot_token)
        self.telegram_chat_id = telegram_chat_id
        self.markets: Dict[str, Market] = {}
        self.previous_books: Dict[str, Dict] = {}
        self.previous_spreads: Dict[str, Decimal] = {}
        self.setup_logging()

    def setup_logging(self):
        self.logger = logging.getLogger('PolymarketMonitor')
        self.logger.setLevel(logging.INFO)

        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        console_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        console_handler.setFormatter(console_formatter)
        self.logger.addHandler(console_handler)

        log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'poly.log')
        file_handler = RotatingFileHandler(log_file, maxBytes=5*1024*1024, backupCount=2)
        file_handler.setLevel(logging.INFO)
        file_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(file_formatter)
        self.logger.addHandler(file_handler)

    async def initialize_markets(self):
        self.logger.info("Initializing markets...")
        next_cursor = ""
        while True:
            markets_data = await self.client.get_markets(next_cursor)
            for market_data in markets_data["data"]:
                market = Market(**market_data)
                self.markets[market.condition_id] = market
                self.logger.debug(f"Added market: {market.description}")
            next_cursor = markets_data["next_cursor"]
            if next_cursor == "LTE=":
                break
        self.logger.info(f"Initialized {len(self.markets)} markets")

    async def monitor_markets(self):
        await self.initialize_markets()
        self.logger.info("Starting market monitoring...")
        while True:
            try:
                for condition_id, market in self.markets.items():
                    if market.active and not market.closed:
                        token_id = market.tokens[0]["token_id"]
                        await self.check_market(condition_id, token_id)
                await asyncio.sleep(60)
            except Exception as e:
                self.logger.error(f"An error occurred: {str(e)}", exc_info=True)
                await asyncio.sleep(60)

    async def check_market(self, condition_id: str, token_id: str):
        self.logger.debug(f"Checking market: {condition_id}")
        book = await self.client.get_order_book(token_id)
        spread = await self.client.get_spread(token_id)

        price_changes = self.calculate_price_changes(self.previous_books.get(token_id), book)
        spread_change = self.calculate_spread_change(self.previous_spreads.get(token_id), spread)

        if price_changes or spread_change:
            await self.send_alert(condition_id, token_id, price_changes, spread_change)

        self.previous_books[token_id] = book
        self.previous_spreads[token_id] = spread

    def calculate_price_changes(self, previous_book: Optional[Dict], current_book: Dict) -> Dict:
        changes = {}
        if previous_book:
            for side in ["bids", "asks"]:
                prev_best = Decimal(previous_book[side][0]["price"]) if previous_book[side] else Decimal('0')
                curr_best = Decimal(current_book[side][0]["price"]) if current_book[side] else Decimal('0')
                if prev_best != 0:
                    change_percent = (curr_best - prev_best) / prev_best * 100
                    if abs(change_percent) >= 15:  # 15% threshold
                        changes[side] = change_percent
        return changes

    def calculate_spread_change(self, previous_spread: Optional[Decimal], current_spread: Decimal) -> Optional[Decimal]:
        if previous_spread:
            change_percent = (current_spread - previous_spread) / previous_spread * 100
            if abs(change_percent) >= 50:  # 50% threshold for spread changes
                return change_percent
        return None

    async def send_alert(self, condition_id: str, token_id: str, price_changes: Dict, spread_change: Optional[Decimal]):
        market = self.markets[condition_id]
        message = f"🚨 Significant changes in market: {market.description}\n"
        message += f"Market ID: {condition_id}\n"
        message += f"Token ID: {token_id}\n"
        for side, change in price_changes.items():
            message += f"{side.capitalize()} price: {change:.2f}% change\n"
        if spread_change:
            message += f"Spread: {spread_change:.2f}% change\n"
        
        self.logger.info(message)
        
        try:
            await self.telegram_bot.send_message(chat_id=self.telegram_chat_id, text=message)
            self.logger.info("Alert sent to Telegram successfully")
        except Exception as e:
            self.logger.error(f"Failed to send Telegram alert: {str(e)}", exc_info=True)

async def main():
    monitor = OrderbookMonitor(PRIVATE_KEY, POLYGON_RPC_URL, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
    try:
        await monitor.monitor_markets()
    finally:
        await monitor.client.close()

if __name__ == "__main__":
    asyncio.run(main())
