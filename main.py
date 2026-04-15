"""
Subscription Bot — Entry Point
================================
Runs two services on the same asyncio event loop:

  1. Telegram bot (long-polling)
       /start   — subscribe (shows Buy button when FREE_ACCESS=false)
       /stop    — unsubscribe
       /status  — check subscription + expiry
       /buy     — payment method menu (Stars / Crypto / Manual)
       /stats   — admin: subscriber counts
       /approve — admin: approve a pending payment
       /deny    — admin: deny a pending payment
       /pending — admin: list pending payments

  2. aiohttp HTTP server
       POST /broadcast  { "message": "<HTML>" }  → fan-out to paid subscribers
       GET  /health                               → health check + subscriber count

Environment variables (set in .env or Railway dashboard):
  SUB_BOT_TOKEN        Telegram bot token (required)
  SUB_BOT_API_KEY      Shared secret for /broadcast endpoint
  PORT                 HTTP port — set automatically by Railway (default: 8080)
  SUB_BOT_HOST         HTTP bind address  (default: 0.0.0.0)
  ADMIN_CHAT_ID        Telegram chat-id for admin commands + payment notifications
  FREE_ACCESS          true = no payment needed  (default: false)
  SUB_PRICE_STARS      Stars per subscription period  (default: 100)
  SUB_DURATION_DAYS    Days per period               (default: 30)
  CRYPTO_PRICE_USD     USD price shown for crypto     (default: 5)
  CRYPTO_BTC_ADDRESS   BTC wallet address
  CRYPTO_ETH_ADDRESS   ETH wallet address
  CRYPTO_USDT_ADDRESS  USDT TRC-20 wallet address
  CRYPTO_SOL_ADDRESS   SOL wallet address
  CHANNEL_NAME         Display name shown in messages (default: Futures Signals)
  CHANNEL_ID           Telegram channel ID for access gating, e.g. -1001234567890
  DB_PATH              SQLite path  (default: subscribers.db)
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

from aiohttp import web
from dotenv import load_dotenv
from telegram import BotCommand
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)

from src.db import init_db
from src.handlers import cmd_start, cmd_stop, cmd_status, cmd_stats, cb_get_access, cb_my_status
from src.payments import (
    cmd_buy,
    cb_buy_stars,
    cb_buy_crypto,
    cb_buy_btc, cb_buy_eth, cb_buy_usdt, cb_buy_sol,
    cb_confirm_btc, cb_confirm_eth, cb_confirm_usdt, cb_confirm_sol,
    cb_approve_payment, cb_deny_payment,
    pre_checkout,
    successful_payment,
)
from src.admin import cmd_approve, cmd_deny, cmd_pending
from src.channel import handle_join_request, kick_expired_trials
from src.server import make_app


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging():
    Path("logs").mkdir(exist_ok=True)
    fmt      = "%(asctime)s | %(levelname)-8s | %(message)s"
    date_fmt = "%Y-%m-%d %H:%M:%S"

    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        datefmt=date_fmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("logs/subbot.log", encoding="utf-8"),
        ],
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run():
    setup_logging()
    load_dotenv()
    logger = logging.getLogger("subbot")

    token   = os.getenv("SUB_BOT_TOKEN", "").strip()
    api_key = os.getenv("SUB_BOT_API_KEY", "").strip()
    host    = os.getenv("SUB_BOT_HOST", "0.0.0.0")
    port    = int(os.getenv("PORT") or os.getenv("SUB_BOT_PORT") or "8080")

    if not token:
        logger.error("SUB_BOT_TOKEN is not set — exiting.")
        sys.exit(1)

    init_db()
    logger.info("Database ready")

    # ------------------------------------------------------------------
    # Telegram application
    # ------------------------------------------------------------------
    tg_app = Application.builder().token(token).build()

    # User commands
    tg_app.add_handler(CommandHandler("start",  cmd_start))
    tg_app.add_handler(CommandHandler("stop",   cmd_stop))
    tg_app.add_handler(CommandHandler("status", cmd_status))
    tg_app.add_handler(CommandHandler("buy",    cmd_buy))
    tg_app.add_handler(CommandHandler("stats",  cmd_stats))

    # Admin commands
    tg_app.add_handler(CommandHandler("approve", cmd_approve))
    tg_app.add_handler(CommandHandler("deny",    cmd_deny))
    tg_app.add_handler(CommandHandler("pending", cmd_pending))

    # Navigation button callbacks
    tg_app.add_handler(CallbackQueryHandler(cb_get_access,  pattern="^get_access$"))
    tg_app.add_handler(CallbackQueryHandler(cb_my_status,   pattern="^my_status$"))

    # Payment method menu callbacks
    tg_app.add_handler(CallbackQueryHandler(cmd_buy,        pattern="^buy$"))
    tg_app.add_handler(CallbackQueryHandler(cb_buy_stars,   pattern="^buy_stars$"))
    tg_app.add_handler(CallbackQueryHandler(cb_buy_crypto,  pattern="^buy_crypto$"))
    tg_app.add_handler(CallbackQueryHandler(cb_buy_btc,     pattern="^buy_btc$"))
    tg_app.add_handler(CallbackQueryHandler(cb_buy_eth,     pattern="^buy_eth$"))
    tg_app.add_handler(CallbackQueryHandler(cb_buy_usdt,    pattern="^buy_usdt$"))
    tg_app.add_handler(CallbackQueryHandler(cb_buy_sol,     pattern="^buy_sol$"))
    tg_app.add_handler(CallbackQueryHandler(cb_confirm_btc,  pattern="^confirm_btc$"))
    tg_app.add_handler(CallbackQueryHandler(cb_confirm_eth,  pattern="^confirm_eth$"))
    tg_app.add_handler(CallbackQueryHandler(cb_confirm_usdt, pattern="^confirm_usdt$"))
    tg_app.add_handler(CallbackQueryHandler(cb_confirm_sol,  pattern="^confirm_sol$"))
    tg_app.add_handler(CallbackQueryHandler(cb_approve_payment, pattern="^approve_pay:"))
    tg_app.add_handler(CallbackQueryHandler(cb_deny_payment,    pattern="^deny_pay:"))

    # Channel join requests
    tg_app.add_handler(ChatJoinRequestHandler(handle_join_request))

    # Hourly job: kick users whose free trial has expired
    async def _kick_job(ctx: ContextTypes.DEFAULT_TYPE):
        await kick_expired_trials(ctx.bot)

    tg_app.job_queue.run_repeating(_kick_job, interval=3600, first=60)

    # Stars payment flow
    tg_app.add_handler(PreCheckoutQueryHandler(pre_checkout))
    tg_app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))

    await tg_app.initialize()

    # Set bot command menu (shown when user taps the "/" button)
    await tg_app.bot.set_my_commands([
        BotCommand("start",  "Join channel / check trial"),
        BotCommand("status", "Check your subscription status"),
        BotCommand("buy",    "Subscribe or extend subscription"),
        BotCommand("stop",   "Unsubscribe"),
    ])

    await tg_app.start()
    await tg_app.updater.start_polling(drop_pending_updates=True)
    logger.info("Telegram bot polling started")

    # ------------------------------------------------------------------
    # HTTP server
    # ------------------------------------------------------------------
    web_app = make_app(tg_app.bot, api_key)
    runner  = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info(f"HTTP server listening on {host}:{port}")

    # ------------------------------------------------------------------
    # Run until interrupted
    # ------------------------------------------------------------------
    stop_event = asyncio.Event()

    loop = asyncio.get_running_loop()
    try:
        import signal as _signal
        loop.add_signal_handler(_signal.SIGINT,  lambda: stop_event.set())
        loop.add_signal_handler(_signal.SIGTERM, lambda: stop_event.set())
    except NotImplementedError:
        pass  # Windows — Ctrl+C raises KeyboardInterrupt instead

    logger.info("Subscription bot is running. Press Ctrl+C to stop.")
    try:
        await stop_event.wait()
    except (KeyboardInterrupt, SystemExit):
        pass

    # ------------------------------------------------------------------
    # Graceful shutdown
    # ------------------------------------------------------------------
    logger.info("Shutting down...")
    await tg_app.updater.stop()
    await tg_app.stop()
    await tg_app.shutdown()
    await runner.cleanup()
    logger.info("Shutdown complete.")


if __name__ == "__main__":
    asyncio.run(run())
