"""پنل ← 🪪 احراز هویت: تنظیمات، فهرست در انتظار، و تأیید/رد اتمیک روی عکس هر درخواست."""
from __future__ import annotations

from html import escape

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..admins import Admins
from ..db import Database
from ..kyc import PHOTO_MODES, REJECT_REASONS, REVIEWERS, Kyc
from ..pricing import fmt_toman, to_int
from ..ui import Adm, back_admin, cancel_menu, drop_markup, edit_or_send, main_menu
from .filters import IsAdmin
from .kyc import finalize, review_menu, send_to_reviewers

router = Router(name="kyc_admin")
router.message.filter(IsAdmin())
router.callback_query.filter(IsAdmin())

ALREADY = "قبلاً بررسی شده"


class KycAdminForm(StatesGroup):
    reason = State()
    limit = State()


def _lim(v: int) -> str:
    return fmt_toman(v) if v else "بدون سقف"


async def _section(kyc: Kyc):
    cfg = await kyc.config()
    n = await kyc.count_pending()
    text = ("🪪 <b>احراز هویت (KYC)</b>\n\n"
            f"وضعیت: {'🟢 روشن' if cfg['enabled'] else '🔴 خاموش'}\n"
            f"📸 نوع عکس: {PHOTO_MODES[cfg['mode']]}\n"
            f"💳 شارژ فقط با کارت تأییدشده: {'🟢 بله' if cfg['require_card'] else '🔴 خیر'}\n"
            f"⬇️ سقف شارژ روزانه‌ی تأییدنشده‌ها: {_lim(cfg['limit_unverified'])}\n"
            f"⬆️ سقف شارژ روزانه‌ی تأییدشده‌ها: {_lim(cfg['limit_verified'])}\n"
            f"👮 بررسی‌کننده‌ها: {REVIEWERS[cfg['reviewers']]}\n"
            f"⏳ در انتظار بررسی: <b>{n:,}</b>\n\n"
            "کاربر از 👤 حساب کاربری ← 🪪 احراز هویت کارت بانکی (۱۶ رقم با بررسی Luhn) و نام صاحب کارت را ثبت می‌کند "
            "و عکس می‌فرستد. هر درخواست با دکمه‌ی تأیید/رد برای بررسی‌کننده‌ها فرستاده می‌شود؛ اولین تصمیم ثبت و "
            "نسخه‌ی بقیه‌ی مدیرها به‌روز می‌شود. شماره‌ی کامل کارت فقط به مدیرها نشان داده می‌شود.\n"
            "سقف روزانه به وقت تهران و فقط وقتی احراز هویت روشن است اعمال می‌شود.")
    b = InlineKeyboardBuilder()
    b.button(text=f"📋 در انتظار ({n})", callback_data=Adm(name="kyc_list"))
    b.button(text="🔴 خاموش کردن" if cfg["enabled"] else "🟢 روشن کردن", callback_data=Adm(name="kyc", arg="t"))
    b.button(text="📸 تغییر نوع عکس", callback_data=Adm(name="kyc", arg="m"))
    b.button(text="💳 شارژ فقط با کارت تأییدشده", callback_data=Adm(name="kyc", arg="c"))
    b.button(text="⬇️ سقف تأییدنشده‌ها", callback_data=Adm(name="kyc_lim", arg="u"))
    b.button(text="⬆️ سقف تأییدشده‌ها", callback_data=Adm(name="kyc_lim", arg="v"))
    b.button(text="👮 بررسی‌کننده‌ها", callback_data=Adm(name="kyc", arg="r"))
    b.adjust(1, 1, 2, 2, 1)
    return text, back_admin(b)


@router.callback_query(Adm.filter(F.name == "kyc"))
async def cb_kyc(cb: CallbackQuery, callback_data: Adm, db: Database, is_owner: bool):
    kyc = Kyc(db)
    cfg = await kyc.config()
    op = callback_data.arg
    if op == "r" and not is_owner:
        await cb.answer("فقط مالک می‌تواند بررسی‌کننده‌ها را عوض کند.", show_alert=True)
        return
    changes = {"t": {"enabled": not cfg["enabled"]},
               "m": {"mode": "selfie" if cfg["mode"] == "note" else "note"},
               "c": {"require_card": not cfg["require_card"]},
               "r": {"reviewers": "owners" if cfg["reviewers"] == "all" else "all"}}.get(op)
    if changes:
        await kyc.set_config(cb.from_user.id, **changes)
    text, kb = await _section(kyc)
    await edit_or_send(cb.message, text, kb)
    await cb.answer("انجام شد" if changes else None)


@router.callback_query(Adm.filter(F.name == "kyc_lim"))
async def cb_kyc_limit(cb: CallbackQuery, callback_data: Adm, state: FSMContext):
    if callback_data.arg not in ("u", "v"):
        await cb.answer()
        return
    await state.set_state(KycAdminForm.limit)
    await state.update_data(which=callback_data.arg)
    who = "تأییدنشده" if callback_data.arg == "u" else "تأییدشده"
    await cb.message.answer(f"سقف شارژ روزانه برای کاربران {who} را به تومان بفرستید (0 = بدون سقف):",
                            reply_markup=cancel_menu())
    await cb.answer()


@router.message(KycAdminForm.limit, F.text)
async def kyc_limit_value(message: Message, state: FSMContext, db: Database):
    n = to_int(message.text)
    if n is None or n < 0:
        await message.answer("❗️ یک عدد (تومان) بفرستید؛ 0 یعنی بدون سقف.")
        return
    which = (await state.get_data()).get("which")
    await state.clear()
    kyc = Kyc(db)
    await kyc.set_config(message.from_user.id, **{"limit_unverified" if which == "u" else "limit_verified": n})
    await message.answer("✅ سقف ذخیره شد.", reply_markup=main_menu(True))
    text, kb = await _section(kyc)
    await message.answer(text, reply_markup=kb)


@router.callback_query(Adm.filter(F.name == "kyc_list"))
async def cb_kyc_list(cb: CallbackQuery, db: Database, bot: Bot, admins: Admins):
    kyc = Kyc(db)
    if not kyc.can_review(admins, cb.from_user.id, await kyc.config()):
        await cb.answer("بررسی احراز هویت فقط برای مالک‌هاست.", show_alert=True)
        return
    rows = await kyc.pending(20)
    if not rows:
        await cb.answer("درخواست احراز هویت در انتظاری نیست ✅", show_alert=True)
        return
    await cb.answer()
    for r in rows:
        await send_to_reviewers(bot, kyc, admins, r, only=[cb.from_user.id])
    total = await kyc.count_pending()
    if total > len(rows):
        await cb.message.answer(f"… و {total - len(rows):,} درخواست دیگر. بعد از بررسی این‌ها دوباره بزنید.")


async def _decide(cb_or_msg, bot: Bot, kyc: Kyc, kid: int, admin_id: int, approve: bool, reason: str | None):
    row = await kyc.decide(kid, admin_id, approve, reason)
    if row is None:
        return None
    await finalize(bot, kyc, row)
    return row


@router.callback_query(Adm.filter(F.name.in_({"kyc_ok", "kyc_no", "kyc_rr", "kyc_rb", "kyc_rc"})))
async def cb_kyc_review(cb: CallbackQuery, callback_data: Adm, state: FSMContext, db: Database, bot: Bot,
                        admins: Admins):
    kyc = Kyc(db)
    if not kyc.can_review(admins, cb.from_user.id, await kyc.config()):
        await cb.answer("بررسی احراز هویت فقط برای مالک‌هاست.", show_alert=True)
        return
    kid_s, _, idx = callback_data.arg.partition("|")
    kid = to_int(kid_s) or 0
    row = await kyc.get(kid)
    if row is None or row["status"] != "pending":
        await cb.answer(ALREADY, show_alert=True)
        await drop_markup(cb.message)
        return
    name = callback_data.name
    if name == "kyc_no":  # انتخاب دلیل
        b = InlineKeyboardBuilder()
        for i, r in enumerate(REJECT_REASONS):
            b.button(text=r, callback_data=Adm(name="kyc_rr", arg=f"{kid}|{i}"))
        b.button(text="✏️ دلیل دلخواه", callback_data=Adm(name="kyc_rc", arg=str(kid)))
        b.button(text="🔙 بازگشت", callback_data=Adm(name="kyc_rb", arg=str(kid)))
        b.adjust(1)
        with_markup = getattr(cb.message, "edit_reply_markup", None)
        if with_markup:
            await cb.message.edit_reply_markup(reply_markup=b.as_markup())
        await cb.answer("دلیل رد را انتخاب کنید")
        return
    if name == "kyc_rb":
        if getattr(cb.message, "edit_reply_markup", None):
            await cb.message.edit_reply_markup(reply_markup=review_menu(kid))
        await cb.answer()
        return
    if name == "kyc_rc":
        await state.set_state(KycAdminForm.reason)
        await state.update_data(kid=kid)
        await cb.message.answer(f"✏️ دلیل رد درخواست #{kid} را بفرستید (به کاربر نشان داده می‌شود):",
                                reply_markup=cancel_menu())
        await cb.answer()
        return
    approve = name == "kyc_ok"
    reason = None
    if name == "kyc_rr":
        i = to_int(idx)
        if i is None or not 0 <= i < len(REJECT_REASONS):
            await cb.answer()
            return
        reason = REJECT_REASONS[i]
    done = await _decide(cb, bot, kyc, kid, cb.from_user.id, approve, reason)
    if done is None:
        await cb.answer(ALREADY, show_alert=True)
        await drop_markup(cb.message)
        return
    await cb.answer("✅ تأیید شد" if approve else "❌ رد شد")


@router.message(KycAdminForm.reason, F.text)
async def kyc_reason_value(message: Message, state: FSMContext, db: Database, bot: Bot):
    kid = (await state.get_data()).get("kid")
    reason = " ".join(message.text.split())[:200]
    if len(reason) < 3:
        await message.answer("❗️ دلیل را کامل‌تر بنویسید.")
        return
    await state.clear()
    kyc = Kyc(db)
    done = await _decide(message, bot, kyc, int(kid or 0), message.from_user.id, False, reason)
    if done is None:
        await message.answer(f"ℹ️ این درخواست {ALREADY} است.", reply_markup=main_menu(True))
        return
    await message.answer(f"❌ درخواست #{kid} رد شد. دلیل: {escape(reason)}", reply_markup=main_menu(True))
