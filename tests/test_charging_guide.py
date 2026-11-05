"""راهنمای شارژ: صفحه‌ی کیف پول Stard و راهنمای پنل دو موجودی را توضیح می‌دهند و موجودی لحظه‌ای را نشان می‌دهند."""
import pytest

from bot.handlers.admin import CHARGING_GUIDE, PANEL_HELP
from bot.ui import Adm
from tests.helpers import ADMIN, click, send

pytestmark = pytest.mark.usefixtures("no_unhandled_errors")


def test_guide_text_covers_both_balances():
    for needle in ("💳 شماره کارت", "@Stard_marketbot", "افزایش موجودی", "TON", "STARD_PAY_CURRENCY=IRT",
                   "داشبورد توسعه‌دهنده", "تومان"):
        assert needle in CHARGING_GUIDE, needle
    assert CHARGING_GUIDE in PANEL_HELP and len(PANEL_HELP) < 4096


async def test_wallet_screen_shows_live_balance_mode_and_guide(env):
    dp, bot, session, db, fake = env
    fake.balance = 7_654_321
    await send(dp, bot, ADMIN, "/start")
    await click(dp, bot, ADMIN, Adm(name="wallet"))
    text = session.texts()[-1]
    assert "7,654,321" in text and "test" in text and "حالت آزمایشی ربات" in text
    assert "@Stard_marketbot" in text and "💳 شماره کارت" in text
    assert len(text) < 4096


async def test_wallet_screen_still_shows_guide_when_api_fails(env):
    dp, bot, session, db, fake = env
    await send(dp, bot, ADMIN, "/start")

    async def boom(*a, **k):
        from bot.stard_api import StardError
        raise StardError(0, "network_error", "down")
    shop = dp["shop"]
    shop.api.wallet = boom
    await click(dp, bot, ADMIN, Adm(name="wallet"))
    text = session.texts()[-1]
    assert "network_error" in text and "@Stard_marketbot" in text
