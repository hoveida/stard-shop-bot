"""احراز هویت (KYC): ثبت کارت بانکی با بررسی Luhn، عکس تأیید، بررسی اتمیک مدیر، سقف شارژ روزانه.

تنظیم در پنل ← 🪪 احراز هویت؛ ذخیره در تنظیم kyc (JSON).
حریم خصوصی: شماره‌ی کامل کارت فقط در پایگاه داده و برای مدیر نمایش داده می‌شود؛ همه‌جای دیگر (کاربر، audit، لاگ)
ماسک‌شده است (۶ رقم اول + ۴ رقم آخر).
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .db import Database, Row, now, ts
from .pricing import PERSIAN_DIGITS

TEHRAN = ZoneInfo("Asia/Tehran")
DEFAULT = {"enabled": False, "mode": "note", "require_card": False, "limit_unverified": 0, "limit_verified": 0,
           "reviewers": "all"}
PHOTO_MODES = {"note": "📝 کارت کنار برگه‌ی دست‌نویس (نام ربات + تاریخ)", "selfie": "🤳 سلفی با کارت در دست"}
REVIEWERS = {"all": "همه‌ی مدیرها", "owners": "فقط مالک‌ها"}
STATUS = {"none": "⚪️ ثبت نشده", "pending": "⏳ در انتظار بررسی", "approved": "✅ تأیید شده", "rejected": "❌ رد شده"}
REJECT_REASONS = (
    "عکس واضح نیست",
    "نام صاحب کارت با کارت یکی نیست",
    "شماره‌ی کارت با عکس یکی نیست",
    "برگه‌ی دست‌نویس یا تاریخ دیده نمی‌شود",
    "چهره یا کارت در سلفی دیده نمی‌شود",
    "کارت متعلق به شخص دیگری است",
)


# ---------- کارت ----------
def normalize_card(text: str | None) -> str | None:
    """۱۶ رقم (ارقام فارسی/عربی، فاصله و خط تیره مجاز) یا None."""
    t = (text or "").translate(PERSIAN_DIGITS)
    for ch in " -_‌‏‎":
        t = t.replace(ch, "")
    return t if len(t) == 16 and t.isdigit() else None


def luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def fmt_card(card: str) -> str:
    return "-".join(card[i:i + 4] for i in range(0, len(card), 4))


def mask(card: str) -> str:
    """۶ رقم اول + ۴ رقم آخر: 6037-99**-****-1234"""
    if len(card) != 16:
        return "****"
    return fmt_card(card[:6] + "*" * 6 + card[-4:])


# ---------- تاریخ شمسی (برای برگه‌ی دست‌نویس) ----------
def jalali(d) -> tuple[int, int, int]:
    gy, gm, gd = d.year, d.month, d.day
    g_d_m = (0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334)
    gy2 = gy + 1 if gm > 2 else gy
    days = 355666 + 365 * gy + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400 + gd + g_d_m[gm - 1]
    jy = -1595 + 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    jm = 1 + days // 31 if days < 186 else 7 + (days - 186) // 30
    jd = 1 + (days % 31 if days < 186 else (days - 186) % 30)
    return jy, jm, jd


def today_jalali(at: datetime | None = None) -> str:
    y, m, d = jalali((at or datetime.now(TEHRAN)).astimezone(TEHRAN).date())
    return f"{y}/{m:02d}/{d:02d}"


def day_start_utc(at: datetime | None = None) -> str:
    """شروع «امروز» به وقت تهران، به UTC (قالب created_at)."""
    local = (at or datetime.now(TEHRAN)).astimezone(TEHRAN)
    return ts(local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc))


# ---------- پایگاه داده ----------
class KycError(Exception):
    pass


class Kyc:
    def __init__(self, db: Database):
        self.db = db

    async def config(self) -> dict:
        cfg = dict(DEFAULT)
        cfg.update(await self.db.get_json("kyc", {}) or {})
        return cfg

    async def set_config(self, admin_id: int | None, **changes) -> dict:
        before = await self.config()
        cfg = {**before, **{k: v for k, v in changes.items() if k in DEFAULT}}
        await self.db.set_json("kyc", cfg)
        await self.db.audit(admin_id=admin_id, action="kyc.config", before=before, after=cfg)
        return cfg

    def can_review(self, admins, uid: int, cfg: dict) -> bool:
        return admins.is_owner(uid) if cfg.get("reviewers") == "owners" else admins.is_admin(uid)

    def reviewers(self, admins, cfg: dict) -> list[int]:
        return sorted(admins.owners) if cfg.get("reviewers") == "owners" else admins.all()

    async def get(self, kid: int) -> Row | None:
        return await self.db.one("SELECT * FROM kyc_cards WHERE id = :id", {"id": kid})

    async def cards(self, uid: int) -> list[Row]:
        return await self.db.all("SELECT * FROM kyc_cards WHERE user_id = :u ORDER BY id DESC", {"u": uid})

    async def approved_cards(self, uid: int) -> list[Row]:
        return await self.db.all("SELECT * FROM kyc_cards WHERE user_id = :u AND status = 'approved' ORDER BY id",
                                 {"u": uid})

    async def is_verified(self, uid: int) -> bool:
        return bool(await self.approved_cards(uid))

    async def status(self, uid: int) -> tuple[str, Row | None]:
        """وضعیت کلی: approved اگر حداقل یک کارت تأییدشده باشد، وگرنه وضعیت آخرین درخواست."""
        rows = await self.cards(uid)
        if any(r["status"] == "approved" for r in rows):
            return "approved", next(r for r in rows if r["status"] == "approved")
        return (rows[0]["status"], rows[0]) if rows else ("none", None)

    async def submit(self, uid: int, card: str, holder: str, photo_id: str, photo_unique_id: str | None,
                     mode: str) -> int:
        async with self.db.tx() as c:
            if await self.db.one("SELECT id FROM kyc_cards WHERE user_id = :u AND status = 'pending'", {"u": uid}, c=c):
                raise KycError("درخواست قبلی شما هنوز در حال بررسی است.")
            if await self.db.one("SELECT id FROM kyc_cards WHERE user_id = :u AND card_number = :n "
                                 "AND status = 'approved'", {"u": uid, "n": card}, c=c):
                raise KycError("این کارت قبلاً برای شما تأیید شده است.")
            kid = (await self.db._x(c, "INSERT INTO kyc_cards(user_id, card_number, holder, photo_id, photo_unique_id, "
                                       "photo_mode, status, created_at) VALUES(:u, :n, :h, :p, :pu, :m, 'pending', :t) "
                                       "RETURNING id",
                                    {"u": uid, "n": card, "h": holder[:128], "p": photo_id, "pu": photo_unique_id,
                                     "m": mode, "t": now()})).scalar_one()
            await self.db.audit_in(c, admin_id=None, action="kyc_submit", user_id=uid, ref=f"kyc:{kid}",
                                   after={"card": mask(card), "mode": mode})
        return kid

    async def decide(self, kid: int, admin_id: int, approve: bool, reason: str | None = None) -> Row | None:
        """تأیید/رد فقط یک بار: مدیر دوم None می‌گیرد (UPDATE شرطی، چندنمونه‌ای امن)."""
        status = "approved" if approve else "rejected"
        async with self.db.tx() as c:
            r = await self.db._x(c, "UPDATE kyc_cards SET status = :s, admin_id = :a, reason = :r, decided_at = :t "
                                    "WHERE id = :id AND status = 'pending'",
                                 {"s": status, "a": admin_id, "r": None if approve else (reason or "")[:256],
                                  "t": now(), "id": kid})
            if r.rowcount != 1:
                return None
            row = await self.db.one("SELECT * FROM kyc_cards WHERE id = :id", {"id": kid}, c=c)
            await self.db.audit_in(c, admin_id=admin_id, action="kyc_approve" if approve else "kyc_reject",
                                   user_id=row["user_id"], ref=f"kyc:{kid}", before={"status": "pending"},
                                   after={"status": status, "card": mask(row["card_number"])}, reason=row["reason"])
        return row

    async def add_message(self, kid: int, chat_id: int, message_id: int) -> None:
        await self.db.write("INSERT INTO kyc_messages(kyc_id, chat_id, message_id) VALUES(:k, :c, :m) "
                            "ON CONFLICT DO NOTHING", {"k": kid, "c": chat_id, "m": message_id})

    async def messages(self, kid: int) -> list[Row]:
        return await self.db.all("SELECT chat_id, message_id FROM kyc_messages WHERE kyc_id = :k", {"k": kid})

    async def pending(self, limit: int = 20) -> list[Row]:
        return await self.db.all("SELECT * FROM kyc_cards WHERE status = 'pending' ORDER BY id LIMIT :l", {"l": limit})

    async def count_pending(self) -> int:
        return int(await self.db.scalar("SELECT COUNT(*) FROM kyc_cards WHERE status = 'pending'") or 0)

    async def card_owners(self, card: str, except_uid: int) -> list[int]:
        """کاربران دیگری که همین کارت را ثبت کرده‌اند (هشدار برای مدیر)."""
        rows = await self.db.all("SELECT DISTINCT user_id FROM kyc_cards WHERE card_number = :n AND user_id != :u "
                                 "AND status IN ('pending', 'approved')", {"n": card, "u": except_uid})
        return [r["user_id"] for r in rows]

    async def daily_limit(self, uid: int, cfg: dict | None = None) -> int | None:
        """سقف شارژ امروز این کاربر (تومان) یا None = بدون سقف. فقط وقتی احراز هویت روشن است."""
        cfg = cfg or await self.config()
        if not cfg["enabled"]:
            return None
        lim = int(cfg["limit_verified"] if await self.is_verified(uid) else cfg["limit_unverified"]) or 0
        return lim if lim > 0 else None

    async def used_today(self, uid: int) -> int:
        return int(await self.db.scalar(
            "SELECT COALESCE(SUM(amount), 0) FROM topups WHERE user_id = :u AND status IN ('pending', 'approved') "
            "AND created_at >= :s", {"u": uid, "s": day_start_utc()}) or 0)
