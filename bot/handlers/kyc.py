"""🪪 احراز هویت: کاربر کارت و عکس می‌فرستد؛ مدیرها با یک دکمه تأیید/رد می‌کنند (اتمیک، یک بار)."""
from __future__ import annotations

import contextlib
import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..admins import Admins
from ..db import Database, Row, User
from ..kyc import PHOTO_MODES, STATUS, Kyc, KycError, fmt_card, luhn_ok, mask, normalize_card, today_jalali
from ..locks import RateLimiter, allow
from ..ui import Adm, cancel_menu, edit_or_send, main_menu

log = logging.getLogger(__name__)
router = Router(name="kyc")

KYC_HOME, KYC_NEW = "kyc:home", "kyc:new"


class KycForm(StatesGroup):
    card = State()
    holder = State()
    photo = State()


# ---------- مشترک ----------
def review_menu(kid: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✅ تایید", callback_data=Adm(name="kyc_ok", arg=str(kid)))
    b.button(text="❌ رد", callback_data=Adm(name="kyc_no", arg=str(kid)))
    b.adjust(2)
    return b.as_markup()


async def admin_caption(kyc: Kyc, row: Row) -> str:
    """متن عکس برای مدیر (شماره‌ی کامل کارت فقط اینجا)."""
    u = await kyc.db.get_user(row["user_id"])
    uname = f"@{escape(u.username)}" if u and u.username else "—"
    others = await kyc.card_owners(row["card_number"], row["user_id"])
    warn = ("\n⚠️ همین کارت برای کاربر(ان) دیگر هم ثبت شده: " + ", ".join(f"<code>{x}</code>" for x in others[:5])
            if others else "")
    text = (f"🪪 <b>احراز هویت #{row['id']}</b>\n"
            f"👤 {escape((u.first_name if u else '') or '')} {uname}\n🆔 <code>{row['user_id']}</code>\n"
            f"💳 <code>{fmt_card(row['card_number'])}</code>\n👤 به نام: {escape(row['holder'])}\n"
            f"📸 {PHOTO_MODES.get(row['photo_mode'], row['photo_mode'])}\n"
            f"📅 {row['created_at'][:16].replace('T', ' ')}{warn}")
    if row["status"] == "approved":
        text += f"\n\n✅ تأیید شد توسط <code>{row['admin_id']}</code>"
    elif row["status"] == "rejected":
        text += f"\n\n❌ رد شد توسط <code>{row['admin_id']}</code>\nدلیل: {escape(row['reason'] or '—')}"
    return text


async def send_to_reviewers(bot: Bot, kyc: Kyc, admins: Admins, row: Row, only: list[int] | None = None) -> int:
    cfg = await kyc.config()
    caption = await admin_caption(kyc, row)
    sent = 0
    for aid in only if only is not None else kyc.reviewers(admins, cfg):
        try:
            m = await bot.send_photo(aid, row["photo_id"], caption=caption, reply_markup=review_menu(row["id"]))
        except Exception as e:  # مدیر ربات را استارت نکرده یا بلاک کرده
            log.warning("cannot send kyc %s to admin %s: %s", row["id"], aid, e)
            continue
        await kyc.add_message(row["id"], aid, m.message_id)
        sent += 1
    return sent


async def finalize(bot: Bot, kyc: Kyc, row: Row) -> None:
    """بعد از تصمیم: نسخه‌ی همه‌ی مدیرها ویرایش و دکمه‌ها حذف می‌شود، و به کاربر خبر داده می‌شود."""
    caption = await admin_caption(kyc, row)
    for m in await kyc.messages(row["id"]):
        with contextlib.suppress(Exception):
            await bot.edit_message_caption(chat_id=m["chat_id"], message_id=m["message_id"], caption=caption,
                                           reply_markup=None)
    card = mask(row["card_number"])
    if row["status"] == "approved":
        text = f"✅ <b>احراز هویت شما تأیید شد</b>\n💳 کارت <code>{card}</code> تأیید شد."
        markup = None
    else:
        text = (f"❌ <b>احراز هویت کارت <code>{card}</code> رد شد</b>\nدلیل: {escape(row['reason'] or '—')}\n\n"
                "می‌توانید مدارک را دوباره بفرستید.")
        b = InlineKeyboardBuilder()
        b.button(text="🔁 ارسال دوباره", callback_data=KYC_NEW)
        markup = b.as_markup()
    from ..notify import safe_send
    await safe_send(bot, row["user_id"], text, markup)


def photo_instruction(mode: str, bot_username: str | None) -> str:
    name = f"@{bot_username}" if bot_username else "نام ربات"
    if mode == "selfie":
        return ("🤳 یک <b>سلفی</b> بفرستید که کارت را کنار صورتتان در دست دارید؛ "
                "چهره، شماره‌ی کارت و نام روی کارت باید خوانا باشد.")
    return (f"📝 روی یک برگه با دست بنویسید: <b>{escape(name)}</b> و تاریخ امروز <b>{today_jalali()}</b>، "
            "کارت را کنار برگه بگذارید و از هر دو یک <b>عکس</b> واضح بفرستید.")


# ---------- کاربر ----------
async def kyc_view(kyc: Kyc, uid: int) -> tuple[str, InlineKeyboardMarkup]:
    status, _ = await kyc.status(uid)
    rows = await kyc.cards(uid)
    lines = ["🪪 <b>احراز هویت</b>\n", f"وضعیت: <b>{STATUS[status]}</b>"]
    if rows:
        lines.append("\n<b>کارت‌های شما</b>")
        for r in rows[:10]:
            reason = f"\n   دلیل: {escape(r['reason'])}" if r["status"] == "rejected" and r["reason"] else ""
            lines.append(f"• <code>{mask(r['card_number'])}</code> — {STATUS.get(r['status'], r['status'])}{reason}")
    else:
        lines.append("\nبا ثبت و تأیید کارت بانکی، شارژ کیف پول سریع‌تر و با سقف بالاتر انجام می‌شود.")
    b = InlineKeyboardBuilder()
    if status != "pending" and not any(r["status"] == "pending" for r in rows):
        b.button(text="🔁 ارسال دوباره" if status == "rejected" else "➕ ثبت کارت" if status == "none"
                 else "➕ ثبت کارت دیگر", callback_data=KYC_NEW)
    b.adjust(1)
    return "\n".join(lines), b.as_markup()


@router.message(Command("kyc"))
@router.callback_query(F.data == KYC_HOME)
async def kyc_home(event: Message | CallbackQuery, state: FSMContext, db: Database, user: User):
    kyc = Kyc(db)
    msg = event if isinstance(event, Message) else event.message
    if not (await kyc.config())["enabled"]:
        if isinstance(event, CallbackQuery):
            await event.answer("احراز هویت فعلاً فعال نیست.", show_alert=True)
        else:
            await event.answer("🪪 احراز هویت فعلاً فعال نیست.")
        return
    await state.clear()
    text, kb = await kyc_view(kyc, user.id)
    if isinstance(event, CallbackQuery):
        await edit_or_send(msg, text, kb)
        await event.answer()
    else:
        await msg.answer(text, reply_markup=kb)


@router.callback_query(F.data == KYC_NEW)
async def kyc_new(cb: CallbackQuery, state: FSMContext, db: Database, user: User):
    kyc = Kyc(db)
    if not (await kyc.config())["enabled"]:
        await cb.answer("احراز هویت فعلاً فعال نیست.", show_alert=True)
        return
    if (await kyc.status(user.id))[0] == "pending" or any(r["status"] == "pending" for r in await kyc.cards(user.id)):
        await cb.answer("⏳ درخواست قبلی شما هنوز در حال بررسی است.", show_alert=True)
        return
    await state.set_state(KycForm.card)
    await cb.message.answer("💳 شماره‌ی ۱۶ رقمی کارت بانکی خودتان را بفرستید:", reply_markup=cancel_menu())
    await cb.answer()


@router.message(KycForm.card, F.text)
async def kyc_card(message: Message, state: FSMContext):
    card = normalize_card(message.text)
    if card is None:
        await message.answer("❗️ شماره کارت باید ۱۶ رقم باشد. دوباره بفرستید:")
        return
    if not luhn_ok(card):
        await message.answer("❗️ این شماره کارت معتبر نیست (رقم کنترلی اشتباه است). دوباره بررسی کنید:")
        return
    await state.update_data(card=card)
    await state.set_state(KycForm.holder)
    await message.answer(f"✅ کارت <code>{mask(card)}</code>\n👤 نام و نام خانوادگی صاحب کارت را بفرستید:")


@router.message(KycForm.holder, F.text)
async def kyc_holder(message: Message, state: FSMContext, db: Database, bot: Bot):
    holder = " ".join(message.text.split())
    if not 3 <= len(holder) <= 64 or any(ch.isdigit() for ch in holder) or holder.startswith("/"):
        await message.answer("❗️ نام صاحب کارت را درست بفرستید (۳ تا ۶۴ حرف، بدون عدد):")
        return
    mode = (await Kyc(db).config())["mode"]
    await state.update_data(holder=holder, mode=mode)
    await state.set_state(KycForm.photo)
    me = await bot.me()
    await message.answer(photo_instruction(mode, me.username))


@router.message(KycForm.photo, F.photo)
async def kyc_photo(message: Message, state: FSMContext, db: Database, bot: Bot, user: User, admins: Admins,
                    is_admin: bool, limiter: RateLimiter | None = None):
    data = await state.get_data()
    if not data.get("card") or not data.get("holder"):
        await state.clear()
        return
    if not await allow(limiter, "kyc", user.id):
        await message.answer("⏳ تعداد ارسال‌ها در این ساعت زیاد است؛ بعداً دوباره تلاش کنید.")
        return
    await state.clear()
    kyc = Kyc(db)
    p = message.photo[-1]
    try:
        kid = await kyc.submit(user.id, data["card"], data["holder"], p.file_id, p.file_unique_id,
                               data.get("mode") or "note")
    except KycError as e:
        await message.answer(f"ℹ️ {e}", reply_markup=main_menu(is_admin))
        return
    await message.answer(f"✅ مدارک شما (درخواست #{kid}) ثبت شد و پس از بررسی نتیجه اعلام می‌شود.\n"
                         f"💳 کارت: <code>{mask(data['card'])}</code>", reply_markup=main_menu(is_admin))
    await send_to_reviewers(bot, kyc, admins, await kyc.get(kid))


@router.message(KycForm.photo)
@router.message(KycForm.card)
@router.message(KycForm.holder)
async def kyc_invalid(message: Message, state: FSMContext):
    st = await state.get_state()
    await message.answer("📸 لطفاً عکس را بفرستید (یا «❌ انصراف»)." if st == KycForm.photo.state
                         else "✍️ لطفاً متن بفرستید (یا «❌ انصراف»).")
