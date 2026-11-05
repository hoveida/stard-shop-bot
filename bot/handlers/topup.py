"""شارژ کیف پول با کارت‌به‌کارت: مبلغ ← (کارت تأییدشده) ← رسید ← تأیید مدیر.

با احراز هویت روشن: «شارژ فقط با کارت تأییدشده» می‌پرسد با کدام کارت تأییدشده واریز شده (در رسید مدیر ماسک‌شده)،
و سقف شارژ روزانه‌ی کاربران تأییدنشده/تأییدشده اعمال می‌شود (به وقت تهران، داخل تراکنش ثبت رسید).
"""
from __future__ import annotations

import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..admins import Admins
from ..db import Database, TopupLimitExceeded, User
from ..kyc import Kyc, day_start_utc, mask
from ..locks import RateLimiter, allow
from ..pricing import fmt_toman, to_int
from ..ui import BTN_TOPUP, cancel_menu, main_menu, topup_review_menu
from .kyc import KYC_HOME

log = logging.getLogger(__name__)
router = Router(name="topup")

DEFAULT_MIN_TOPUP = 10_000
MAX_TOPUP = 500_000_000
PAY_CARD = "tpc:"


class Topup(StatesGroup):
    amount = State()
    card = State()
    receipt = State()


async def _min_topup(db: Database) -> int:
    v = to_int(await db.get_setting("min_topup"))
    return v if v else DEFAULT_MIN_TOPUP


def _kyc_button():
    b = InlineKeyboardBuilder()
    b.button(text="🪪 احراز هویت", callback_data=KYC_HOME)
    return b.as_markup()


@router.message(F.text == BTN_TOPUP)
async def topup_start(message: Message, state: FSMContext, db: Database, user: User):
    card = await db.get_setting("card_number")
    if not card:
        await message.answer("⚠️ شارژ کیف پول هنوز توسط مدیر فعال نشده است.")
        return
    kyc = Kyc(db)
    cfg = await kyc.config()
    if cfg["enabled"] and cfg["require_card"] and not await kyc.approved_cards(user.id):
        await message.answer("🪪 برای شارژ کیف پول اول باید کارت بانکی‌تان را احراز هویت کنید.\n"
                             "بعد از تأیید، فقط با همان کارت واریز کنید.", reply_markup=_kyc_button())
        return
    limit = await kyc.daily_limit(user.id, cfg)
    note = ""
    if limit:
        left = max(0, limit - await kyc.used_today(user.id))
        verified = await kyc.is_verified(user.id)
        if left <= 0:
            await message.answer(f"⛔️ سقف شارژ امروز شما ({fmt_toman(limit)}) پر شده است. فردا دوباره تلاش کنید."
                                 + ("" if verified else "\n🪪 با احراز هویت سقف بالاتری می‌گیرید."),
                                 reply_markup=None if verified else _kyc_button())
            return
        note = f"\n📊 باقی‌مانده‌ی سقف امروز: {fmt_toman(left)}"
    min_amount = await _min_topup(db)
    await state.set_state(Topup.amount)
    await message.answer(f"💳 مبلغ شارژ را به <b>تومان</b> بفرستید (حداقل {fmt_toman(min_amount)}):{note}",
                         reply_markup=cancel_menu())


async def _ask_receipt(message: Message, state: FSMContext, db: Database, amount: int, paid_from: str = ""):
    card = await db.get_setting("card_number", "")
    holder = await db.get_setting("card_holder", "")
    await state.set_state(Topup.receipt)
    await message.answer(
        f"مبلغ <b>{fmt_toman(amount)}</b> را{paid_from} به کارت زیر واریز کنید:\n\n"
        f"💳 <code>{escape(card)}</code>\n"
        f"👤 به نام: {escape(holder)}\n\n"
        "📸 سپس <b>عکس رسید</b> را همین‌جا بفرستید.")


@router.message(Topup.amount)
async def topup_amount(message: Message, state: FSMContext, db: Database, user: User):
    amount = to_int(message.text)
    min_amount = await _min_topup(db)
    if amount is None or not min_amount <= amount <= MAX_TOPUP:
        await message.answer(f"❗️ یک عدد بین {fmt_toman(min_amount)} و {fmt_toman(MAX_TOPUP)} بفرستید.")
        return
    kyc = Kyc(db)
    cfg = await kyc.config()
    limit = await kyc.daily_limit(user.id, cfg)
    if limit:
        left = max(0, limit - await kyc.used_today(user.id))
        if amount > left:
            await message.answer(f"❗️ سقف شارژ روزانه‌ی شما {fmt_toman(limit)} است؛ امروز حداکثر "
                                 f"{fmt_toman(left)} دیگر می‌توانید شارژ کنید. مبلغ کمتری بفرستید.")
            return
    await state.update_data(amount=amount)
    if cfg["enabled"] and cfg["require_card"]:
        cards = await kyc.approved_cards(user.id)
        if not cards:
            await state.clear()
            await message.answer("🪪 کارت تأییدشده‌ای ندارید.", reply_markup=_kyc_button())
            return
        b = InlineKeyboardBuilder()
        for c in cards:
            b.button(text=f"💳 {mask(c['card_number'])}", callback_data=f"{PAY_CARD}{c['id']}")
        b.adjust(1)
        await state.set_state(Topup.card)
        await message.answer("💳 با کدام کارت <b>تأییدشده</b> واریز می‌کنید؟ واریز از کارت دیگر تأیید نمی‌شود.",
                             reply_markup=b.as_markup())
        return
    await _ask_receipt(message, state, db, amount)


@router.callback_query(Topup.card, F.data.startswith(PAY_CARD))
async def topup_card(cb: CallbackQuery, state: FSMContext, db: Database, user: User):
    cid = to_int(cb.data[len(PAY_CARD):])
    card = await Kyc(db).get(cid) if cid else None
    amount = (await state.get_data()).get("amount")
    if card is None or card["user_id"] != user.id or card["status"] != "approved" or not amount:
        await cb.answer("این کارت تأییدشده نیست.", show_alert=True)
        return
    await state.update_data(card_id=card["id"])
    await cb.answer()
    await _ask_receipt(cb.message, state, db, amount, f" از کارت <code>{mask(card['card_number'])}</code>")


@router.message(Topup.card)
async def topup_card_invalid(message: Message):
    await message.answer("💳 یکی از کارت‌های بالا را انتخاب کنید (یا «❌ انصراف»).")


@router.message(Topup.receipt, F.photo)
async def topup_receipt(message: Message, state: FSMContext, db: Database, bot: Bot, user: User,
                        admins: Admins, is_admin: bool, limiter: RateLimiter | None = None):
    data = await state.get_data()
    amount = data.get("amount")
    if not await allow(limiter, "topup", user.id):
        await message.answer("⏳ تعداد درخواست‌های شارژ در این ساعت زیاد است؛ بعداً دوباره بفرستید.")
        return
    if not amount:
        await state.clear()
        return
    await state.clear()
    kyc = Kyc(db)
    cfg = await kyc.config()
    card = await kyc.get(data["card_id"]) if data.get("card_id") else None
    if cfg["enabled"] and cfg["require_card"] and (card is None or card["status"] != "approved"):
        await message.answer("🪪 کارت تأییدشده انتخاب نشده؛ دوباره «💳 شارژ کیف پول» را بزنید.",
                             reply_markup=main_menu(is_admin))
        return
    limit = await kyc.daily_limit(user.id, cfg)
    photo_id = message.photo[-1].file_id
    try:
        tid, created = await db.create_topup(user.id, amount, photo_id, message.photo[-1].file_unique_id,
                                             card_id=card["id"] if card else None,
                                             daily_limit=(limit, day_start_utc()) if limit else None)
    except TopupLimitExceeded as e:
        await message.answer(f"⛔️ سقف شارژ امروز شما ({fmt_toman(e.limit)}) پر شده است؛ باقی‌مانده: "
                             f"{fmt_toman(e.remaining)}.", reply_markup=main_menu(is_admin))
        return
    if not created:
        await message.answer(f"ℹ️ این رسید قبلاً با شماره‌ی #{tid} ثبت شده است و در حال بررسی است.",
                             reply_markup=main_menu(is_admin))
        return
    await message.answer(
        f"✅ رسید شما (درخواست #{tid}) ثبت شد و پس از بررسی، موجودی شارژ می‌شود.",
        reply_markup=main_menu(is_admin))
    uname = f"@{escape(user.username)}" if user.username else "—"
    paid = f"\n💳 از کارت: <code>{mask(card['card_number'])}</code> ({escape(card['holder'])})" if card else ""
    caption = (f"💳 <b>درخواست شارژ #{tid}</b>\n"
               f"👤 {escape(user.first_name or '')} {uname}\n🆔 <code>{user.id}</code>\n"
               f"💵 مبلغ: <b>{fmt_toman(amount)}</b>{paid}")
    for aid in admins.all():
        try:
            await bot.send_photo(aid, photo_id, caption=caption, reply_markup=topup_review_menu(tid))
        except Exception as e:  # مدیر ربات را استارت نکرده یا بلاک کرده
            log.warning("cannot notify admin %s: %s", aid, e)


@router.message(Topup.receipt)
async def topup_receipt_invalid(message: Message):
    await message.answer("📸 لطفاً عکس رسید را بفرستید (یا «❌ انصراف»).")
