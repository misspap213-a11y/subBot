"""
Admin commands — only usable by ADMIN_CHAT_ID
----------------------------------------------
  /approve <chat_id> [days]  — activate subscription for a user
  /deny    <chat_id>         — reject a pending payment request
  /pending                   — list all unresolved payment requests
"""

import logging
import os

from telegram import Update
from telegram.ext import ContextTypes

from .db import set_paid, subscribe, resolve_pending, get_pending_all, get_pending_for

logger  = logging.getLogger("subbot.admin")
LINE    = "━" * 28
DLINE   = "─" * 28

ADMIN_ID      = int(os.getenv("ADMIN_CHAT_ID", "0"))
DURATION_DAYS = int(os.getenv("SUB_DURATION_DAYS", "30"))


def _is_admin(chat_id: int) -> bool:
    return ADMIN_ID and chat_id == ADMIN_ID


# ------------------------------------------------------------------
# /approve <chat_id> [days]
# ------------------------------------------------------------------

async def cmd_approve(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not _is_admin(chat_id):
        await update.message.reply_html("⛔ Admin only.")
        return

    args = ctx.args
    if not args:
        await update.message.reply_html("Usage: /approve <chat_id> [days]")
        return

    try:
        target_id = int(args[0])
        days      = int(args[1]) if len(args) > 1 else DURATION_DAYS
    except ValueError:
        await update.message.reply_html("⚠️ Invalid chat_id or days value.")
        return

    # Activate subscription
    subscribe(target_id, "", "")
    expiry     = set_paid(target_id, days)
    expiry_str = expiry.strftime("%d %b %Y")
    resolve_pending(target_id, "approved")

    # Notify the user
    try:
        await ctx.bot.send_message(
            chat_id=target_id,
            text=(
                f"🎉 <b>Subscription Activated!</b>\n"
                f"{LINE}\n"
                f"✅ Your payment has been confirmed.\n"
                f"📅 Active until: <b>{expiry_str}</b>\n"
                f"{LINE}\n"
                f"You'll now receive all live trading signals here.\n"
                f"Use /status to check your subscription anytime."
            ),
            parse_mode="HTML",
        )
        user_notified = "✅ User notified."
    except Exception as e:
        logger.warning(f"Could not notify {target_id}: {e}")
        user_notified = "⚠️ Could not notify user (they may not have started the bot)."

    await update.message.reply_html(
        f"✅ <b>Approved</b>  (<code>{target_id}</code>)\n"
        f"Subscription active for <b>{days} days</b> until {expiry_str}.\n"
        f"{user_notified}"
    )
    logger.info(f"Admin approved {target_id} for {days} days")


# ------------------------------------------------------------------
# /deny <chat_id>
# ------------------------------------------------------------------

async def cmd_deny(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not _is_admin(chat_id):
        await update.message.reply_html("⛔ Admin only.")
        return

    args = ctx.args
    if not args:
        await update.message.reply_html("Usage: /deny <chat_id>")
        return

    try:
        target_id = int(args[0])
    except ValueError:
        await update.message.reply_html("⚠️ Invalid chat_id.")
        return

    resolve_pending(target_id, "denied")

    # Notify the user
    try:
        await ctx.bot.send_message(
            chat_id=target_id,
            text=(
                "❌ <b>Payment Not Confirmed</b>\n"
                "We could not verify your payment.\n\n"
                "If you believe this is a mistake, please contact the admin.\n"
                "Use /buy to try again."
            ),
            parse_mode="HTML",
        )
        user_notified = "✅ User notified."
    except Exception as e:
        logger.warning(f"Could not notify {target_id}: {e}")
        user_notified = "⚠️ Could not notify user."

    await update.message.reply_html(
        f"❌ <b>Denied</b>  (<code>{target_id}</code>)\n{user_notified}"
    )
    logger.info(f"Admin denied {target_id}")


# ------------------------------------------------------------------
# /pending
# ------------------------------------------------------------------

async def cmd_pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not _is_admin(chat_id):
        await update.message.reply_html("⛔ Admin only.")
        return

    requests = get_pending_all()
    if not requests:
        await update.message.reply_html("✅ No pending payment requests.")
        return

    lines = []
    for r in requests:
        username_str = f"@{r['username']}" if r["username"] else f"ID {r['chat_id']}"
        ts = r["requested_at"][:16].replace("T", " ")
        lines.append(
            f"{username_str}  (<code>{r['chat_id']}</code>)\n"
            f"  Method: <b>{r['method']}</b>  ·  {ts} UTC\n"
            f"  /approve {r['chat_id']}  |  /deny {r['chat_id']}"
        )

    await update.message.reply_html(
        f"📋 <b>Pending Payments ({len(requests)})</b>\n"
        f"{LINE}\n" +
        f"\n{DLINE}\n".join(lines)
    )
