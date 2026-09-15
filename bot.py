import logging
import os
import sqlite3
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DAILY_REWARD = int(os.getenv("DAILY_REWARD_NAIRA", "100"))
CHANNEL_ID = os.getenv("CHANNEL_ID", "")
CHANNEL_URL = os.getenv("CHANNEL_URL", "https://t.me/rewardbmax")
MIN_WITHDRAWAL = int(os.getenv("MIN_WITHDRAWAL_NAIRA", "1000"))
MAX_WITHDRAWAL = int(os.getenv("MAX_WITHDRAWAL_NAIRA", "100000"))
SUPPORTED_BANKS = [
    "Opay",
    "PalmPay",
    "Moniepoint",
    "Kuda",
    "First Bank",
    "UBA",
    "Access Bank",
    "GTBank",
    "Zenith Bank",
    "Fidelity Bank",
]
CHANNEL_TASKS = [
    {
        "key": "telegram_1",
        "kind": "telegram",
        "chat_id": os.getenv("TELEGRAM_CHANNEL_1_ID", ""),
        "url": os.getenv("TELEGRAM_CHANNEL_1_URL", ""),
        "label": "Telegram channel 1",
    },
    {
        "key": "telegram_2",
        "kind": "telegram",
        "chat_id": os.getenv("TELEGRAM_CHANNEL_2_ID", ""),
        "url": os.getenv("TELEGRAM_CHANNEL_2_URL", ""),
        "label": "Telegram channel 2",
    },
]
CHANNEL_TASK_REWARD = int(os.getenv("CHANNEL_TASK_REWARD_NAIRA", "500"))
REFERRAL_REWARD = int(os.getenv("REFERRAL_REWARD_NAIRA", "500"))
WITHDRAWAL_DATE = os.getenv("WITHDRAWAL_DATE", "").strip()
MIN_REFERRALS = int(os.getenv("MIN_REFERRALS", "20"))
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "rewards.db"))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)


class RewardsStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id INTEGER PRIMARY KEY,
                    username TEXT,
                    balance INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS claims (
                    telegram_id INTEGER NOT NULL,
                    claim_date TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    claimed_at TEXT NOT NULL,
                    PRIMARY KEY (telegram_id, claim_date),
                    FOREIGN KEY (telegram_id) REFERENCES users (telegram_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS referrals (
                    referred_id INTEGER PRIMARY KEY,
                    referrer_id INTEGER NOT NULL,
                    referred_at TEXT NOT NULL,
                    FOREIGN KEY (referred_id) REFERENCES users (telegram_id),
                    FOREIGN KEY (referrer_id) REFERENCES users (telegram_id)
                )
                """
            )
            referral_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(referrals)").fetchall()
            }
            if "rewarded_at" not in referral_columns:
                connection.execute("ALTER TABLE referrals ADD COLUMN rewarded_at TEXT")
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(users)").fetchall()
            }
            if "reward_balance" not in columns:
                connection.execute(
                    "ALTER TABLE users ADD COLUMN reward_balance INTEGER NOT NULL DEFAULT 0"
                )
                connection.execute(
                    "UPDATE users SET reward_balance = balance WHERE balance != 0"
                )
            if "referral_balance" not in columns:
                connection.execute(
                    "ALTER TABLE users ADD COLUMN referral_balance INTEGER NOT NULL DEFAULT 0"
                )
            for column in ("bank_name", "account_number", "account_name"):
                if column not in columns:
                    connection.execute(f"ALTER TABLE users ADD COLUMN {column} TEXT")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS channel_tasks (
                    telegram_id INTEGER NOT NULL,
                    task_key TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    awarded_at TEXT NOT NULL,
                    reversed_at TEXT,
                    PRIMARY KEY (telegram_id, task_key),
                    FOREIGN KEY (telegram_id) REFERENCES users (telegram_id)
                )
                """
            )

    def ensure_user(self, telegram_id: int, username: str | None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO users (telegram_id, username, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET username = excluded.username
                """,
                (telegram_id, username, now),
            )

    def add_reward(self, telegram_id: int, amount: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE users SET balance = balance + ?, reward_balance = reward_balance + ? WHERE telegram_id = ?",
                (amount, amount, telegram_id),
            )

    def add_referral_reward(self, telegram_id: int, amount: int) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE users SET balance = balance + ?, referral_balance = referral_balance + ? WHERE telegram_id = ?",
                (amount, amount, telegram_id),
            )

    def claim_today(self, telegram_id: int, amount: int) -> bool:
        today = datetime.now(timezone.utc).date().isoformat()
        claimed_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO claims (telegram_id, claim_date, amount, claimed_at) VALUES (?, ?, ?, ?)",
                    (telegram_id, today, amount, claimed_at),
                )
                connection.execute(
                    "UPDATE users SET balance = balance + ?, reward_balance = reward_balance + ? WHERE telegram_id = ?",
                    (amount, amount, telegram_id),
                )
            except sqlite3.IntegrityError:
                connection.rollback()
                return False
        return True

    def get_balance(self, telegram_id: int) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT balance FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()
        return int(row["balance"]) if row else 0

    def get_user(self, telegram_id: int) -> sqlite3.Row | None:
        with self._connect() as connection:
            return connection.execute(
                "SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)
            ).fetchone()

    def save_bank_details(
        self, telegram_id: int, bank_name: str, account_number: str, account_name: str
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE users SET bank_name = ?, account_number = ?, account_name = ? WHERE telegram_id = ?",
                (bank_name, account_number, account_name, telegram_id),
            )

    def claimed_today(self, telegram_id: int) -> bool:
        today = datetime.now(timezone.utc).date().isoformat()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM claims WHERE telegram_id = ? AND claim_date = ?",
                (telegram_id, today),
            ).fetchone()
        return row is not None

    def add_referral(self, referred_id: int, referrer_id: int) -> bool:
        if referred_id == referrer_id:
            return False
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO referrals (referred_id, referrer_id, referred_at) VALUES (?, ?, ?)",
                (referred_id, referrer_id, datetime.now(timezone.utc).isoformat()),
            )
        return cursor.rowcount == 1

    def referral_count(self, referrer_id: int) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM referrals WHERE referrer_id = ?",
                (referrer_id,),
            ).fetchone()
        return int(row["total"])

    def referral_for(self, referred_id: int) -> sqlite3.Row | None:
        with self._connect() as connection:
            return connection.execute(
                "SELECT referrer_id, rewarded_at FROM referrals WHERE referred_id = ?",
                (referred_id,),
            ).fetchone()

    def reward_referral(self, referred_id: int, amount: int) -> int | None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT referrer_id, rewarded_at FROM referrals WHERE referred_id = ?",
                (referred_id,),
            ).fetchone()
            if row is None or row["rewarded_at"] is not None:
                return None
            connection.execute(
                "UPDATE referrals SET rewarded_at = ? WHERE referred_id = ?",
                (now, referred_id),
            )
            connection.execute(
                "UPDATE users SET balance = balance + ?, referral_balance = referral_balance + ? WHERE telegram_id = ?",
                (amount, amount, row["referrer_id"]),
            )
        return int(row["referrer_id"])

    def get_balances(self, telegram_id: int) -> sqlite3.Row | None:
        with self._connect() as connection:
            return connection.execute(
                "SELECT balance, reward_balance, referral_balance FROM users WHERE telegram_id = ?",
                (telegram_id,),
            ).fetchone()

    def task_awarded(self, telegram_id: int, task_key: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM channel_tasks WHERE telegram_id = ? AND task_key = ? AND reversed_at IS NULL",
                (telegram_id, task_key),
            ).fetchone()
        return row is not None

    def award_task(self, telegram_id: int, task_key: str, amount: int) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT reversed_at FROM channel_tasks WHERE telegram_id = ? AND task_key = ?",
                    (telegram_id, task_key),
                ).fetchone()
                if existing is None:
                    connection.execute(
                        "INSERT INTO channel_tasks (telegram_id, task_key, amount, awarded_at) VALUES (?, ?, ?, ?)",
                        (telegram_id, task_key, amount, now),
                    )
                elif existing["reversed_at"] is not None:
                    connection.execute(
                        "UPDATE channel_tasks SET amount = ?, awarded_at = ?, reversed_at = NULL WHERE telegram_id = ? AND task_key = ?",
                        (amount, now, telegram_id, task_key),
                    )
                else:
                    connection.rollback()
                    return False
                connection.execute(
                    "UPDATE users SET balance = balance + ?, reward_balance = reward_balance + ? WHERE telegram_id = ?",
                    (amount, amount, telegram_id),
                )
            except sqlite3.IntegrityError:
                connection.rollback()
                return False
        return True

    def reverse_task(self, telegram_id: int, task_key: str) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT amount FROM channel_tasks WHERE telegram_id = ? AND task_key = ? AND reversed_at IS NULL",
                (telegram_id, task_key),
            ).fetchone()
            if row is None:
                return 0
            amount = int(row["amount"])
            connection.execute(
                "UPDATE channel_tasks SET reversed_at = ? WHERE telegram_id = ? AND task_key = ?",
                (now, telegram_id, task_key),
            )
            connection.execute(
                "UPDATE users SET balance = MAX(0, balance - ?), reward_balance = MAX(0, reward_balance - ?) WHERE telegram_id = ?",
                (amount, amount, telegram_id),
            )
        return amount


store = RewardsStore(DATABASE_PATH)


def claim_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"🎁 Claim ₦{DAILY_REWARD:,}", callback_data="claim")]]
    )


def bank_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for index in range(0, len(SUPPORTED_BANKS), 2):
        rows.append(
            [
                InlineKeyboardButton(
                    f"🏦 {bank}", callback_data=f"bank:{index + offset}"
                )
                for offset, bank in enumerate(SUPPORTED_BANKS[index : index + 2])
            ]
        )
    return InlineKeyboardMarkup(rows)


def save_bank_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🏦 Save bank details", callback_data="save_bank")]]
    )


def join_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📢 Join channel", url=CHANNEL_URL)],
            [InlineKeyboardButton("✅ I've joined", callback_data="check_join")],
        ]
    )


def task_keyboard(telegram_id: int) -> InlineKeyboardMarkup:
    rows = []
    telegram_tasks = [task for task in CHANNEL_TASKS if task["kind"] == "telegram" and task["chat_id"] and task["url"]]
    next_task = next(
        (task for task in telegram_tasks if not store.task_awarded(telegram_id, task["key"])),
        None,
    )
    if next_task:
        rows.append([InlineKeyboardButton(f"📢 Join {next_task['label']}", url=next_task["url"])])
        rows.append(
            [InlineKeyboardButton(f"✅ Verify {next_task['label']}", callback_data=f"verify:{next_task['key']}")]
        )
    return InlineKeyboardMarkup(
        rows or [[InlineKeyboardButton("✅ All tasks completed", callback_data="noop")]]
    )


def menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [["🎁 Claim reward", "👤 Profile"], ["💸 Withdraw", "📋 Tasks"], ["🔗 Referral"]],
        resize_keyboard=True,
    )


def naira(amount: int) -> str:
    return f"₦{amount:,}"


def withdrawal_date_text() -> str:
    if not WITHDRAWAL_DATE:
        return "not set yet"
    try:
        return datetime.strptime(WITHDRAWAL_DATE, "%Y-%m-%d").strftime("%d %B %Y")
    except ValueError:
        return WITHDRAWAL_DATE


def is_withdrawal_day() -> bool:
    return bool(WITHDRAWAL_DATE and datetime.now(timezone.utc).date().isoformat() == WITHDRAWAL_DATE)


async def is_channel_member(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not CHANNEL_ID:
        logger.error("CHANNEL_ID is not configured; access is blocked")
        return False
    try:
        member = await context.bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status in {"member", "administrator", "creator"} or (
            member.status == "restricted" and getattr(member, "is_member", False)
        )
    except BadRequest as error:
        if "Member list is inaccessible" in str(error):
            logger.error("Bot must be an administrator in the gate channel")
        else:
            logger.exception("Unable to verify channel membership")
        return False
    except Exception:
        logger.exception("Unable to verify channel membership")
        return False


async def send_join_gate(message, first_name: str) -> None:
    await message.reply_text(
        f"<b>🌟 Welcome to Daily Rewards, {escape(first_name)}!</b>\n\n"
        "🎁 Earn naira by completing simple tasks.\n"
        "📢 Join our channel to unlock your rewards menu.\n"
        "💰 You must join before you can start earning.\n\n"
        "Tap <b>Join channel</b>, then come back and press <b>✅ I've joined</b>.",
        parse_mode=ParseMode.HTML,
        reply_markup=join_keyboard(),
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or update.message is None:
        return
    store.ensure_user(user.id, user.username)
    referral_started = bool(context.args and context.args[0].startswith("ref_"))
    referrer = None
    new_referral = False
    if referral_started:
        try:
            referrer_id = int(context.args[0][4:])
            referrer = store.get_user(referrer_id)
            new_referral = store.add_referral(user.id, referrer_id)
        except ValueError:
            pass
    if new_referral and referrer:
        referrer_name = referrer["username"] or f"user {referrer['telegram_id']}"
        try:
            await context.bot.send_message(
                chat_id=referrer["telegram_id"],
                text=(
                    "🎉 <b>Great news, you have a new referral!</b>\n\n"
                    f"<b>{escape(user.full_name)}</b> just joined the bot using your personal referral link.\n\n"
                    f"They need to join and verify all Telegram task channels. They will earn "
                    f"<b>{naira(CHANNEL_TASK_REWARD)}</b> for each completed Telegram task, and "
                    f"after they finish everything, you will receive <b>{naira(REFERRAL_REWARD)}</b> "
                    "in your referral balance."
                ),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            logger.exception("Unable to notify referrer about new referral")
    if not await is_channel_member(user.id, context):
        await send_join_gate(update.message, user.first_name)
        return
    await update.message.reply_text(
        f"<b>Welcome, {escape(user.first_name)}!</b>\n\n"
        f"Claim <b>{naira(DAILY_REWARD)}</b> once per day.\n\n"
        "Choose an option below:",
        parse_mode=ParseMode.HTML,
        reply_markup=menu_keyboard(),
    )
    if referral_started:
        inviter = escape(referrer["username"] or referrer_name) if referrer else "your inviter"
        await update.message.reply_text(
            "🤝 <b>Welcome, you were invited!</b>\n\n"
            f"<b>{inviter}</b> shared this rewards bot with you.\n\n"
            f"Join and verify each Telegram task channel to earn <b>{naira(CHANNEL_TASK_REWARD)}</b> "
            f"for every completed task. When you finish all Telegram tasks, "
            f"<b>{inviter}</b> will receive <b>{naira(REFERRAL_REWARD)}</b> in their referral balance.\n\n"
            "Please complete the tasks honestly so both of you can benefit.",
            parse_mode=ParseMode.HTML,
        )


async def claim(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or update.message is None:
        return
    if not await is_channel_member(user.id, context):
        await send_join_gate(update.message, user.first_name)
        return
    store.ensure_user(user.id, user.username)
    if store.claim_today(user.id, DAILY_REWARD):
        balance = store.get_balance(user.id)
        await update.message.reply_text(
            f"<b>🎉 Claim successful!</b>\n\n"
            f"You received <b>{naira(DAILY_REWARD)}</b>.\n"
            f"Balance: <b>{naira(balance)}</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=menu_keyboard(),
        )
    else:
        await update.message.reply_text(
            "<b>⏳ Already claimed</b>\n\nCome back tomorrow for your next reward!",
            parse_mode=ParseMode.HTML,
            reply_markup=menu_keyboard(),
        )


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or update.message is None:
        return
    store.ensure_user(user.id, user.username)
async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or update.message is None:
        return
    if not await is_channel_member(user.id, context):
        await send_join_gate(update.message, user.first_name)
        return
    store.ensure_user(user.id, user.username)
    profile_data = store.get_user(user.id)
    balances = store.get_balances(user.id)
    username = f"@{user.username}" if user.username else "Not set"
    joined_at = profile_data["created_at"][:10] if profile_data else "Unknown"
    bank_details = "Not saved"
    if profile_data and profile_data["bank_name"]:
        bank_details = (
            f"{escape(profile_data['bank_name'])} · "
            f"{escape(profile_data['account_number'])} · "
            f"{escape(profile_data['account_name'])}"
        )
    await update.message.reply_text(
        f"<b>👤 Profile</b>\n\n"
        f"<b>Name:</b> {escape(user.full_name)}\n"
        f"<b>Username:</b> {escape(username)}\n"
        f"<b>💰 Total balance:</b> {naira(balances['balance'])}\n"
        f"<b>🎁 Reward balance:</b> {naira(balances['reward_balance'])}\n"
        f"<b>🤝 Referral balance:</b> {naira(balances['referral_balance'])}\n"
        f"<b>Minimum withdrawal:</b> {naira(MIN_WITHDRAWAL)}\n"
        f"<b>Maximum withdrawal:</b> {naira(MAX_WITHDRAWAL)}\n"
        f"<b>Total referrals:</b> {store.referral_count(user.id)}\n"
        f"<b>🏦 Bank:</b> {bank_details}\n"
        f"<b>Joined:</b> {joined_at}",
        parse_mode=ParseMode.HTML,
        reply_markup=save_bank_keyboard(),
    )


async def withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or update.message is None:
        return
    if not await is_channel_member(user.id, context):
        await send_join_gate(update.message, user.first_name)
        return
    store.ensure_user(user.id, user.username)
    referrals = store.referral_count(user.id)
    if not is_withdrawal_day():
        await update.message.reply_text(
            "<b>💸 Withdrawals are closed</b>\n\n"
            f"The next withdrawal date is <b>{withdrawal_date_text()}</b>.\n"
            "Please return on that date to check your eligibility.",
            parse_mode=ParseMode.HTML,
            reply_markup=menu_keyboard(),
        )
        return
    eligible = referrals >= MIN_REFERRALS
    await update.message.reply_text(
        "<b>💸 Withdrawal day</b>\n\n"
        f"Eligibility: <b>{'✅ Eligible' if eligible else '❌ Not eligible'}</b>\n"
        f"Referrals: <b>{referrals}/{MIN_REFERRALS}</b>\n"
        f"Available balance: <b>{naira(store.get_balance(user.id))}</b>\n"
        f"Withdrawal range: <b>{naira(MIN_WITHDRAWAL)} - {naira(MAX_WITHDRAWAL)}</b>\n\n"
        + ("You can submit a withdrawal request now." if eligible else "Invite more friends before requesting a withdrawal."),
        parse_mode=ParseMode.HTML,
        reply_markup=menu_keyboard(),
    )


async def tasks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or update.message is None:
        return
    if not await is_channel_member(user.id, context):
        await send_join_gate(update.message, user.first_name)
        return
    store.ensure_user(user.id, user.username)
    status = "✅ Completed" if store.claimed_today(user.id) else "⏳ Available"
    configured_telegram = [task for task in CHANNEL_TASKS if task["kind"] == "telegram" and task["chat_id"]]
    completed = sum(store.task_awarded(user.id, task["key"]) for task in configured_telegram)
    await update.message.reply_text(
        f"<b>📋 Tasks</b>\n\n"
        f"🌞 Daily reward: <b>{status}</b> (+{naira(DAILY_REWARD)})\n"
        f"📢 Telegram channels: <b>{completed}/{len(configured_telegram)}</b> complete\n"
        f"💵 Each Telegram join: <b>+{naira(CHANNEL_TASK_REWARD)}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=task_keyboard(user.id),
    )
    if completed == len(configured_telegram) and configured_telegram:
        await update.message.reply_text(
            "<b>🎉 All tasks completed!</b>\n\n"
            "There are no more tasks available right now.\n"
            "Invite friends to join and earn referral rewards.",
            parse_mode=ParseMode.HTML,
            reply_markup=menu_keyboard(),
        )


async def referral(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or update.message is None:
        return
    if not await is_channel_member(user.id, context):
        await send_join_gate(update.message, user.first_name)
        return
    store.ensure_user(user.id, user.username)
    bot_username = context.bot.username
    if not bot_username:
        bot_username = (await context.bot.get_me()).username
    referral_link = f"https://t.me/{bot_username}?start=ref_{user.id}"
    share_text = (
        "🎁 Join this rewards bot and earn daily naira rewards!\n"
        f"💰 Earn {naira(CHANNEL_TASK_REWARD)} for each Telegram task you complete.\n"
        f"🤝 I earn {naira(REFERRAL_REWARD)} when you complete all the tasks.\n"
        f"🔗 Join here: {referral_link}"
    )
    whatsapp_link = f"https://wa.me/?text={quote_plus(share_text)}"
    await update.message.reply_text(
        "<b>🔗 Invite friends</b>\n\n"
        "Share this message with friends:\n\n"
        f"<i>{escape(share_text)}</i>\n\n"
        f"<code>{referral_link}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("📤 Share on Telegram", url=f"https://t.me/share/url?url={quote_plus(referral_link)}")],
             [InlineKeyboardButton("🟢 Share on WhatsApp", url=whatsapp_link)]]
        ),
    )


async def save_bank(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return
    await query.answer()
    context.user_data.pop("bank_account_number", None)
    context.user_data.pop("bank_name", None)
    context.user_data["bank_state"] = "bank_choice"
    await query.edit_message_text(
        "<b>🏦 Save bank details</b>\n\nChoose your bank:",
        parse_mode=ParseMode.HTML,
        reply_markup=bank_keyboard(),
    )


async def select_bank(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return
    await query.answer()
    try:
        bank_index = int((query.data or "").removeprefix("bank:"))
        bank_name = SUPPORTED_BANKS[bank_index]
    except (ValueError, IndexError):
        await query.answer("Invalid bank selection.", show_alert=True)
        return
    context.user_data["bank_name"] = bank_name
    context.user_data["bank_state"] = "account_number"
    await query.edit_message_text(
        f"<b>🏦 {escape(bank_name)}</b>\n\n"
        "Please send your 10-digit account number.",
        parse_mode=ParseMode.HTML,
    )


async def bank_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.effective_user is None:
        return
    state = context.user_data.get("bank_state")
    if state not in {"account_number", "account_name"}:
        return
    value = (update.message.text or "").strip()
    if state == "account_number":
        if not value.isdigit() or len(value) != 10:
            await update.message.reply_text("Please send a valid 10-digit account number.")
            return
        context.user_data["bank_account_number"] = value
        context.user_data["bank_state"] = "account_name"
        await update.message.reply_text(
            f"Account number received for <b>{escape(context.user_data['bank_name'])}</b>.\n\n"
            "Now send the account holder's full name.",
            parse_mode=ParseMode.HTML,
        )
        return
    if len(value) < 2:
        await update.message.reply_text("Please send the full account name.")
        return
    user = update.effective_user
    store.ensure_user(user.id, user.username)
    store.save_bank_details(
        user.id,
        context.user_data["bank_name"],
        context.user_data["bank_account_number"],
        value,
    )
    context.user_data.pop("bank_state", None)
    await update.message.reply_text(
        "<b>✅ Bank details saved</b>\n\n"
        f"🏦 Bank: <b>{escape(context.user_data.pop('bank_name'))}</b>\n"
        f"🔢 Account: <b>{escape(context.user_data.pop('bank_account_number'))}</b>\n"
        f"👤 Name: <b>{escape(value)}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=menu_keyboard(),
    )
async def menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    actions = {
        "🎁 Claim reward": claim,
        "👤 Profile": profile,
        "💸 Withdraw": withdraw,
        "📋 Tasks": tasks,
        "🔗 Referral": referral,
    }
    handler = actions.get(update.message.text or "")
    if handler:
        await handler(update, context)


async def claim_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return
    await query.answer()
    if not await is_channel_member(user.id, context):
        await query.edit_message_text(
            "<b>🔒 Join the channel first</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=join_keyboard(),
        )
        return
    store.ensure_user(user.id, user.username)
    if store.claim_today(user.id, DAILY_REWARD):
        balance = store.get_balance(user.id)
        await query.edit_message_text(
            f"<b>🎉 Claim successful!</b>\n\n"
            f"You received <b>{naira(DAILY_REWARD)}</b>.\n"
            f"Balance: <b>{naira(balance)}</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=claim_keyboard(),
        )
    else:
        await query.edit_message_text(
            "<b>⏳ Already claimed</b>\n\nCome back tomorrow for your next reward!",
            parse_mode=ParseMode.HTML,
            reply_markup=claim_keyboard(),
        )


async def verify_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return
    await query.answer()
    task_key = (query.data or "").removeprefix("verify:")
    task = next((item for item in CHANNEL_TASKS if item["key"] == task_key), None)
    if task is None or task["kind"] != "telegram" or not task["chat_id"]:
        await query.answer("This task is not configured.", show_alert=True)
        return
    telegram_tasks = [item for item in CHANNEL_TASKS if item["kind"] == "telegram" and item["chat_id"]]
    next_task = next(
        (item for item in telegram_tasks if not store.task_awarded(user.id, item["key"])),
        None,
    )
    if next_task is None or next_task["key"] != task_key:
        await query.answer("Complete the tasks in order.", show_alert=True)
        return
    store.ensure_user(user.id, user.username)
    try:
        member = await context.bot.get_chat_member(task["chat_id"], user.id)
        is_member = member.status in {"member", "administrator", "creator"} or (
            member.status == "restricted" and getattr(member, "is_member", False)
        )
    except BadRequest as error:
        if "Member list is inaccessible" in str(error):
            await query.answer(
                "Verification is not ready. Please ask the bot admin to add this bot as an administrator in the channel.",
                show_alert=True,
            )
            logger.error("Bot is not an administrator in task channel %s", task["chat_id"])
            return
        logger.exception("Unable to verify task membership")
        is_member = False
    except Exception:
        logger.exception("Unable to verify task membership")
        is_member = False
    if not is_member:
        removed = store.reverse_task(user.id, task_key)
        message = "❌ Join the channel first."
        if removed:
            message += f"\n{naira(removed)} was deducted for leaving."
        await query.answer(message, show_alert=True)
        return
    awarded = store.award_task(user.id, task_key, CHANNEL_TASK_REWARD)
    if not awarded:
        await query.answer("Already verified.", show_alert=True)
        return
    referrer_id = None
    if telegram_tasks and all(store.task_awarded(user.id, item["key"]) for item in telegram_tasks):
        referrer_id = store.reward_referral(user.id, REFERRAL_REWARD)
    balances = store.get_balances(user.id)
    text = (
        f"✅ <b>Task completed!</b>\n\n"
        f"{escape(task['label'])} verified.\n"
        f"🎁 <b>{naira(CHANNEL_TASK_REWARD)}</b> added to your reward balance.\n"
        f"💰 Total balance: <b>{naira(balances['balance'])}</b>"
    )
    if referrer_id:
        text += f"\n🤝 Your referrer earned {naira(REFERRAL_REWARD)}!"
        try:
            await context.bot.send_message(
                chat_id=referrer_id,
                text=(
                    "🎉 <b>Referral completed!</b>\n\n"
                    f"Your referral finished all Telegram tasks. You earned <b>{naira(REFERRAL_REWARD)}</b>."
                ),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            logger.exception("Unable to notify referrer")
    if telegram_tasks and all(store.task_awarded(user.id, item["key"]) for item in telegram_tasks):
        text += (
            "\n\n🎉 <b>All tasks completed!</b>"
            "\nThere are no more tasks available right now."
            "\nInvite friends to join and earn more referral rewards."
        )
    await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=task_keyboard(user.id))


async def membership_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    change = update.chat_member
    if change is None:
        return
    new_status = change.new_chat_member.status
    old_status = change.old_chat_member.status
    if new_status not in {"member", "administrator", "creator", "restricted", "left", "kicked"}:
        return
    task = next(
        (
            item for item in CHANNEL_TASKS
            if item["kind"] == "telegram"
            and item["chat_id"]
            and (
                str(change.chat.id) == str(item["chat_id"])
                or (
                    str(item["chat_id"]).startswith("@")
                    and f"@{change.chat.username}".lower() == str(item["chat_id"]).lower()
                )
            )
        ),
        None,
    )
    if task is None:
        return
    user = change.new_chat_member.user
    is_member = new_status in {"member", "administrator", "creator"} or (
        new_status == "restricted" and getattr(change.new_chat_member, "is_member", False)
    )
    was_member = old_status in {"member", "administrator", "creator"} or (
        old_status == "restricted" and getattr(change.old_chat_member, "is_member", False)
    )
    if is_member and not was_member:
        store.ensure_user(user.id, user.username)
        telegram_tasks = [
            item for item in CHANNEL_TASKS
            if item["kind"] == "telegram" and item["chat_id"]
        ]
        next_task = next(
            (item for item in telegram_tasks if not store.task_awarded(user.id, item["key"])),
            None,
        )
        if next_task is None or next_task["key"] != task["key"]:
            return
        if not store.award_task(user.id, task["key"], CHANNEL_TASK_REWARD):
            return
        referrer_id = None
        if all(store.task_awarded(user.id, item["key"]) for item in telegram_tasks):
            referrer_id = store.reward_referral(user.id, REFERRAL_REWARD)
        balances = store.get_balances(user.id)
        text = (
            "✅ <b>Task completed automatically!</b>\n\n"
            f"{escape(task['label'])} joined successfully.\n"
            f"🎁 <b>{naira(CHANNEL_TASK_REWARD)}</b> added to your reward balance.\n"
            f"💰 Total balance: <b>{naira(balances['balance'])}</b>"
        )
        if referrer_id:
            text += f"\n🤝 Your referrer earned {naira(REFERRAL_REWARD)}!"
            try:
                await context.bot.send_message(
                    chat_id=referrer_id,
                    text=(
                        "🎉 <b>Referral reward unlocked!</b>\n\n"
                        f"Your referral completed all Telegram tasks. You earned <b>{naira(REFERRAL_REWARD)}</b>."
                    ),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                logger.exception("Unable to notify referrer about automatic task completion")
        if all(store.task_awarded(user.id, item["key"]) for item in telegram_tasks):
            text += (
                "\n\n🎉 <b>All tasks completed!</b>"
                "\nThere are no more tasks available right now."
                "\nInvite friends to join and earn more referral rewards."
            )
        try:
            await context.bot.send_message(
                chat_id=user.id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=task_keyboard(user.id),
            )
        except Exception:
            logger.exception("Unable to notify user about automatic task completion")
        return
    if is_member or new_status not in {"left", "kicked"}:
        return
    removed = store.reverse_task(user.id, task["key"])
    if not removed:
        return
    try:
        await context.bot.send_message(
            chat_id=user.id,
            text=(
                "⚠️ <b>Task reward reversed</b>\n\n"
                f"You left {escape(task['label'])}, so <b>{naira(removed)}</b> was deducted immediately."
            ),
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        logger.exception("Unable to notify user about task deduction")


async def check_join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return
    await query.answer()
    if not await is_channel_member(user.id, context):
        await query.answer(
            "Please join the channel before you can start earning rewards.",
            show_alert=True,
        )
        await query.edit_message_text(
            "<b>🔒 Channel membership required</b>\n\n"
            "You have to join the channel before you can start earning rewards.\n\n"
            "Join the channel, then tap <b>✅ I've joined</b> again.",
            parse_mode=ParseMode.HTML,
            reply_markup=join_keyboard(),
        )
        return
    await query.edit_message_text(
        f"<b>✅ Verified!</b>\n\nWelcome, {escape(user.first_name)}. Your rewards menu is ready.",
        parse_mode=ParseMode.HTML,
        reply_markup=None,
    )
    await context.bot.send_message(
        chat_id=user.id,
        text="Choose an option below:",
        reply_markup=menu_keyboard(),
    )


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing. Add it to a .env file.")

    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("claim", claim))
    application.add_handler(CallbackQueryHandler(claim_button, pattern="^claim$"))
    application.add_handler(CallbackQueryHandler(verify_task, pattern="^verify:"))
    application.add_handler(CallbackQueryHandler(check_join, pattern="^check_join$"))
    application.add_handler(CallbackQueryHandler(save_bank, pattern="^save_bank$"))
    application.add_handler(CallbackQueryHandler(select_bank, pattern="^bank:"))
    application.add_handler(ChatMemberHandler(membership_update, ChatMemberHandler.CHAT_MEMBER))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, bank_input), group=1)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_button))
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
