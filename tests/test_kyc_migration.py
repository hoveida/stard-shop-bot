"""ارتقای پایگاه داده‌ی نسخه‌ی 3.2.4 (revision 0004) به 0005_kyc با همان مسیری که ربات هنگام شروع/آپدیت اجرا می‌کند."""
from alembic import command
from sqlalchemy import inspect

from bot.db import Database
from bot.migrations_runner import alembic_config, all_revisions, current_revision, head_revision, upgrade
from bot.updater import _revisions_from_names
from tests.conftest import requires_sqlite


@requires_sqlite
async def test_v324_database_upgrades_automatically(tmp_path):
    path = str(tmp_path / "shop.db")
    old = Database(path)
    await old.connect(create=False)
    cfg = alembic_config()

    def to_0004(sc):
        cfg.attributes["connection"] = sc
        command.upgrade(cfg, "0004")
    async with old.engine.connect() as c:
        await c.run_sync(to_0004)
        await c.commit()
    assert await current_revision(old) == "0004"
    await old.upsert_user(7, "old", "Old")
    await old.credit(7, 123_000, "topup")
    # شِمای 0004 ستون card_id ندارد؛ ردیف را مثل کد 3.2.4 مستقیم می‌نویسیم
    await old.write("INSERT INTO topups(user_id, amount, photo_id, file_unique_id, status, created_at) "
                    "VALUES(7, 50000, 'photo', 'uniq', 'pending', '2026-10-09T10:00:00Z')")
    tid = await old.scalar("SELECT id FROM topups")
    await old.close()

    # شروع عادی ربات (Database.connect) خودش مهاجرت را اجرا می‌کند؛ هیچ قدم دستی لازم نیست
    db = Database(path)
    await db.connect()
    try:
        assert await current_revision(db) == head_revision() == "0005"
        async with db.engine.connect() as c:
            tables = await c.run_sync(lambda sc: set(inspect(sc).get_table_names()))
            cols = await c.run_sync(lambda sc: {x["name"] for x in inspect(sc).get_columns("topups")})
        assert {"kyc_cards", "kyc_messages"} <= tables and "card_id" in cols
        assert (await db.get_user(7)).balance == 123_000
        t = await db.get_topup(tid)
        assert t["amount"] == 50_000 and t["card_id"] is None
        await upgrade(db)  # دوباره اجرا شدن بی‌ضرر است
        assert await current_revision(db) == "0005"
    finally:
        await db.close()


def test_updater_sees_the_new_revision():
    names = [f"stard-shop-bot-v3.3.0/bot/migrations/versions/{n}" for n in
             ("0001_v2_baseline.py", "0002_v3_production.py", "0003_broadcasts.py", "0004_precise_times.py",
              "0005_kyc.py")]
    assert _revisions_from_names(names) == [r for r, _ in all_revisions()]
