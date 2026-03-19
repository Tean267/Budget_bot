#!/usr/bin/env python3
"""
Telegram Budget Bot v2.5
Tính năng:
  - Theo dõi ngân sách hàng ngày (dư / thâm hụt)
  - Phân loại chi tiêu theo danh mục (inline keyboard)
  - Báo cáo ngày / tuần / danh mục / top
  - [NEW] Nhắc nhở tự động hàng tối qua JobQueue
  - [NEW] Hoàn tác giao dịch gần nhất (/undo)
"""

import logging
import os
import pytz
from datetime import date, time, timedelta, datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)
import mysql.connector

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════
#  CẤU HÌNH — đọc từ biến môi trường (docker-compose hoặc .env)
# ══════════════════════════════════════════════════════════════════
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")

DB_CONFIG = {
    "host":     os.environ.get("MYSQL_HOST",     "localhost"),
    "user":     os.environ.get("MYSQL_USER",     "root"),
    "password": os.environ.get("MYSQL_PASSWORD", "your_password"),
    "database": os.environ.get("MYSQL_DATABASE", "budget_bot"),
}

# ── Whitelist (tuỳ chọn) ───────────────────────────────────────────
# Để trống ALLOWED_USERS="" = cho phép tất cả mọi người dùng
# Điền user_id cách nhau bởi dấu phẩy để giới hạn:
#   ALLOWED_USERS="123456789,987654321"
_raw = os.environ.get("ALLOWED_USERS", "").strip()
ALLOWED_USERS: set = (
    {int(x.strip()) for x in _raw.split(",") if x.strip().isdigit()}
    if _raw else set()
)

def is_allowed(user_id: int) -> bool:
    """True nếu whitelist trống (public) hoặc user_id nằm trong whitelist."""
    return not ALLOWED_USERS or user_id in ALLOWED_USERS


# Múi giờ Việt Nam — nhắc nhở sẽ gửi đúng giờ VN
VN_TZ = pytz.timezone("Asia/Ho_Chi_Minh")

# Giờ gửi nhắc nhở mặc định (21:00 VN)
DEFAULT_REMINDER_HOUR   = 21
DEFAULT_REMINDER_MINUTE = 0

CATEGORIES = {
    "🍜": "Ăn uống",
    "🚗": "Di chuyển",
    "🛒": "Mua sắm",
    "🏠": "Nhà ở",
    "💊": "Sức khỏe",
    "🎮": "Giải trí",
    "📚": "Học tập",
    "💡": "Hóa đơn",
    "🎁": "Quà tặng",
    "❓": "Khác",
}

# ══════════════════════════════════════════════════════════════════
#  DATABASE
# ══════════════════════════════════════════════════════════════════

def get_connection():
    return mysql.connector.connect(**DB_CONFIG)


def init_db():
    """Tạo database + tất cả bảng nếu chưa tồn tại."""
    cfg = {k: v for k, v in DB_CONFIG.items() if k != "database"}
    conn = mysql.connector.connect(**cfg)
    cur  = conn.cursor()

    cur.execute(
        f"CREATE DATABASE IF NOT EXISTS `{DB_CONFIG['database']}` "
        f"CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
    )
    cur.execute(f"USE `{DB_CONFIG['database']}`")

    # ── users ──────────────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id          BIGINT PRIMARY KEY,
            username         VARCHAR(100) DEFAULT '',
            total_budget     DOUBLE  NOT NULL DEFAULT 0,
            spent            DOUBLE  NOT NULL DEFAULT 0,
            start_date       DATE,
            end_date         DATE,
            reminder_hour    TINYINT UNSIGNED DEFAULT 21,
            reminder_minute  TINYINT UNSIGNED DEFAULT 0,
            reminder_enabled TINYINT(1)       DEFAULT 1,
            created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ── transactions ───────────────────────────────────────────────
    # Thêm cột is_undone để soft-delete (không mất lịch sử thật)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id          INT AUTO_INCREMENT PRIMARY KEY,
            user_id     BIGINT       NOT NULL,
            amount      DOUBLE       NOT NULL,
            note        VARCHAR(255) DEFAULT '',
            category    VARCHAR(50)  DEFAULT 'Khác',
            tx_date     DATE         NOT NULL,
            daily_after DOUBLE       NOT NULL,
            is_undone   TINYINT(1)   DEFAULT 0,
            created_at  TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
    """)

    # ── daily_summary ──────────────────────────────────────────────
    cur.execute("""
        CREATE TABLE IF NOT EXISTS daily_summary (
            id           INT AUTO_INCREMENT PRIMARY KEY,
            user_id      BIGINT NOT NULL,
            summary_date DATE   NOT NULL,
            total_spent  DOUBLE NOT NULL DEFAULT 0,
            budget_day   DOUBLE NOT NULL DEFAULT 0,
            balance      DOUBLE NOT NULL DEFAULT 0,
            UNIQUE KEY uq_user_date (user_id, summary_date),
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
    """)

    conn.commit()
    cur.close()
    conn.close()
    logger.info("✅ Database v2.5 sẵn sàng.")


# ── CRUD helpers ───────────────────────────────────────────────────

def get_user(user_id):
    conn = get_connection()
    cur  = conn.cursor(dictionary=True)
    cur.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
    row = cur.fetchone()
    cur.close(); conn.close()
    return row


def upsert_user(user_id, username, total_budget, spent, start_date, end_date):
    conn = get_connection()
    cur  = conn.cursor()
    cur.execute("""
        INSERT INTO users (user_id, username, total_budget, spent, start_date, end_date)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            username     = VALUES(username),
            total_budget = VALUES(total_budget),
            spent        = VALUES(spent),
            start_date   = VALUES(start_date),
            end_date     = VALUES(end_date)
    """, (user_id, username, total_budget, spent, start_date, end_date))
    conn.commit()
    cur.close(); conn.close()


def set_reminder(user_id, hour, minute, enabled=True):
    conn = get_connection()
    cur  = conn.cursor()
    cur.execute("""
        UPDATE users
        SET reminder_hour = %s, reminder_minute = %s, reminder_enabled = %s
        WHERE user_id = %s
    """, (hour, minute, 1 if enabled else 0, user_id))
    conn.commit()
    cur.close(); conn.close()


def get_all_reminder_users():
    """Lấy tất cả user đang bật nhắc nhở."""
    conn = get_connection()
    cur  = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT user_id, reminder_hour, reminder_minute
        FROM users
        WHERE reminder_enabled = 1 AND total_budget > 0
    """)
    rows = cur.fetchall()
    cur.close(); conn.close()
    return rows


def add_transaction(user_id, amount, note, category, daily_after):
    conn = get_connection()
    cur  = conn.cursor()
    cur.execute("""
        INSERT INTO transactions (user_id, amount, note, category, tx_date, daily_after)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (user_id, amount, note, category, date.today(), daily_after))
    last_id = cur.lastrowid
    conn.commit()
    cur.close(); conn.close()
    return last_id


def get_last_transaction(user_id):
    """Lấy giao dịch chưa bị undo gần nhất."""
    conn = get_connection()
    cur  = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT * FROM transactions
        WHERE user_id = %s AND is_undone = 0
        ORDER BY created_at DESC
        LIMIT 1
    """, (user_id,))
    row = cur.fetchone()
    cur.close(); conn.close()
    return row


def undo_transaction(tx_id, user_id, amount):
    """
    Soft-delete giao dịch: đánh dấu is_undone = 1
    rồi hoàn trả lại spent / total_budget trong bảng users.
    """
    conn = get_connection()
    cur  = conn.cursor()

    # Đánh dấu undo
    cur.execute(
        "UPDATE transactions SET is_undone = 1 WHERE id = %s",
        (tx_id,)
    )

    # Hoàn trả ngân sách
    if amount < 0:          # giao dịch chi tiêu → trừ spent
        cur.execute(
            "UPDATE users SET spent = spent - %s WHERE user_id = %s",
            (abs(amount), user_id)
        )
    else:                   # giao dịch thêm tiền → trừ total_budget
        cur.execute(
            "UPDATE users SET total_budget = total_budget - %s WHERE user_id = %s",
            (amount, user_id)
        )

    conn.commit()
    cur.close(); conn.close()


def upsert_daily_summary(user_id, summary_date, total_spent, budget_day):
    balance = budget_day - total_spent
    conn = get_connection()
    cur  = conn.cursor()
    cur.execute("""
        INSERT INTO daily_summary (user_id, summary_date, total_spent, budget_day, balance)
        VALUES (%s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            total_spent = VALUES(total_spent),
            budget_day  = VALUES(budget_day),
            balance     = VALUES(balance)
    """, (user_id, summary_date, total_spent, budget_day, balance))
    conn.commit()
    cur.close(); conn.close()


def get_daily_report(user_id, report_date):
    conn = get_connection()
    cur  = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT id, amount, note, category, created_at
        FROM transactions
        WHERE user_id = %s AND tx_date = %s AND is_undone = 0
        ORDER BY created_at ASC
    """, (user_id, report_date))
    rows = cur.fetchall()
    cur.close(); conn.close()
    return rows


def get_history(user_id, limit=10):
    conn = get_connection()
    cur  = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT id, amount, note, category, tx_date, daily_after
        FROM transactions
        WHERE user_id = %s AND is_undone = 0
        ORDER BY created_at DESC LIMIT %s
    """, (user_id, limit))
    rows = cur.fetchall()
    cur.close(); conn.close()
    return rows


def get_weekly_summary(user_id):
    conn = get_connection()
    cur  = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT summary_date, total_spent, budget_day, balance
        FROM daily_summary
        WHERE user_id = %s AND summary_date >= CURDATE() - INTERVAL 6 DAY
        ORDER BY summary_date ASC
    """, (user_id,))
    rows = cur.fetchall()
    cur.close(); conn.close()
    return rows


def get_category_report(user_id, days=30):
    conn = get_connection()
    cur  = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT category,
               SUM(ABS(amount)) AS total,
               COUNT(*)         AS count
        FROM transactions
        WHERE user_id = %s AND amount < 0 AND is_undone = 0
          AND tx_date >= CURDATE() - INTERVAL %s DAY
        GROUP BY category
        ORDER BY total DESC
    """, (user_id, days))
    rows = cur.fetchall()
    cur.close(); conn.close()
    return rows


def get_top_expenses(user_id, limit=5):
    conn = get_connection()
    cur  = conn.cursor(dictionary=True)
    cur.execute("""
        SELECT ABS(amount) AS amount, note, category, tx_date
        FROM transactions
        WHERE user_id = %s AND amount < 0 AND is_undone = 0
        ORDER BY ABS(amount) DESC LIMIT %s
    """, (user_id, limit))
    rows = cur.fetchall()
    cur.close(); conn.close()
    return rows


def delete_user(user_id):
    conn = get_connection()
    cur  = conn.cursor()
    cur.execute("DELETE FROM daily_summary WHERE user_id = %s", (user_id,))
    cur.execute("DELETE FROM transactions  WHERE user_id = %s", (user_id,))
    cur.execute("DELETE FROM users         WHERE user_id = %s", (user_id,))
    conn.commit()
    cur.close(); conn.close()


# ══════════════════════════════════════════════════════════════════
#  BUSINESS LOGIC
# ══════════════════════════════════════════════════════════════════

def days_remaining(u):
    if not u["end_date"]:
        return 1
    delta = (u["end_date"] - date.today()).days + 1
    return max(delta, 1)

def remaining_budget(u):
    return u["total_budget"] - u["spent"]

def daily_allowance(u):
    return remaining_budget(u) / days_remaining(u)

def fmt(amount):
    return f"{amount:,.0f}đ"

def pct_bar(pct, width=10):
    filled = int(min(pct, 100) / 100 * width)
    return f"[{'█' * filled}{'░' * (width - filled)}] {pct:.0f}%"

def today_spent_and_balance(user_id, u):
    rows        = get_daily_report(user_id, date.today())
    spent_today = sum(abs(r["amount"]) for r in rows if r["amount"] < 0)
    added_today = sum(r["amount"]      for r in rows if r["amount"] > 0)
    allowance   = daily_allowance(u)
    return spent_today, added_today, allowance - spent_today, allowance


def status_message(u, user_id):
    rem          = remaining_budget(u)
    days         = days_remaining(u)
    today        = date.today().strftime("%d/%m/%Y")
    end_s        = u["end_date"].strftime("%d/%m/%Y") if u["end_date"] else "chưa đặt"
    spent_today, _, balance_today, allowance = today_spent_and_balance(user_id, u)
    pct_used     = (u["spent"] / u["total_budget"] * 100) if u["total_budget"] else 0

    if balance_today > 0:
        today_status = f"✅ Còn dư hôm nay: *+{fmt(balance_today)}*"
    elif balance_today < 0:
        today_status = f"🔴 Thâm hụt hôm nay: *{fmt(balance_today)}*"
    else:
        today_status = "⚖️ Hôm nay vừa đủ định mức"

    return (
        f"📊 *Tình hình ngân sách*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📅 Hôm nay: {today}  🏁 Kết thúc: {end_s}\n"
        f"⏳ Còn lại: *{days} ngày*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💼 Tổng ngân sách: {fmt(u['total_budget'])}\n"
        f"💸 Đã tiêu (tổng): {fmt(u['spent'])}\n"
        f"{'💰' if rem >= 0 else '⚠️'} Còn lại: *{fmt(rem)}*\n"
        f"{pct_bar(pct_used)}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🎯 Định mức/ngày: *{fmt(allowance)}*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🗓 *Hôm nay ({today})*\n"
        f"  💸 Đã tiêu: {fmt(spent_today)}\n"
        f"  {today_status}\n"
    )


def build_evening_summary(user_id, u):
    """Tin nhắn tóm tắt buổi tối gửi qua JobQueue."""
    rows        = get_daily_report(user_id, date.today())
    allowance   = daily_allowance(u)
    spent_today = sum(abs(r["amount"]) for r in rows if r["amount"] < 0)
    balance     = allowance - spent_today
    rem         = remaining_budget(u)
    days        = days_remaining(u)
    today_str   = date.today().strftime("%d/%m/%Y")

    if balance > 0:
        verdict = f"✅ Hôm nay *tiết kiệm được +{fmt(balance)}*"
        tip     = "Tuyệt vời! Phần dư được dồn sang các ngày tới 💪"
    elif balance < 0:
        verdict = f"🔴 Hôm nay *vượt định mức {fmt(abs(balance))}*"
        tip     = "Ngày mai cố gắng chi tiêu ít hơn nhé 💡"
    else:
        verdict = "⚖️ Hôm nay vừa khít định mức"
        tip     = "Cân bằng hoàn hảo! 🎯"

    # Liệt kê giao dịch hôm nay (tối đa 5 dòng)
    tx_lines = []
    for r in rows[:5]:
        sign = "➖" if r["amount"] < 0 else "➕"
        note = f" {r['note']}" if r.get("note") else ""
        tx_lines.append(f"  {sign} {fmt(abs(r['amount']))} [{r['category']}]{note}")
    if len(rows) > 5:
        tx_lines.append(f"  _...và {len(rows)-5} giao dịch khác_")
    tx_block = "\n".join(tx_lines) if tx_lines else "  _(Không có giao dịch nào)_"

    return (
        f"🌙 *Tóm tắt ngày {today_str}*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🎯 Định mức: {fmt(allowance)}\n"
        f"💸 Đã tiêu:  {fmt(spent_today)}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{verdict}\n"
        f"_{tip}_\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📋 *Giao dịch hôm nay:*\n"
        f"{tx_block}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💼 Ngân sách còn: *{fmt(rem)}* ({days} ngày)\n"
        f"🎯 Định mức từ ngày mai: *{fmt(daily_allowance(u))}*"
    )


# ══════════════════════════════════════════════════════════════════
#  KEYBOARDS
# ══════════════════════════════════════════════════════════════════

def build_category_keyboard(amount, note):
    buttons, row = [], []
    for emoji, name in CATEGORIES.items():
        row.append(InlineKeyboardButton(
            f"{emoji} {name}",
            callback_data=f"cat|{amount}|{note}|{name}"
        ))
        if len(row) == 2:
            buttons.append(row); row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(buttons)


def build_undo_confirm_keyboard(tx_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Xác nhận hoàn tác", callback_data=f"undo_confirm|{tx_id}"),
        InlineKeyboardButton("❌ Huỷ",               callback_data="undo_cancel"),
    ]])


# ══════════════════════════════════════════════════════════════════
#  JOB: GỬI NHẮC NHỞ TỐI
# ══════════════════════════════════════════════════════════════════

async def send_evening_reminder(context: ContextTypes.DEFAULT_TYPE):
    """
    Job chạy mỗi phút, kiểm tra xem user nào đến giờ nhắc thì gửi.
    Cách này không cần lên lịch riêng cho từng user — chỉ cần 1 job duy nhất.
    """
    now_vn   = datetime.now(VN_TZ)
    cur_h    = now_vn.hour
    cur_m    = now_vn.minute

    users = get_all_reminder_users()
    for u_row in users:
        if u_row["reminder_hour"] == cur_h and u_row["reminder_minute"] == cur_m:
            uid = u_row["user_id"]
            u   = get_user(uid)
            if not u:
                continue
            try:
                msg = build_evening_summary(uid, u)
                await context.bot.send_message(
                    chat_id=uid, text=msg, parse_mode="Markdown"
                )
                logger.info(f"📬 Đã gửi nhắc nhở tối cho user {uid}")
            except Exception as e:
                logger.warning(f"Không gửi được cho {uid}: {e}")


async def midnight_rollover(context: ContextTypes.DEFAULT_TYPE):
    """
    Job chạy lúc 00:01 VN mỗi ngày.
    Nhiệm vụ:
      1. Chốt daily_summary cho ngày HÔM QUA (kể cả ngày không tiêu gì → spent=0)
      2. Không cần làm gì thêm vì daily_allowance() luôn tính theo date.today()
         nên tự động cập nhật khi user tương tác.
    """
    yesterday = date.today() - timedelta(days=1)
    conn = get_connection()
    cur  = conn.cursor(dictionary=True)
    # Lấy tất cả user còn trong kỳ ngân sách
    cur.execute("""
        SELECT user_id, total_budget, spent, start_date, end_date
        FROM users
        WHERE total_budget > 0
          AND end_date >= %s
          AND start_date <= %s
    """, (yesterday, yesterday))
    all_users = cur.fetchall()
    cur.close(); conn.close()

    rolled = 0
    for u_row in all_users:
        uid = u_row["user_id"]

        # Tính spent hôm qua từ transactions thực tế
        conn2 = get_connection()
        cur2  = conn2.cursor(dictionary=True)
        cur2.execute("""
            SELECT COALESCE(SUM(ABS(amount)), 0) AS spent_day
            FROM transactions
            WHERE user_id = %s AND tx_date = %s AND amount < 0 AND is_undone = 0
        """, (uid, yesterday))
        row = cur2.fetchone()
        cur2.close(); conn2.close()
        spent_yesterday = float(row["spent_day"]) if row else 0.0

        # Tính định mức của ngày hôm qua
        # (số ngày còn lại tính từ hôm qua)
        total_days_left_yesterday = max((u_row["end_date"] - yesterday).days + 1, 1)
        remaining_yesterday = u_row["total_budget"] - u_row["spent"]
        budget_day_yesterday = remaining_yesterday / total_days_left_yesterday

        # Upsert vào daily_summary (sẽ bỏ qua nếu đã có bản ghi từ lúc user giao dịch)
        conn3 = get_connection()
        cur3  = conn3.cursor()
        cur3.execute("""
            INSERT INTO daily_summary (user_id, summary_date, total_spent, budget_day, balance)
            VALUES (%s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                total_spent = CASE WHEN total_spent = 0 THEN VALUES(total_spent) ELSE total_spent END,
                budget_day  = CASE WHEN budget_day  = 0 THEN VALUES(budget_day)  ELSE budget_day  END,
                balance     = CASE WHEN balance     = 0 AND total_spent = 0
                                   THEN VALUES(balance) ELSE balance END
        """, (uid, yesterday, spent_yesterday, budget_day_yesterday,
              budget_day_yesterday - spent_yesterday))
        conn3.commit()
        cur3.close(); conn3.close()
        rolled += 1

    logger.info(f"🌅 Midnight rollover: đã chốt {rolled} user cho ngày {yesterday}")


# ══════════════════════════════════════════════════════════════════
#  ACCESS CONTROL
# ══════════════════════════════════════════════════════════════════

def require_access(func):
    """Decorator: chặn user không có trong whitelist (nếu whitelist được bật)."""
    import functools
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if not is_allowed(user_id):
            await update.effective_message.reply_text(
                "⛔ Bạn không có quyền sử dụng bot này."
            )
            logger.warning(f"Blocked unauthorized user: {user_id}")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper

# ══════════════════════════════════════════════════════════════════
#  COMMAND HANDLERS
# ══════════════════════════════════════════════════════════════════

@require_access
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Budget Bot v2.5*\n\n"
        "📌 *Lệnh cơ bản:*\n"
        "`/setup <tiền> <ngày>` — Đặt ngân sách\n"
        "`/status` — Tình hình ngân sách\n"
        "`/today` — Chi tiết hôm nay\n"
        "`/history` — 10 giao dịch gần nhất\n"
        "`/undo` — Hoàn tác giao dịch gần nhất\n"
        "`/reset` — Xóa toàn bộ dữ liệu\n\n"
        "📊 *Báo cáo & Thống kê:*\n"
        "`/week` — Tổng kết 7 ngày qua\n"
        "`/category` — Chi tiêu theo danh mục\n"
        "`/top` — Top 5 khoản chi lớn nhất\n\n"
        "🔔 *Nhắc nhở tự động:*\n"
        "`/reminder on` — Bật nhắc nhở (21:00 mặc định)\n"
        "`/reminder off` — Tắt nhắc nhở\n"
        "`/reminder 20 30` — Đặt giờ nhắc (20:30)\n\n"
        "💬 *Nhập giao dịch:*\n"
        "`-50000` → Tiêu 50,000đ (chọn danh mục)\n"
        "`+500000` → Thêm ngân sách\n"
        "`-50000 cà phê` → Có ghi chú\n",
        parse_mode="Markdown"
    )


@require_access
async def setup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id  = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name or ""
    args     = context.args

    if len(args) < 2:
        await update.message.reply_text(
            "❌ Cú pháp: `/setup <số tiền> <số ngày>`\nVí dụ: `/setup 3000000 30`",
            parse_mode="Markdown"
        )
        return
    try:
        budget = float(args[0].replace(",", ""))
        days   = int(args[1])
    except ValueError:
        await update.message.reply_text("❌ Số tiền và số ngày phải là số hợp lệ.")
        return
    if budget <= 0 or days <= 0:
        await update.message.reply_text("❌ Số tiền và số ngày phải lớn hơn 0.")
        return

    start_d = date.today()
    end_d   = start_d + timedelta(days=days - 1)
    upsert_user(user_id, username, budget, 0.0, start_d, end_d)

    await update.message.reply_text(
        f"✅ *Đã thiết lập ngân sách!*\n\n"
        f"💼 Tổng: {fmt(budget)}\n"
        f"⏳ Thời gian: {days} ngày\n"
        f"📅 {start_d.strftime('%d/%m/%Y')} → {end_d.strftime('%d/%m/%Y')}\n\n"
        f"💡 Có thể tiêu mỗi ngày: *{fmt(budget / days)}*\n\n"
        f"🔔 Nhắc nhở tự động: 21:00 mỗi tối\n"
        f"_(Dùng `/reminder off` để tắt)_",
        parse_mode="Markdown"
    )


@require_access
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    u       = get_user(user_id)
    if not u or u["total_budget"] == 0:
        await update.message.reply_text(
            "⚠️ Chưa có ngân sách. Dùng `/setup <số tiền> <số ngày>` để bắt đầu.",
            parse_mode="Markdown"
        )
        return
    await update.message.reply_text(status_message(u, user_id), parse_mode="Markdown")


@require_access
async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    u       = get_user(user_id)
    if not u or u["total_budget"] == 0:
        await update.message.reply_text("⚠️ Chưa có ngân sách.", parse_mode="Markdown")
        return

    rows      = get_daily_report(user_id, date.today())
    allowance = daily_allowance(u)

    if not rows:
        await update.message.reply_text(
            f"🗓 *Hôm nay chưa có giao dịch nào.*\n\n"
            f"🎯 Định mức hôm nay: *{fmt(allowance)}*",
            parse_mode="Markdown"
        )
        return

    spent_today = sum(abs(r["amount"]) for r in rows if r["amount"] < 0)
    added_today = sum(r["amount"]      for r in rows if r["amount"] > 0)
    balance     = allowance - spent_today

    lines = [f"🗓 *Chi tiết hôm nay — {date.today().strftime('%d/%m/%Y')}*\n",
             f"🎯 Định mức: {fmt(allowance)}\n",
             "━━━━━━━━━━━━━━━━━━"]

    for r in rows:
        sign   = "➖" if r["amount"] < 0 else "➕"
        note   = f" {r['note']}" if r.get("note") else ""
        cat    = f"[{r['category']}]" if r.get("category") else ""
        time_s = r["created_at"].strftime("%H:%M") if hasattr(r["created_at"], "strftime") else ""
        lines.append(f"{sign} {fmt(abs(r['amount']))} {cat}{note}  _{time_s}_")

    lines += ["━━━━━━━━━━━━━━━━━━",
              f"💸 Tổng đã tiêu: *{fmt(spent_today)}*"]
    if added_today > 0:
        lines.append(f"➕ Đã thêm: *{fmt(added_today)}*")

    if balance > 0:
        lines += [f"\n✅ *Hôm nay dư: +{fmt(balance)}*",
                  "_(Phần dư sẽ được tính vào các ngày tới)_"]
    elif balance < 0:
        lines += [f"\n🔴 *Hôm nay thâm hụt: {fmt(balance)}*",
                  "_(Đã trừ vào ngân sách các ngày còn lại)_"]
    else:
        lines.append("\n⚖️ *Hôm nay vừa đủ định mức!*")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@require_access
async def week_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    u       = get_user(user_id)
    if not u or u["total_budget"] == 0:
        await update.message.reply_text("⚠️ Chưa có ngân sách.", parse_mode="Markdown")
        return

    rows = get_weekly_summary(user_id)
    if not rows:
        await update.message.reply_text("📭 Chưa có dữ liệu tuần này.")
        return

    lines = ["📅 *Tổng kết 7 ngày qua*\n", "━━━━━━━━━━━━━━━━━━"]
    total_spent = total_bal = 0

    for r in rows:
        d     = r["summary_date"]
        d_str = d.strftime("%d/%m") if hasattr(d, "strftime") else str(d)
        bal   = r["balance"]
        total_spent += r["total_spent"]
        total_bal   += bal
        bal_str = f"+{fmt(bal)}" if bal >= 0 else fmt(bal)
        lines.append(f"{'✅' if bal >= 0 else '🔴'} {d_str}: tiêu {fmt(r['total_spent'])} | {bal_str}")

    lines += ["━━━━━━━━━━━━━━━━━━",
              f"💸 Tổng tiêu: *{fmt(total_spent)}*",
              f"{'✅' if total_bal >= 0 else '🔴'} Tổng {'dư' if total_bal >= 0 else 'thâm hụt'}: *{fmt(total_bal)}*"]

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@require_access
async def category_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_category_report(update.effective_user.id, days=30)
    if not rows:
        await update.message.reply_text("📭 Chưa có dữ liệu chi tiêu.")
        return

    total = sum(r["total"] for r in rows)
    lines = ["🗂 *Chi tiêu theo danh mục (30 ngày)*\n", "━━━━━━━━━━━━━━━━━━"]
    for r in rows:
        pct = (r["total"] / total * 100) if total else 0
        bar = "█" * int(pct / 10) + "░" * (10 - int(pct / 10))
        lines.append(
            f"*{r['category']}*: {fmt(r['total'])} ({pct:.0f}%)\n"
            f"  [{bar}] {r['count']} lần"
        )
    lines += ["━━━━━━━━━━━━━━━━━━", f"💸 Tổng: *{fmt(total)}*"]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@require_access
async def top_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_top_expenses(update.effective_user.id, limit=5)
    if not rows:
        await update.message.reply_text("📭 Chưa có dữ liệu chi tiêu.")
        return

    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
    lines  = ["🏆 *Top 5 khoản chi lớn nhất*\n", "━━━━━━━━━━━━━━━━━━"]
    for i, r in enumerate(rows):
        note  = f" — {r['note']}" if r.get("note") else ""
        d_str = r["tx_date"].strftime("%d/%m/%Y") if hasattr(r["tx_date"], "strftime") else str(r["tx_date"])
        lines.append(
            f"{medals[i]} *{fmt(r['amount'])}*{note}\n"
            f"   [{r['category']}] 📅 {d_str}"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@require_access
async def history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_history(update.effective_user.id)
    if not rows:
        await update.message.reply_text("📭 Chưa có giao dịch nào.")
        return
    lines = ["📜 *Lịch sử giao dịch (10 gần nhất):*\n"]
    for tx in rows:
        sign  = "➕" if tx["amount"] > 0 else "➖"
        note  = f" {tx['note']}" if tx.get("note") else ""
        cat   = f"[{tx['category']}]" if tx.get("category") else ""
        d_str = tx["tx_date"].strftime("%d/%m/%Y") if hasattr(tx["tx_date"], "strftime") else str(tx["tx_date"])
        lines.append(
            f"{sign} {fmt(abs(tx['amount']))} {cat}{note}\n"
            f"   📅 {d_str} | Còn/ngày: {fmt(tx['daily_after'])}"
        )
    await update.message.reply_text("\n\n".join(lines), parse_mode="Markdown")


@require_access
async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    delete_user(update.effective_user.id)
    await update.message.reply_text("🗑️ Đã xóa toàn bộ dữ liệu. Dùng /setup để bắt đầu lại.")


# ── [NEW] UNDO ─────────────────────────────────────────────────────────────────

@require_access
async def undo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/undo — Hiện giao dịch gần nhất và hỏi xác nhận."""
    user_id = update.effective_user.id
    tx      = get_last_transaction(user_id)

    if not tx:
        await update.message.reply_text("📭 Không có giao dịch nào để hoàn tác.")
        return

    sign    = "➖" if tx["amount"] < 0 else "➕"
    note    = f" — {tx['note']}" if tx.get("note") else ""
    cat     = f"[{tx['category']}]" if tx.get("category") else ""
    time_s  = tx["created_at"].strftime("%d/%m/%Y %H:%M") if hasattr(tx["created_at"], "strftime") else ""

    await update.message.reply_text(
        f"↩️ *Hoàn tác giao dịch gần nhất?*\n\n"
        f"{sign} *{fmt(abs(tx['amount']))}* {cat}{note}\n"
        f"🕐 {time_s}\n\n"
        f"_Giao dịch sẽ bị xoá và ngân sách được hoàn trả._",
        parse_mode="Markdown",
        reply_markup=build_undo_confirm_keyboard(tx["id"])
    )


@require_access
async def undo_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Xử lý nút xác nhận / huỷ undo."""
    query   = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if query.data == "undo_cancel":
        await query.edit_message_text("❌ Đã huỷ hoàn tác.")
        return

    # undo_confirm|<tx_id>
    _, tx_id_str = query.data.split("|", 1)
    tx_id        = int(tx_id_str)

    # Kiểm tra giao dịch vẫn thuộc về user này và chưa bị undo
    conn = get_connection()
    cur  = conn.cursor(dictionary=True)
    cur.execute(
        "SELECT * FROM transactions WHERE id = %s AND user_id = %s AND is_undone = 0",
        (tx_id, user_id)
    )
    tx = cur.fetchone()
    cur.close(); conn.close()

    if not tx:
        await query.edit_message_text("⚠️ Giao dịch không hợp lệ hoặc đã bị hoàn tác.")
        return

    undo_transaction(tx_id, user_id, tx["amount"])

    # Cập nhật daily_summary sau undo
    u           = get_user(user_id)
    rows_today  = get_daily_report(user_id, date.today())
    spent_today = sum(abs(r["amount"]) for r in rows_today if r["amount"] < 0)
    upsert_daily_summary(user_id, date.today(), spent_today, daily_allowance(u))

    new_daily = daily_allowance(u)
    sign      = "➖" if tx["amount"] < 0 else "➕"
    note      = f" — {tx['note']}" if tx.get("note") else ""

    await query.edit_message_text(
        f"✅ *Đã hoàn tác thành công!*\n\n"
        f"{sign} {fmt(abs(tx['amount']))}{note} đã được xoá\n\n"
        f"💼 Ngân sách còn: *{fmt(remaining_budget(u))}*\n"
        f"🎯 Định mức/ngày mới: *{fmt(new_daily)}*",
        parse_mode="Markdown"
    )


# ── [NEW] REMINDER ─────────────────────────────────────────────────────────────

@require_access
async def reminder_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/reminder on|off|<giờ> <phút>"""
    user_id = update.effective_user.id
    u       = get_user(user_id)
    if not u:
        await update.message.reply_text(
            "⚠️ Bạn chưa thiết lập ngân sách. Dùng /setup trước.",
            parse_mode="Markdown"
        )
        return

    args = context.args

    # /reminder off
    if args and args[0].lower() == "off":
        set_reminder(user_id, u["reminder_hour"], u["reminder_minute"], enabled=False)
        await update.message.reply_text("🔕 Đã *tắt* nhắc nhở hàng tối.", parse_mode="Markdown")
        return

    # /reminder on
    if args and args[0].lower() == "on":
        set_reminder(user_id, u["reminder_hour"], u["reminder_minute"], enabled=True)
        h = u["reminder_hour"]; m = u["reminder_minute"]
        await update.message.reply_text(
            f"🔔 Đã *bật* nhắc nhở lúc *{h:02d}:{m:02d}* mỗi tối.",
            parse_mode="Markdown"
        )
        return

    # /reminder <giờ> <phút>
    if len(args) >= 2:
        try:
            h = int(args[0]); m = int(args[1])
            assert 0 <= h <= 23 and 0 <= m <= 59
        except (ValueError, AssertionError):
            await update.message.reply_text("❌ Giờ hợp lệ: 0–23, phút hợp lệ: 0–59\nVí dụ: `/reminder 21 00`", parse_mode="Markdown")
            return
        set_reminder(user_id, h, m, enabled=True)
        await update.message.reply_text(
            f"🔔 Đã đặt nhắc nhở lúc *{h:02d}:{m:02d}* (giờ VN) mỗi tối.",
            parse_mode="Markdown"
        )
        return

    # /reminder (không có args) — hiện trạng thái
    status_r = "🔔 Bật" if u["reminder_enabled"] else "🔕 Tắt"
    h = u["reminder_hour"]; m = u["reminder_minute"]
    await update.message.reply_text(
        f"*Cài đặt nhắc nhở:*\n\n"
        f"Trạng thái: {status_r}\n"
        f"Giờ nhắc: *{h:02d}:{m:02d}* (giờ VN)\n\n"
        f"Lệnh:\n"
        f"`/reminder on` — Bật\n"
        f"`/reminder off` — Tắt\n"
        f"`/reminder 21 00` — Đổi giờ",
        parse_mode="Markdown"
    )


# ══════════════════════════════════════════════════════════════════
#  MESSAGE HANDLER (nhập +/- giao dịch)
# ══════════════════════════════════════════════════════════════════

@require_access
async def handle_transaction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id  = update.effective_user.id
    username = update.effective_user.username or update.effective_user.first_name or ""
    text     = update.message.text.strip()

    if not text or text[0] not in ("+", "-"):
        await update.message.reply_text(
            "❓ Nhập: `-50000` hoặc `+200000`\nDùng /start để xem hướng dẫn.",
            parse_mode="Markdown"
        )
        return

    u = get_user(user_id)
    if not u or u["total_budget"] == 0:
        await update.message.reply_text(
            "⚠️ Bạn chưa đặt ngân sách. Dùng `/setup <số tiền> <số ngày>` trước.",
            parse_mode="Markdown"
        )
        return

    parts = text.split(None, 1)
    note  = parts[1].strip() if len(parts) > 1 else ""
    try:
        raw    = parts[0].replace(",", "").replace(".", "")
        amount = float(raw)
    except ValueError:
        await update.message.reply_text("❌ Số tiền không hợp lệ.")
        return

    context.user_data["pending_amount"] = amount
    context.user_data["pending_note"]   = note

    if amount < 0:
        await update.message.reply_text(
            f"💸 *Chi tiêu {fmt(abs(amount))}*"
            f"{' — ' + note if note else ''}\n\nChọn danh mục:",
            parse_mode="Markdown",
            reply_markup=build_category_keyboard(amount, note)
        )
    else:
        await _apply_transaction(update, context, user_id, u, amount, note, "Thu nhập")


@require_access
async def category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    u       = get_user(user_id)
    if not u:
        await query.edit_message_text("⚠️ Không tìm thấy ngân sách.")
        return

    _, amount_str, note, category = query.data.split("|", 3)
    await _apply_transaction_query(query, context, user_id, u, float(amount_str), note, category)


def _build_tx_response(u, amount, note, category, old_daily):
    """Tạo chuỗi phản hồi sau giao dịch."""
    new_daily  = daily_allowance(u)
    rem        = remaining_budget(u)
    days       = days_remaining(u)
    diff       = new_daily - old_daily
    diff_str   = f"+{fmt(diff)}" if diff >= 0 else f"-{fmt(abs(diff))}"
    note_str   = f"\n📝 _{note}_" if note else ""
    tx_type    = "chi tiêu" if amount < 0 else "thêm ngân sách"
    emoji      = "💸" if amount < 0 else "💰"

    # Balance hôm nay tính từ daily_allowance CŨ (trước khi update)
    rows_today  = get_daily_report(u["user_id"] if "user_id" in u else 0, date.today())
    spent_today = sum(abs(r["amount"]) for r in rows_today if r["amount"] < 0)
    balance_today = old_daily - spent_today

    if balance_today >= 0:
        today_line = f"✅ Hôm nay còn dư: *+{fmt(balance_today)}*"
    else:
        today_line = f"🔴 Hôm nay thâm hụt: *{fmt(balance_today)}*"

    warning = ""
    if rem < 0:
        warning = "\n\n🚨 *Cảnh báo: Vượt quá ngân sách!*"
    elif new_daily < 50000:
        warning = "\n\n⚠️ *Cẩn thận: Ngân sách còn rất ít!*"

    return (
        f"{emoji} *{tx_type.capitalize()}: {fmt(abs(amount))}*{note_str}\n"
        f"🗂 Danh mục: {category}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"💼 Ngân sách còn: {fmt(rem)} | ⏳ {days} ngày\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{today_line}\n"
        f"🎯 Định mức/ngày mới: *{fmt(new_daily)}*\n"
        f"{'📈' if diff >= 0 else '📉'} Thay đổi: {diff_str}/ngày"
        f"{warning}"
    )


async def _apply_transaction(update, context, user_id, u, amount, note, category):
    old_daily = daily_allowance(u)

    if amount < 0:
        u["spent"] += abs(amount)
    else:
        u["total_budget"] += amount

    upsert_user(user_id, u.get("username", ""), u["total_budget"], u["spent"], u["start_date"], u["end_date"])
    u["user_id"] = user_id
    new_daily    = daily_allowance(u)
    add_transaction(user_id, amount, note, category, new_daily)

    rows_today  = get_daily_report(user_id, date.today())
    spent_today = sum(abs(r["amount"]) for r in rows_today if r["amount"] < 0)
    upsert_daily_summary(user_id, date.today(), spent_today, old_daily)

    msg = _build_tx_response(u, amount, note, category, old_daily)
    await update.message.reply_text(msg, parse_mode="Markdown")


async def _apply_transaction_query(query, context, user_id, u, amount, note, category):
    old_daily = daily_allowance(u)

    if amount < 0:
        u["spent"] += abs(amount)
    else:
        u["total_budget"] += amount

    upsert_user(user_id, u.get("username", ""), u["total_budget"], u["spent"], u["start_date"], u["end_date"])
    u["user_id"] = user_id
    new_daily    = daily_allowance(u)
    add_transaction(user_id, amount, note, category, new_daily)

    rows_today  = get_daily_report(user_id, date.today())
    spent_today = sum(abs(r["amount"]) for r in rows_today if r["amount"] < 0)
    upsert_daily_summary(user_id, date.today(), spent_today, old_daily)

    msg = _build_tx_response(u, amount, note, category, old_daily)
    await query.edit_message_text(msg, parse_mode="Markdown")


# ══════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════

def main():
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start",    start))
    app.add_handler(CommandHandler("help",     start))
    app.add_handler(CommandHandler("setup",    setup))
    app.add_handler(CommandHandler("status",   status))
    app.add_handler(CommandHandler("today",    today_cmd))
    app.add_handler(CommandHandler("week",     week_cmd))
    app.add_handler(CommandHandler("category", category_cmd))
    app.add_handler(CommandHandler("top",      top_cmd))
    app.add_handler(CommandHandler("history",  history))
    app.add_handler(CommandHandler("undo",     undo_cmd))
    app.add_handler(CommandHandler("reminder", reminder_cmd))
    app.add_handler(CommandHandler("reset",    reset))

    # Callbacks
    app.add_handler(CallbackQueryHandler(category_callback, pattern=r"^cat\|"))
    app.add_handler(CallbackQueryHandler(undo_callback,     pattern=r"^undo_"))

    # Message
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_transaction))

    # ── Job nhắc nhở mỗi phút ──────────────────────────────────────
    app.job_queue.run_repeating(
        send_evening_reminder,
        interval=60,
        first=10,
    )

    # ── Job chốt ngày lúc 00:01 VN ────────────────────────────────
    # Tự động lưu daily_summary kể cả ngày không có giao dịch nào
    # Đảm bảo /week luôn đủ 7 ngày, daily_allowance luôn đúng
    now_vn      = datetime.now(VN_TZ)
    tomorrow_vn = (now_vn + timedelta(days=1)).replace(
        hour=0, minute=1, second=0, microsecond=0
    )
    first_run_s = (tomorrow_vn - now_vn).total_seconds()
    app.job_queue.run_repeating(
        midnight_rollover,
        interval=86400,
        first=first_run_s,
    )
    logger.info(
        f"🌅 Midnight rollover lần đầu sau {first_run_s/3600:.1f}h "
        f"({tomorrow_vn.strftime('%d/%m/%Y %H:%M')} VN)"
    )

    logger.info("🤖 Budget Bot v2.5 đang chạy...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()