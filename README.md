# Daily Rewards Telegram Bot

A small Telegram bot that gives each user one reward per UTC calendar day.

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy its token.
2. Create a virtual environment:

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

3. Install dependencies:

   ```powershell
   pip install -r requirements.txt
   ```

4. Copy `.env.example` to `.env` and set `BOT_TOKEN`, `CHANNEL_ID`, and `CHANNEL_URL`.
   The bot must be an administrator in the channel so it can verify membership.
   Configure the three Telegram task channels and two WhatsApp invite URLs too.
5. Start the bot:

   ```powershell
   python bot.py
   ```

## Commands

- `/start` shows the welcome message and channel membership gate.
- `/claim` claims the daily naira reward.
- The menu shows claiming, profile, withdrawal status, tasks, and referrals after membership is verified.

Rewards and withdrawal limits are configured in naira with `DAILY_REWARD_NAIRA`,
`MIN_WITHDRAWAL_NAIRA`, and `MAX_WITHDRAWAL_NAIRA`.

Each verified Telegram task awards `CHANNEL_TASK_REWARD_NAIRA` (₦500 by default).
Telegram membership can be checked and a task reward is reversed if a user leaves.
WhatsApp tasks are link/share-only because WhatsApp membership cannot be verified by
the Telegram bot. Referral rewards are issued after all configured Telegram tasks are complete.

The withdrawal button currently displays the user's available balance. Payout processing
will need a configured payout method and an admin workflow before it can be enabled.

Claims are stored in SQLite and are protected by a unique `(telegram_id, claim_date)` key.
