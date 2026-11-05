"""احراز هویت از طریق Dispatcher واقعی: ثبت، تأیید، رد، تصمیم دوباره، ارسال دوباره، نمایش ماسک‌شده، شارژ با کارت
تأییدشده و سقف روزانه."""
import pytest
from aiogram.types import PhotoSize

from bot.db import TopupLimitExceeded
from bot.handlers.kyc import KYC_HOME, KYC_NEW
from bot.kyc import Kyc, luhn_ok, mask, normalize_card
from bot.logging_setup import redact
from bot.ui import Adm
from tests.helpers import ADMIN, CUSTOMER, click, click_raw, send

pytestmark = pytest.mark.usefixtures("no_unhandled_errors")

CARD = "6037991234567893"          # Luhn معتبر
CARD2 = "4111111111111111"
ADMIN2 = 300


def photo(uid="P1"):
    return [PhotoSize(file_id=f"F{uid}", file_unique_id=uid, width=1, height=1)]


async def _on(dp, db, **cfg):
    await Kyc(db).set_config(ADMIN, enabled=True, **cfg)
    await db.set_json("admins", [ADMIN2])
    await dp["admins"].load()


async def _submit(dp, bot, card=CARD, holder="Ali Rezaei", uid="P1"):
    await click_raw(dp, bot, CUSTOMER, KYC_NEW)
    await send(dp, bot, CUSTOMER, card)
    await send(dp, bot, CUSTOMER, holder)
    await send(dp, bot, CUSTOMER, photo=photo(uid))


def test_card_helpers():
    assert normalize_card("۶۰۳۷-۹۹۱۲ 3456 7893") == CARD and normalize_card("1234") is None
    assert luhn_ok(CARD) and luhn_ok(CARD2) and not luhn_ok("6037991234567894")
    assert mask(CARD) == "6037-99**-****-7893"
    assert CARD not in redact(f"card {CARD} / 6037-9912-3456-7893") and "7893" in redact(f"card {CARD}")


async def test_submit_approve_and_masked_display(env):
    dp, bot, session, db, fake = env
    await _on(dp, db)
    for uid in (ADMIN, ADMIN2, CUSTOMER):
        await send(dp, bot, uid, "/start")
    await send(dp, bot, CUSTOMER, "👤 حساب کاربری")
    assert "ثبت نشده" in session.texts()[-1]
    await click_raw(dp, bot, CUSTOMER, KYC_NEW)
    await send(dp, bot, CUSTOMER, "6037991234567894")
    assert "معتبر نیست" in session.texts()[-1]
    await send(dp, bot, CUSTOMER, CARD)
    await send(dp, bot, CUSTOMER, "Ali Rezaei")
    assert "@shop_bot" in session.texts()[-1] and "/" in session.texts()[-1]  # برگه: نام ربات + تاریخ
    await send(dp, bot, CUSTOMER, photo=photo())
    photos = [m for m in session.sent if type(m).__name__ == "SendPhoto"]
    assert {m.chat_id for m in photos} == {ADMIN, ADMIN2}
    assert all("6037-9912-3456-7893" in m.caption for m in photos)  # مدیر شماره‌ی کامل را می‌بیند
    user_texts = [getattr(m, "text", "") or "" for m in session.sent if getattr(m, "chat_id", None) == CUSTOMER]
    assert not any(CARD in t or "6037-9912-3456-7893" in t for t in user_texts)
    assert any(mask(CARD) in t for t in user_texts)
    kid = (await Kyc(db).pending())[0]["id"]
    assert len(await Kyc(db).messages(kid)) == 2

    await click(dp, bot, CUSTOMER, Adm(name="kyc_ok", arg=str(kid)))  # کاربر عادی نمی‌تواند
    assert (await Kyc(db).get(kid))["status"] == "pending"
    session.sent.clear()
    await click(dp, bot, ADMIN, Adm(name="kyc_ok", arg=str(kid)))
    assert (await Kyc(db).get(kid))["status"] == "approved"
    edits = [m for m in session.sent if type(m).__name__ == "EditMessageCaption"]
    assert {m.chat_id for m in edits} == {ADMIN, ADMIN2}
    assert all(m.reply_markup is None and "تأیید شد توسط" in m.caption for m in edits)
    assert any(getattr(m, "chat_id", None) == CUSTOMER and "تأیید شد" in (getattr(m, "text", "") or "")
               for m in session.sent)
    audit = await db.audit_entries(action="kyc_approve")
    assert audit and audit[0]["admin_id"] == ADMIN and CARD not in (audit[0]["after"] or "")

    # تصمیم دوم
    await click(dp, bot, ADMIN2, Adm(name="kyc_no", arg=str(kid)))
    assert "قبلاً بررسی شده" in session.alerts()[-1]
    await send(dp, bot, CUSTOMER, "👤 حساب کاربری")
    assert "تأیید شده" in session.texts()[-1]
    await click_raw(dp, bot, CUSTOMER, KYC_HOME)
    assert mask(CARD) in session.texts()[-1] and CARD not in session.texts()[-1]
    # کارت کاربر در پنل: شماره‌ی کامل و وضعیت
    await click(dp, bot, ADMIN, Adm(name="ucard", arg=str(CUSTOMER)))
    assert "6037-9912-3456-7893" in session.texts()[-1] and "احراز هویت" in session.texts()[-1]


async def test_reject_with_preset_and_custom_reason_then_resubmit(env):
    dp, bot, session, db, fake = env
    await _on(dp, db, mode="selfie")
    for uid in (ADMIN, ADMIN2, CUSTOMER):
        await send(dp, bot, uid, "/start")
    await _submit(dp, bot)
    kyc = Kyc(db)
    kid = (await kyc.pending())[0]["id"]
    await click(dp, bot, ADMIN, Adm(name="kyc_no", arg=str(kid)))
    assert (await kyc.get(kid))["status"] == "pending"  # فقط منوی دلیل باز شد
    await click(dp, bot, ADMIN, Adm(name="kyc_rr", arg=f"{kid}|0"))
    row = await kyc.get(kid)
    assert row["status"] == "rejected" and row["reason"] == "عکس واضح نیست"
    assert any("رد شد" in t and "عکس واضح نیست" in t for t in session.texts())
    st, _ = await kyc.status(CUSTOMER)
    assert st == "rejected"
    await click(dp, bot, ADMIN2, Adm(name="kyc_ok", arg=str(kid)))
    assert "قبلاً بررسی شده" in session.alerts()[-1] and (await kyc.get(kid))["status"] == "rejected"
    assert await db.audit_entries(action="kyc_reject")

    # ارسال دوباره بعد از رد؛ این بار دلیل دلخواه
    await _submit(dp, bot, uid="P2")
    kid2 = (await kyc.pending())[0]["id"]
    assert kid2 != kid
    await click(dp, bot, ADMIN2, Adm(name="kyc_rc", arg=str(kid2)))
    await send(dp, bot, ADMIN2, "کارت به نام شما نیست")
    assert (await kyc.get(kid2))["reason"] == "کارت به نام شما نیست"
    # درخواست در انتظار فقط یکی
    await _submit(dp, bot, uid="P3")
    await _submit(dp, bot, uid="P4")
    await click_raw(dp, bot, CUSTOMER, KYC_NEW)
    assert "در حال بررسی" in session.alerts()[-1]
    assert len(await kyc.pending()) == 1


async def test_owners_only_reviewers(env):
    dp, bot, session, db, fake = env
    await _on(dp, db, reviewers="owners")
    for uid in (ADMIN, ADMIN2, CUSTOMER):
        await send(dp, bot, uid, "/start")
    await _submit(dp, bot)
    assert {m.chat_id for m in session.sent if type(m).__name__ == "SendPhoto"} == {ADMIN}
    kid = (await Kyc(db).pending())[0]["id"]
    await click(dp, bot, ADMIN2, Adm(name="kyc_ok", arg=str(kid)))
    assert "فقط برای مالک" in session.alerts()[-1] and (await Kyc(db).get(kid))["status"] == "pending"


async def test_topup_requires_verified_card_and_shows_it_masked(env):
    dp, bot, session, db, fake = env
    await db.set_setting("card_number", "5022-2910-0000-0000")
    await _on(dp, db, require_card=True)
    for uid in (ADMIN, CUSTOMER):
        await send(dp, bot, uid, "/start")
    await send(dp, bot, CUSTOMER, "💳 شارژ کیف پول")
    assert "احراز هویت" in session.texts()[-1]
    await _submit(dp, bot)
    kyc = Kyc(db)
    kid = (await kyc.pending())[0]["id"]
    await click(dp, bot, ADMIN, Adm(name="kyc_ok", arg=str(kid)))
    await send(dp, bot, CUSTOMER, "💳 شارژ کیف پول")
    await send(dp, bot, CUSTOMER, "50000")
    assert "کدام کارت" in session.texts()[-1]
    await click_raw(dp, bot, CUSTOMER, "tpc:999")
    assert "تأییدشده نیست" in session.alerts()[-1]
    await click_raw(dp, bot, CUSTOMER, f"tpc:{kid}")
    assert "5022-2910-0000-0000" in session.texts()[-1]
    session.sent.clear()
    await send(dp, bot, CUSTOMER, photo=photo("R1"))
    t = (await db.pending_topups())[0]
    assert t["card_id"] == kid
    caps = [m.caption for m in session.sent if type(m).__name__ == "SendPhoto" and m.chat_id == ADMIN]
    assert caps and mask(CARD) in caps[0] and CARD not in caps[0]
    await click(dp, bot, ADMIN, Adm(name="topups"))
    assert any(mask(CARD) in (getattr(m, "caption", "") or "") for m in session.sent
               if type(m).__name__ == "SendPhoto")
    await click(dp, bot, ADMIN, Adm(name="tp_ok", arg=str(t["id"])))
    assert (await db.get_user(CUSTOMER)).balance == 50_000


async def test_daily_limits_unverified_vs_verified(env):
    dp, bot, session, db, fake = env
    await db.set_setting("card_number", "5022-2910-0000-0000")
    await _on(dp, db, limit_unverified=100_000, limit_verified=300_000)
    for uid in (ADMIN, CUSTOMER):
        await send(dp, bot, uid, "/start")
    await send(dp, bot, CUSTOMER, "💳 شارژ کیف پول")
    assert "100,000" in session.texts()[-1]
    await send(dp, bot, CUSTOMER, "150000")
    assert "سقف" in session.texts()[-1]
    await send(dp, bot, CUSTOMER, "80000")
    await send(dp, bot, CUSTOMER, photo=photo("R1"))
    assert len(await db.pending_topups()) == 1
    await send(dp, bot, CUSTOMER, "💳 شارژ کیف پول")
    await send(dp, bot, CUSTOMER, "30000")
    assert "سقف" in session.texts()[-1]
    # سقف داخل تراکنش هم اعمال می‌شود (دو رسید هم‌زمان)
    from bot.kyc import day_start_utc
    with pytest.raises(TopupLimitExceeded):
        await db.create_topup(CUSTOMER, 30_000, "x", "y", daily_limit=(100_000, day_start_utc()))
    # بعد از احراز هویت سقف بالاتر
    await _submit(dp, bot)
    kid = (await Kyc(db).pending())[0]["id"]
    await click(dp, bot, ADMIN, Adm(name="kyc_ok", arg=str(kid)))
    await send(dp, bot, CUSTOMER, "💳 شارژ کیف پول")
    await send(dp, bot, CUSTOMER, "200000")
    assert "عکس رسید" in session.texts()[-1]
    await send(dp, bot, CUSTOMER, photo=photo("R2"))
    assert len(await db.pending_topups()) == 2
    # خاموش بودن احراز هویت = بدون سقف
    await Kyc(db).set_config(ADMIN, enabled=False)
    assert await Kyc(db).daily_limit(CUSTOMER) is None


async def test_admin_section_and_settings(env):
    dp, bot, session, db, fake = env
    await send(dp, bot, ADMIN, "/start")
    await send(dp, bot, CUSTOMER, "/start")
    await click_raw(dp, bot, CUSTOMER, KYC_HOME)
    assert "فعال نیست" in session.alerts()[-1]
    for arg in ("", "t", "m", "c", "r"):
        await click(dp, bot, ADMIN, Adm(name="kyc", arg=arg))
    cfg = await Kyc(db).config()
    assert cfg["enabled"] and cfg["mode"] == "selfie" and cfg["require_card"] and cfg["reviewers"] == "owners"
    await click(dp, bot, ADMIN, Adm(name="kyc_lim", arg="u"))
    await send(dp, bot, ADMIN, "۲۰۰۰۰۰")
    assert (await Kyc(db).config())["limit_unverified"] == 200_000
    await click(dp, bot, ADMIN, Adm(name="kyc_list"))
    assert "در انتظاری نیست" in session.alerts()[-1]
    await click(dp, bot, ADMIN, Adm(name="settings"))
    await click(dp, bot, ADMIN, Adm(name="home"))
    assert await db.audit_entries(action="kyc.config")
