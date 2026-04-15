"""
Multi-method payment handlers
--------------------------------
Methods:
  1. Telegram Stars  — native, instant, no admin needed
  2. Crypto          — show wallet address → user confirms → admin approves
  3. Manual          — user contacts admin → admin approves

Callback data map:
  buy              → payment method menu
  buy_stars        → send Stars invoice
  buy_crypto       → coin selection menu
  buy_btc          → show BTC wallet
  buy_eth          → show ETH wallet
  buy_usdt         → show USDT (TRC-20) wallet
  buy_sol          → show SOL wallet
  confirm_btc etc. → user says "I've paid" → admin notified
  buy_manual       → manual instructions + admin notified

Environment variables:
  SUB_PRICE_STARS      Stars per period            (default: 100)
  SUB_DURATION_DAYS    Days per period             (default: 30)
  CRYPTO_PRICE_USD     USD price shown for crypto  (default: 5)
  CRYPTO_BTC_ADDRESS   BTC wallet address
  CRYPTO_ETH_ADDRESS   ETH wallet address
  CRYPTO_USDT_ADDRESS  USDT TRC-20 wallet address
  CRYPTO_SOL_ADDRESS   SOL wallet address
  ADMIN_CHAT_ID        Admin telegram id for notifications
"""

import logging
import os
from datetime import timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Update
from telegram.ext import ContextTypes

from .db import subscribe, set_paid, get_expiry, add_pending, resolve_pending
from .channel import grant_access

logger = logging.getLogger("subbot.payments")

# Config
PRICE_STARS   = int(os.getenv("SUB_PRICE_STARS",   "1000"))
DURATION_DAYS = int(os.getenv("SUB_DURATION_DAYS", "30"))
PRICE_USD     = os.getenv("CRYPTO_PRICE_USD", "20")
ADMIN_ID      = int(os.getenv("ADMIN_CHAT_ID", "0"))

WALLETS = {
    "btc":  ("₿ Bitcoin (BTC)",      os.getenv("CRYPTO_BTC_ADDRESS",   "")),
    "eth":  ("💎 Ethereum (ETH)",     os.getenv("CRYPTO_ETH_ADDRESS",   "")),
    "usdt": ("💵 USDT (TRC-20)",      os.getenv("CRYPTO_USDT_ADDRESS",  "")),
    "sol":  ("◎ Solana (SOL)",        os.getenv("CRYPTO_SOL_ADDRESS",   "")),
}

LINE  = "━" * 28
DLINE = "─" * 28


# ------------------------------------------------------------------
# Keyboards
# ------------------------------------------------------------------

def _method_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"⭐ Telegram Stars  ({PRICE_STARS} Stars)", callback_data="buy_stars")],
        [InlineKeyboardButton("₿ Pay with Crypto", callback_data="buy_crypto")],
    ]
    return InlineKeyboardMarkup(rows)


def _crypto_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for coin, (label, addr) in WALLETS.items():
        if addr:  # Only show coins that have a wallet configured
            rows.append([InlineKeyboardButton(label, callback_data=f"buy_{coin}")])
    rows.append([InlineKeyboardButton("← Back", callback_data="buy")])
    return InlineKeyboardMarkup(rows)


def _confirm_keyboard(coin: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ I've Sent Payment", callback_data=f"confirm_{coin}")],
        [InlineKeyboardButton("← Back", callback_data="buy_crypto")],
    ])


# ------------------------------------------------------------------
# /buy command — entry point
# ------------------------------------------------------------------

async def cmd_buy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user    = update.effective_user
    chat_id = update.effective_chat.id

    if update.callback_query:
        await update.callback_query.answer()

    subscribe(chat_id, user.username, user.first_name)

    text = (
        f"💳 <b>Subscribe to signals</b>\n"
        f"{LINE}\n"
        f"📅 Duration: <b>{DURATION_DAYS} days</b>\n"
        f"💵 Crypto price: <b>${PRICE_USD} USD</b>\n"
        f"⭐ Stars price: <b>{PRICE_STARS} Stars</b>\n"
        f"{LINE}\n"
        f"Choose your payment method 👇"
    )

    if update.callback_query:
        await update.callback_query.edit_message_text(
            text, parse_mode="HTML", reply_markup=_method_keyboard()
        )
    else:
        await update.message.reply_html(text, reply_markup=_method_keyboard())


# ------------------------------------------------------------------
# Callback: Stars
# ------------------------------------------------------------------

async def cb_buy_stars(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    user    = query.from_user
    chat_id = query.message.chat_id
    await query.answer()

    subscribe(chat_id, user.username, user.first_name)
    expiry = get_expiry(chat_id)
    action = "Extend" if expiry else "Buy"

    await ctx.bot.send_invoice(
        chat_id=chat_id,
        title=f"📡 {DURATION_DAYS}-Day Signal Subscription",
        description=(
            f"Get {DURATION_DAYS} days of live crypto trading signals "
            f"delivered directly to this chat.\n\n"
            f"• All confirmed signals with TP/SL levels\n"
            f"• EMA, S/R Bounce, Structure Break and more\n"
            f"• Activates instantly after payment"
        ),
        payload=f"sub_{DURATION_DAYS}d",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(f"{action} {DURATION_DAYS} days", PRICE_STARS)],
    )


# ------------------------------------------------------------------
# Callback: Crypto menu
# ------------------------------------------------------------------

async def cb_buy_crypto(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    configured = [label for _, (label, addr) in WALLETS.items() if addr]
    if not configured:
        await query.edit_message_text(
            "⚠️ Crypto payments are not configured yet. Use /buy and choose another method.",
            parse_mode="HTML",
        )
        return

    await query.edit_message_text(
        f"₿ <b>Pay with Crypto</b>\n"
        f"{LINE}\n"
        f"Send <b>${PRICE_USD} USD</b> worth of your chosen coin.\n"
        f"After sending, tap <b>I've Sent Payment</b> to notify us.\n"
        f"{DLINE}\n"
        f"Select a coin 👇",
        parse_mode="HTML",
        reply_markup=_crypto_keyboard(),
    )


# ------------------------------------------------------------------
# Callback: Show specific wallet
# ------------------------------------------------------------------

async def _show_wallet(update: Update, coin: str):
    query = update.callback_query
    await query.answer()

    label, address = WALLETS[coin]
    if not address:
        await query.answer("This coin is not configured yet.", show_alert=True)
        return

    await query.edit_message_text(
        f"{label}\n"
        f"{LINE}\n"
        f"Send <b>${PRICE_USD} USD</b> worth of {label.split()[1]} to:\n\n"
        f"<code>{address}</code>\n\n"
        f"{DLINE}\n"
        f"⚠️ Send only <b>{label.split()[1]}</b> to this address.\n"
        f"After sending, tap the button below 👇",
        parse_mode="HTML",
        reply_markup=_confirm_keyboard(coin),
    )


async def cb_buy_btc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _show_wallet(update, "btc")

async def cb_buy_eth(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _show_wallet(update, "eth")

async def cb_buy_usdt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _show_wallet(update, "usdt")

async def cb_buy_sol(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _show_wallet(update, "sol")


# ------------------------------------------------------------------
# Callback: User confirms crypto payment sent
# ------------------------------------------------------------------

def _admin_action_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"approve_pay:{chat_id}"),
        InlineKeyboardButton("❌ Deny",    callback_data=f"deny_pay:{chat_id}"),
    ]])


async def _confirm_crypto(update: Update, ctx: ContextTypes.DEFAULT_TYPE, coin: str):
    query   = update.callback_query
    user    = query.from_user
    chat_id = query.message.chat_id
    await query.answer()

    label, _ = WALLETS[coin]
    add_pending(chat_id, user.username, f"crypto_{coin}")

    # Notify admin with approve/deny buttons
    if ADMIN_ID:
        username_str = f"@{user.username}" if user.username else f"ID {chat_id}"
        await ctx.bot.send_message(
            chat_id=ADMIN_ID,
            text=(
                f"💰 <b>Crypto Payment Claim</b>\n"
                f"{LINE}\n"
                f"User: {username_str}  (<code>{chat_id}</code>)\n"
                f"Coin: {label}\n"
                f"Amount: ${PRICE_USD} USD\n"
            ),
            parse_mode="HTML",
            reply_markup=_admin_action_keyboard(chat_id),
        )

    await query.edit_message_text(
        f"⏳ <b>Payment claim submitted!</b>\n"
        f"{LINE}\n"
        f"We've been notified of your {label} payment.\n\n"
        f"📎 <b>Please reply to this message with your transaction hash or a screenshot</b> "
        f"so we can verify it faster.\n\n"
        f"<i>You'll receive a confirmation message here once approved.</i>",
        parse_mode="HTML",
    )
    logger.info(f"Crypto payment claimed: {user.username or chat_id} via {coin}")


# ------------------------------------------------------------------
# Admin inline approve / deny callbacks
# ------------------------------------------------------------------

async def cb_approve_payment(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    admin   = query.from_user

    # Only the configured admin can tap this
    if ADMIN_ID and admin.id != ADMIN_ID:
        await query.answer("Not authorised.", show_alert=True)
        return

    target_id = int(query.data.split(":")[1])
    await query.answer("Approving...")

    subscribe(target_id, "", "")
    expiry     = set_paid(target_id, DURATION_DAYS)
    expiry_str = expiry.strftime("%d %b %Y")
    resolve_pending(target_id, "approved")

    # Update the admin message
    original = query.message.text or ""
    await query.edit_message_text(
        original + f"\n\n✅ <b>Approved by you — {expiry_str}</b>",
        parse_mode="HTML",
    )

    # Grant channel access
    await grant_access(ctx.bot, target_id)

    # Thank the user
    try:
        await ctx.bot.send_message(
            chat_id=target_id,
            text=(
                f"🎉 <b>Payment Confirmed!</b>\n"
                f"{LINE}\n"
                f"Thank you! Your subscription is now active.\n"
                f"📅 Valid until: <b>{expiry_str}</b>\n"
                f"{LINE}\n"
                f"Tap below to join the channel."
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📡 Get Channel Link", callback_data="get_access")
            ]]),
        )
    except Exception as e:
        logger.warning(f"Could not notify {target_id} of approval: {e}")

    logger.info(f"Admin approved payment for {target_id}")


async def cb_deny_payment(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    admin   = query.from_user

    if ADMIN_ID and admin.id != ADMIN_ID:
        await query.answer("Not authorised.", show_alert=True)
        return

    target_id = int(query.data.split(":")[1])
    await query.answer("Denied.")

    resolve_pending(target_id, "denied")

    # Update the admin message
    original = query.message.text or ""
    await query.edit_message_text(
        original + "\n\n❌ <b>Denied by you.</b>",
        parse_mode="HTML",
    )

    # Notify the user
    try:
        await ctx.bot.send_message(
            chat_id=target_id,
            text=(
                f"❌ <b>Payment Not Confirmed</b>\n"
                f"{LINE}\n"
                f"We couldn't verify your payment.\n\n"
                f"Please double-check the amount and wallet address, "
                f"then try again with /buy."
            ),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning(f"Could not notify {target_id} of denial: {e}")

    logger.info(f"Admin denied payment for {target_id}")


async def cb_confirm_btc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _confirm_crypto(update, ctx, "btc")

async def cb_confirm_eth(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _confirm_crypto(update, ctx, "eth")

async def cb_confirm_usdt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _confirm_crypto(update, ctx, "usdt")

async def cb_confirm_sol(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _confirm_crypto(update, ctx, "sol")


# ------------------------------------------------------------------
# Stars: Pre-checkout + successful payment (unchanged)
# ------------------------------------------------------------------

async def pre_checkout(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    if query.invoice_payload.startswith("sub_"):
        await query.answer(ok=True)
    else:
        await query.answer(ok=False, error_message="Unknown invoice.")


async def successful_payment(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user    = update.effective_user
    chat_id = update.effective_chat.id
    payment = update.message.successful_payment
    stars   = payment.total_amount
    payload = payment.invoice_payload

    try:
        days = int(payload.split("_")[1].rstrip("d"))
    except (IndexError, ValueError):
        days = DURATION_DAYS

    subscribe(chat_id, user.username, user.first_name)
    expiry     = set_paid(chat_id, days)
    expiry_str = expiry.astimezone(timezone.utc).strftime("%d %b %Y")

    logger.info(f"Stars payment: {user.username or chat_id} — {stars} Stars — expires {expiry_str}")

    await update.message.reply_html(
        f"🎉 <b>Payment Received!</b>\n"
        f"{LINE}\n"
        f"⭐ {stars} Stars received\n"
        f"📅 Active until: <b>{expiry_str}</b>\n"
        f"{LINE}\n"
        f"Granting channel access now..."
    )

    # Grant channel access (approve join request or send invite link)
    await grant_access(ctx.bot, chat_id)
