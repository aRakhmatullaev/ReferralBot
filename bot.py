import os
import asyncio
import logging
from datetime import datetime
import pandas as pd
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, \
    CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import CommandObject
from apscheduler.schedulers.asyncio import AsyncIOScheduler

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
EXCEL_FILE = "contest_database.xlsx"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


class AdminStates(StatesGroup):
    waiting_for_broadcast_msg = State()


cached_top_10 = []
last_sorted_time = "Never"


# --- Excel Database Helpers ---
def load_db() -> pd.DataFrame:
    columns = ["User ID", "Username", "Full Name", "Phone", "Points", "Referred By", "Is Subscribed", "Is Registered",
               "Rank"]

    if os.path.exists(EXCEL_FILE):
        try:
            # We explicitly force text columns to prevent pandas from guessing numbers
            df = pd.read_excel(
                EXCEL_FILE,
                dtype={
                    'User ID': str,
                    'Phone': str,
                    'Referred By': str,
                    'Username': str,
                    'Full Name': str,
                    'Rank': str
                }
            )
            # Ensure Points is always an integer numeric type
            df['Points'] = pd.to_numeric(df['Points'], errors='coerce').fillna(0).astype(int)

            # Reindex just in case columns got mixed up or dropped
            df = df.reindex(columns=columns)
            return df
        except Exception as e:
            logging.error(f"Error loading Excel file, recreating it: {e}")
            pass

    # Create fresh empty DataFrame structure explicitly cast as object/string text types
    df = pd.DataFrame(columns=columns)
    df = df.astype({
        'User ID': str, 'Username': str, 'Full Name': str, 'Phone': str,
        'Points': int, 'Referred By': str, 'Rank': str
    })
    df.to_excel(EXCEL_FILE, index=False)
    return df

def save_db(df: pd.DataFrame):
    df.to_excel(EXCEL_FILE, index=False)


# --- Keyboards ---
def get_main_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    buttons = [[KeyboardButton(text="👤 Profile"), KeyboardButton(text="📊 Leaderboard")]]
    if user_id in ADMIN_IDS:
        buttons.append([KeyboardButton(text="📢 Broadcast"), KeyboardButton(text="📋 Admin Panel")])
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


def get_channel_inline_kb(invite_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Join Channel", url=invite_url)],
        [InlineKeyboardButton(text="✅ Check Subscription", callback_data="check_sub")]
    ])


share_phone_kb = ReplyKeyboardMarkup(keyboard=[
    [KeyboardButton(text="📱 Share Phone Number", request_contact=True)]
], resize_keyboard=True, one_time_keyboard=True)


async def is_subscribed(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
        return member.status in ["member", "administrator", "creator"]
    except Exception:
        return False


# --- Hourly Sorting and Verification Daemon ---
async def audit_and_sort_excel():
    global cached_top_10, last_sorted_time
    logging.info("Har daqiqalik obuna auditi va keshni yangilash boshlandi...")

    df = load_db()
    if df.empty:
        logging.info("Baza bo'sh. Audit o'tkazilmadi.")
        return

    df["User ID"] = df["User ID"].astype(str)
    if "Referred By" in df.columns:
        df["Referred By"] = df["Referred By"].astype(str)

    # 1. Obunadan chiqqanlar uchun ball chegirish qismi
    for idx, row in df.iterrows():
        user_id_str = str(row["User ID"])
        was_subscribed = str(row.get("Is Subscribed", "FALSE")).upper() == "TRUE" or row.get("Is Subscribed") is True

        try:
            is_currently_active = await is_subscribed(int(user_id_str))

            # Ilgari a'zo edi, lekin hozir chiqib ketgan bo'lsa
            if was_subscribed and not is_currently_active:
                df.at[idx, "Is Subscribed"] = False

                referrer_id = row.get("Referred By", "")
                if pd.notna(referrer_id) and str(referrer_id).strip() != "" and str(referrer_id) != "nan":
                    referrer_id_str = str(referrer_id).split('.')[0]

                    if referrer_id_str in df["User ID"].values:
                        current_points = pd.to_numeric(df.loc[df["User ID"] == referrer_id_str, "Points"].values[0],
                                                       errors='coerce')
                        if pd.isna(current_points):
                            current_points = 0

                        new_points = max(0, int(current_points) - 1)
                        df.loc[df["User ID"] == referrer_id_str, "Points"] = new_points

                        try:
                            await bot.send_message(
                                chat_id=int(referrer_id_str),
                                text=f"⚠️ Siz taklif qilgan doʻstlardan biri kanalni tark etdi! Shu sababli sizdan 1 ball chegirildi. Joriy ballingiz: {new_points} ball."
                            )
                        except Exception as e:
                            logging.error(f"Referrer {referrer_id_str} ga ogohlantirish yuborishda xato: {e}")

            elif not was_subscribed and is_currently_active:
                df.at[idx, "Is Subscribed"] = True

        except Exception as e:
            logging.error(f"Foydalanuvchi {user_id_str} ni audit qilishda xatolik: {e}")

    # 2. Ma'lumotlarni saralash va tartiblash
    df['Points'] = pd.to_numeric(df['Points'], errors='coerce').fillna(0).astype(int)

    # Faqat ro'yxatdan o'tganlarni tartiblaymiz
    registered_df = df[df["Is Registered"] == True].sort_values(by=["Points"], ascending=False)

    # Ranks (O'rinlar) ustunini yangilash
    for rank, idx in enumerate(registered_df.index, start=1):
        df.at[idx, "Rank"] = str(rank)

    save_db(df)

    # 3. 🌟 GLOBAL KESHNI YANGILASH (Mana shu qism keshni to'ldiradi)
    temp_top_10 = []
    top_10_rows = registered_df.head(10)

    for rank, (idx, row) in enumerate(top_10_rows.iterrows(), start=1):
        name = row["Full Name"] if pd.notna(row["Full Name"]) else "Ishtirokchi"
        points = int(row["Points"])
        temp_top_10.append({
            "rank": rank,
            "name": name,
            "points": points
        })

    cached_top_10 = temp_top_10

    # Oxirgi yangilangan vaqtni chiroyli formatda yozamiz (soat:daqiqa)
    # Matndagi "(Har soatda yangilanadi)" iborasini daqiqalik tizimga moslab o'zgartirishingiz mumkin
    last_sorted_time = datetime.now().strftime("%H:%M")
    logging.info("Kesh muvaffaqiyatli yangilandi!")

# 🌟 Make sure this is positioned right below your async def start_cmd block!
@dp.callback_query(F.data == "verify_sub")
async def check_subscription_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    is_active = await is_subscribed(user_id)

    if not is_active:
        await callback.answer(
            "❌ Siz hali kanalga aʼzo boʻlmadingiz! Iltimos, aʼzo boʻlib qaytadan urinib koʻring.",
            show_alert=True
        )
        return

    await callback.answer("✅ Obuna tasdiqlandi!")

    try:
        await callback.message.delete()
    except Exception:
        pass

    contact_keyboard = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Telefon raqamni yuborish", request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True
    )

    await callback.message.answer(
        "✅ Obuna tasdiqlandi!\n\nRoʻyxatdan oʻtishni yakunlash uchun pastdaging tugma orqali telefon raqamingizni yuboring:",
        reply_markup=contact_keyboard
    )
# --- Bot Handlers ---
@dp.message(CommandStart())
async def start_cmd(message: Message, command: CommandObject):
    user_id_str = str(message.from_user.id)
    username = message.from_user.username or ""
    name = message.from_user.full_name or ""

    # Referal ID ni aniqlash
    referrer_id = None
    if command.args:
        args = command.args.strip()
        if args.isdigit() and args != user_id_str:
            referrer_id = args

    df = load_db()
    df["User ID"] = df["User ID"].astype(str)

    user_exists = user_id_str in df["User ID"].values

    if user_exists:
        user_row = df[df["User ID"] == user_id_str].iloc[0]
        # Agar foydalanuvchi allaqachon toʻliq roʻyxatdan oʻtgan boʻlsa, asosiy menyuni koʻrsatish
        if str(user_row.get("Is Registered", "")).upper() == "TRUE" or user_row.get("Is Registered") is True:
            await message.answer(
                "📊 Xush kelibsiz! Quyidagi menyu orqali botdan foydalanishingiz mumkin.",
                reply_markup=get_main_keyboard(message.from_user.id)
            )
            return
    else:
        # Yangi qator yaratish (Ball hali 0, roʻyxatdan oʻtish yakunlanmagan)
        new_row = {
            "User ID": user_id_str,
            "Username": username,
            "Full Name": name,
            "Phone": "",
            "Points": 0,
            "Referred By": str(referrer_id) if referrer_id else "",
            "Is Subscribed": False,
            "Is Registered": False,
            "Rank": ""
        }
        df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
        save_db(df)

    # 1. Obuna boʻlganligini tekshirish
    if not await is_subscribed(message.from_user.id):
        builder = InlineKeyboardBuilder()
        # Kanalingiz ssilkasi (Username qismini oʻzgartiring)
        builder.row(InlineKeyboardButton(text="📢 Kanalga aʼzo boʻlish", url="https://t.me/+ZaTAGLEkJwVmZGIy"))
        builder.row(InlineKeyboardButton(text="🔄 Obunani tekshirish", callback_data="verify_sub"))

        await message.answer(
            "👋 Xush kelibsiz! SAT va Cambridge matematika materiallari ulashiladigan hamda foydali kurslar boʻlib oʻtadigan kanalimizning maxsus konkurs botiga xush kelibsiz.\n\n"
            "Konkursda qatnashish uchun dastlab rasmiy kanalimizga aʼzo boʻlishingiz lozim. "
            "Kanalga qoʻshilib, pastdagi tekshirish tugmasini bosing!",
            reply_markup=builder.as_markup(), parse_mode="HTML"
        )
        return

    # 2. Obunadan oʻtgan boʻlsa, kontakt soʻrash
    contact_keyboard = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Telefon raqamni yuborish", request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True
    )
    await message.answer(
        "👋 Xush kelibsiz! SAT va Cambridge matematika materiallari ulashiladigan hamda foydali kurslar boʻlib oʻtadigan kanalimizning maxsus konkurs botiga xush kelibsiz.\n\n"
        "Quyidagi tugmalar orqali oʻz referal havolangizni oling, doʻstlaringizni taklif qiling va ball yigʻib, reytingda gʻolib boʻling!\n\n"
        "👇 Konkursni boshlash uchun pastdagi tugma orqali telefon raqamingizni yuboring:",
        reply_markup=contact_keyboard
    )

    # 2. ✅ If they pass verification, drop the native contact collector request
    from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
    contact_keyboard = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Share Contact", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True
    )

    await message.answer(
        "✅ <b>Subscription verified!</b>\n\nPlease share your phone number using the button below to finalize registration:",
        reply_markup=contact_keyboard,
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "check_sub")
async def check_sub_cb(callback_query):
    user_id = callback_query.from_user.id
    if await is_subscribed(user_id):
        df = load_db()
        df.loc[df["User ID"] == user_id, "Is Subscribed"] = True
        save_db(df)
        await callback_query.message.answer(
            "✅ Channel membership verified! Please share your phone number to complete registration.",
            reply_markup=share_phone_kb
        )
        await callback_query.answer()
    else:
        await callback_query.answer("❌ You haven't joined the channel yet!", show_alert=True)


@dp.message(F.contact)
async def handle_contact(message: Message):
    user_id_str = str(message.from_user.id)
    df = load_db()
    df["User ID"] = df["User ID"].astype(str)

    if user_id_str not in df["User ID"].values:
        await message.answer("⚠️ Maʼlumot topilmadi. Iltimos, botni qayta ishga tushiring: /start")
        return

    user_row = df[df["User ID"] == user_id_str].iloc[0]

    # Takroriy ball berishni oldini olish devori
    if str(user_row.get("Is Registered", "")).upper() == "TRUE" or user_row.get("Is Registered") is True:
        await message.answer("✅ Siz allaqachon roʻyxatdan oʻtgansiz!",
                             reply_markup=get_main_keyboard(message.from_user.id))
        return

    if not await is_subscribed(message.from_user.id):
        await message.answer("❌ Kanalga obunangiz toʻxtatilgan. Tasdiqlash uchun /start buyrugʻini bosing.")
        return

    # Telefon raqamini saqlash va statusni True qilish
    phone = message.contact.phone_number
    df.loc[df["User ID"] == user_id_str, "Phone"] = str(phone)
    df.loc[df["User ID"] == user_id_str, "Is Registered"] = True
    save_db(df)

    # 🏆 FAQAT SHU YERDA BALL QOʻSHILADI
    referrer_id = user_row.get("Referred By", "")
    if pd.notna(referrer_id) and str(referrer_id).strip() != "" and str(referrer_id) != "nan":
        referrer_id_str = str(referrer_id).split('.')[0]

        if referrer_id_str in df["User ID"].values:
            current_points = pd.to_numeric(df.loc[df["User ID"] == referrer_id_str, "Points"].values[0],
                                           errors='coerce')
            if pd.isna(current_points):
                current_points = 0

            df.loc[df["User ID"] == referrer_id_str, "Points"] = int(current_points) + 1
            save_db(df)

            # Taklif qilgan odamga xabar yuborish
            try:
                await message.bot.send_message(
                    chat_id=int(referrer_id_str),
                    text=f"🎉 Tabriklaymiz! Doʻstingiz sizning havolangiz orqali muvaffaqiyatli roʻyxatdan oʻtdi va sizga +1 ball qoʻshildi."
                )
            except Exception as e:
                logging.error(f"Referrer {referrer_id_str} ga xabar yuborishda xato: {e}")

    await message.answer(
        "🎉 Roʻyxatdan oʻtish muvaffaqiyatli yakunlandi! Konkursda ishtirok etayotganingizdan xursandmiz. \n\n"
        "Join our private <a href='https://t.me/+VdugUXO1awZlY2My'> hiking community </a> to touch some grass. ",
        reply_markup=get_main_keyboard(message.from_user.id),
        parse_mode = "HTML"
    )

# Change this line:
@dp.message(F.text == "👤 Profile")
async def show_profile(message: Message):
    df = load_db()
    user_id_str = str(message.from_user.id)

    df["User ID"] = df["User ID"].astype(str)

    if user_id_str not in df["User ID"].values:
        await message.answer("❌ Profile not found. Please use /start to re-register.")
        return

    user_data = df[df["User ID"] == user_id_str].iloc[0]

    points = user_data["Points"]
    phone = user_data["Phone"] if pd.notna(user_data["Phone"]) else "Not linked"
    rank = user_data["Rank"] if ("Rank" in user_data and pd.notna(user_data["Rank"])) else "N/A"

    bot_username = "Math_konkurs_bot"
    referral_link = f"https://t.me/{bot_username}?start={user_id_str}"

    # 🌟 Formatted with clean HTML tags instead of markdown syntax
    profile_text = (
        f"👤 <b>Profilingiz</b>\n\n"
        f"🆔 ID: <code>{user_id_str}</code>\n"
        f"📞 Telefon: <code>{phone}</code>\n"
        f"⭐ Ball: <code>{points}</code>\n"
        f"🏆 O'rningiz: <code>{rank}</code>\n\n"
        f"🔗 <b>Referal havolangiz:</b>\n{referral_link}"
    )

    # Switch parse_mode to HTML here
    await message.answer(profile_text, parse_mode="HTML")


@dp.message(F.text == "📊 Leaderboard")
async def show_leaderboard(message: Message):
    if not cached_top_10:
        await message.answer("📊 Top ishtirokchilar ro'yxati shakillanmoqda iltimos birozdan so'ng urinib ko'ring!")
        return

    text_lines = [
        f"🏆 <b>Top ishtirokchilar (Top 10)</b>",
        f"<i>Oxirgi yangilanish: {last_sorted_time} (Har soatda yangilanadi)</i>\n"
    ]

    for entry in cached_top_10:
        text_lines.append(f"<b>{entry['rank']}.</b> {entry['name']} — <code>{entry['points']} pts</code>")

    await message.answer("\n".join(text_lines), parse_mode="HTML")


# --- Admin Operations ---
@dp.message(F.text == "📋 Admin Panel")
async def admin_panel(message: Message):
    if message.from_user.id not in ADMIN_IDS: return

    df = load_db()
    output = ["📋 <b>LOCAL DATABASE RECORD ENGINE LOG</b>\n"]

    for idx, row in df.iterrows():
        if str(row["Is Registered"]).upper() == "TRUE" or row["Is Registered"] is True:
            output.append(
                f"ID: <code>{row['User ID']}</code> | Name: {row['Full Name']} | User: @{row['Username']} | Phone: {row['Phone']} | Points: {row['Points']}")

    full_text = "\n".join(output)
    if len(full_text) > 4000:
        for x in range(0, len(full_text), 4000):
            await message.answer(full_text[x:x + 4000], parse_mode="HTML")
    else:
        await message.answer(full_text, parse_mode="HTML")


@dp.message(F.text == "📢 Broadcast")
async def start_broadcast(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS: return
    await message.answer("Send the message you want to broadcast to ALL bot users.")
    await state.set_state(AdminStates.waiting_for_broadcast_msg)


@dp.message(AdminStates.waiting_for_broadcast_msg)
async def process_broadcast(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await state.clear()
        return

    await state.clear()
    status_msg = await message.answer("🚀 Dispatching system broad message...")

    df = load_db()
    success, fail = 0, 0

    for _, row in df.iterrows():
        if str(row["Is Registered"]).upper() == "TRUE" or row["Is Registered"] is True:
            try:
                await message.copy_to(chat_id=int(row["User ID"]))
                success += 1
                await asyncio.sleep(0.05)
            except Exception:
                fail += 1

    await status_msg.edit_text(f"📢 <b>Broadcast Execution Summary:</b>\n\n✅ Delivered: {success}\n❌ Failed: {fail}",
                               parse_mode="HTML")


async def main():
    scheduler = AsyncIOScheduler()
    scheduler.add_job(audit_and_sort_excel, 'interval', minutes=60)
    scheduler.start()

    await audit_and_sort_excel()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass