import os
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Optional, List
import asyncio
import re

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiohttp import web

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration from environment variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST")  # Your Render URL
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"
PORT = int(os.getenv("PORT", 10000))  # Render uses port 10000

# Initialize bot and dispatcher
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Database setup
DB_NAME = "marketplace.db"

def init_db():
    """Initialize the database with required tables"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS vendors (
            vendor_id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER UNIQUE NOT NULL,
            business_name TEXT NOT NULL,
            services TEXT NOT NULL,
            keywords TEXT NOT NULL,
            contact TEXT NOT NULL,
            bot_username TEXT,
            description TEXT,
            price_range TEXT,
            total_orders INTEGER DEFAULT 0,
            avg_rating REAL DEFAULT 0.0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            order_id INTEGER PRIMARY KEY AUTOINCREMENT,
            vendor_id INTEGER NOT NULL,
            buyer_id INTEGER NOT NULL,
            details TEXT NOT NULL,
            deadline TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,
            FOREIGN KEY (vendor_id) REFERENCES vendors(vendor_id)
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ratings (
            rating_id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER UNIQUE NOT NULL,
            vendor_id INTEGER NOT NULL,
            buyer_id INTEGER NOT NULL,
            stars INTEGER NOT NULL CHECK(stars >= 1 AND stars <= 5),
            review_text TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (order_id) REFERENCES orders(order_id),
            FOREIGN KEY (vendor_id) REFERENCES vendors(vendor_id)
        )
    """)
    
    conn.commit()
    conn.close()
    logger.info("Database initialized successfully")

# FSM States
class VendorRegistration(StatesGroup):
    business_name = State()
    services = State()
    contact = State()
    bot_username = State()
    description = State()
    price_range = State()

class OrderPlacement(StatesGroup):
    vendor_id = State()
    order_details = State()
    deadline = State()

class RatingState(StatesGroup):
    order_id = State()
    stars = State()
    review = State()

# Helper functions
def get_db_connection():
    return sqlite3.connect(DB_NAME, check_same_thread=False)

def extract_keywords(text: str) -> List[str]:
    words = re.findall(r'\b\w+\b', text.lower())
    stopwords = {'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 
                 'of', 'with', 'by', 'from', 'is', 'are', 'was', 'were', 'i', 'you',
                 'my', 'your', 'do', 'does', 'need', 'want', 'looking', 'find', 'get',
                 'have', 'has', 'can', 'could', 'would', 'should', 'will', 'am'}
    keywords = [w for w in words if w not in stopwords and len(w) > 2]
    return keywords

def detect_intent(text: str) -> str:
    text_lower = text.lower()
    
    greetings = ['hi', 'hello', 'hey', 'good morning', 'good afternoon', 'good evening', 'sup', "what's up", 'whatsup']
    if any(greet in text_lower for greet in greetings):
        return 'greeting'
    
    thanks = ['thank', 'thanks', 'appreciate', 'grateful']
    if any(thank in text_lower for thank in thanks):
        return 'thanks'
    
    bot_questions = ['what can you do', 'how does this work', 'what is this', 'help me', 'what do you do']
    if any(q in text_lower for q in bot_questions):
        return 'help'
    
    register_keywords = ['register', 'sign up', 'create account', 'add my business', 'list my business', 'become vendor']
    if any(keyword in text_lower for keyword in register_keywords):
        return 'register'
    
    search_indicators = ['need', 'want', 'looking', 'find', 'where', 'who', 'buy', 'get', 'order', 'search']
    if any(indicator in text_lower for indicator in search_indicators):
        return 'search'
    
    if len(extract_keywords(text)) > 0:
        return 'search'
    
    return 'unknown'

def search_vendors(query: str) -> List[dict]:
    query_keywords = extract_keywords(query)
    
    if not query_keywords:
        return []
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT vendor_id, business_name, services, keywords, description, 
               contact, bot_username, price_range, avg_rating, total_orders
        FROM vendors
    """)
    vendors = cursor.fetchall()
    conn.close()
    
    results = []
    for vendor in vendors:
        vid, name, services, keywords, desc, contact, bot_user, price, rating, orders = vendor
        searchable = f"{name} {services} {keywords} {desc}".lower()
        score = sum(1 for keyword in query_keywords if keyword in searchable)
        
        if any(keyword in services.lower() for keyword in query_keywords):
            score += 2
        
        if score > 0:
            results.append({
                'vendor_id': vid,
                'business_name': name,
                'services': services,
                'description': desc,
                'contact': contact,
                'bot_username': bot_user,
                'price_range': price,
                'avg_rating': rating,
                'total_orders': orders,
                'score': score
            })
    
    results.sort(key=lambda x: x['score'], reverse=True)
    return results

def get_vendor_by_telegram_id(telegram_id: int) -> Optional[dict]:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM vendors WHERE telegram_id = ?", (telegram_id,))
    result = cursor.fetchone()
    conn.close()
    
    if result:
        return {
            "vendor_id": result[0],
            "telegram_id": result[1],
            "business_name": result[2],
            "services": result[3],
            "keywords": result[4],
            "contact": result[5],
            "bot_username": result[6],
            "description": result[7],
            "price_range": result[8],
            "total_orders": result[9],
            "avg_rating": result[10]
        }
    return None

def update_vendor_rating(vendor_id: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT AVG(stars), COUNT(*) 
        FROM ratings 
        WHERE vendor_id = ?
    """, (vendor_id,))
    
    result = cursor.fetchone()
    avg_rating = result[0] if result[0] else 0.0
    total_orders = result[1]
    
    cursor.execute("""
        UPDATE vendors 
        SET avg_rating = ?, total_orders = ?
        WHERE vendor_id = ?
    """, (avg_rating, total_orders, vendor_id))
    
    conn.commit()
    conn.close()

# Keyboards
def vendor_action_keyboard(vendor_id: int, has_bot: bool = False):
    buttons = []
    
    if has_bot:
        buttons.append([InlineKeyboardButton(text="🤖 Order via Their Bot", callback_data=f"botorder_{vendor_id}")])
    else:
        buttons.append([InlineKeyboardButton(text="📦 Place Order", callback_data=f"order_{vendor_id}")])
    
    buttons.append([InlineKeyboardButton(text="📞 View Contact", callback_data=f"contact_{vendor_id}")])
    buttons.append([InlineKeyboardButton(text="🔍 Search Again", callback_data="search_again")])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def rating_keyboard():
    buttons = [
        [InlineKeyboardButton(text=f"{'⭐' * i} ({i})", callback_data=f"rate_{i}")]
        for i in range(1, 6)
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def vendor_list_keyboard(vendors: List[dict]):
    buttons = []
    for vendor in vendors[:10]:
        rating_display = f"⭐ {vendor['avg_rating']:.1f} ({vendor['total_orders']})" if vendor['total_orders'] > 0 else "New"
        text = f"{vendor['business_name']} - {rating_display}"
        buttons.append([InlineKeyboardButton(
            text=text,
            callback_data=f"vendor_{vendor['vendor_id']}"
        )])
    
    buttons.append([InlineKeyboardButton(text="🔍 New Search", callback_data="search_again")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_greeting_response():
    import random
    greetings = [
        "Hey! What can I help you find today?",
        "Hi there! Looking for something specific?",
        "Hello! Need a vendor for something?",
        "Hey! What do you need today?",
        "Hi! How can I help you out?"
    ]
    return random.choice(greetings)

def get_thanks_response():
    import random
    responses = [
        "You're welcome! Anything else you need?",
        "Happy to help! Need anything else?",
        "No problem! What else can I do for you?",
        "Glad I could help! Looking for anything else?",
        "Anytime! Just let me know if you need more help."
    ]
    return random.choice(responses)

# Command handlers
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    welcome_text = (
        "Hey! Welcome to CU Marketplace 🎓\n\n"
        "I'm your campus vendor finder. Just tell me what you need in plain English "
        "and I'll connect you with the right people.\n\n"
        "💬 Try saying things like:\n"
        "• \"I need food delivered\"\n"
        "• \"Looking for laundry services\"\n"
        "• \"Where can I get a haircut?\"\n"
        "• \"Need someone to design a logo\"\n\n"
        "📝 Vendors: Use /register to list your business\n\n"
        "So... what are you looking for?"
    )
    await message.answer(welcome_text)

@dp.message(Command("register"))
async def cmd_register(message: types.Message, state: FSMContext):
    existing_vendor = get_vendor_by_telegram_id(message.from_user.id)
    
    if existing_vendor:
        await message.answer(
            f"Hey! You're already set up as {existing_vendor['business_name']}.\n\n"
            f"Check /myrating to see how you're doing!"
        )
        return
    
    await message.answer(
        "Awesome! Let's get your business on here.\n\n"
        "First up, what's your business name?"
    )
    await state.set_state(VendorRegistration.business_name)

@dp.message(VendorRegistration.business_name)
async def process_business_name(message: types.Message, state: FSMContext):
    await state.update_data(business_name=message.text)
    await message.answer(
        f"Nice! {message.text} sounds good.\n\n"
        "Now tell me what you offer. Be specific so students can find you easily.\n\n"
        "Like:\n"
        "• \"Jollof rice, fried rice, pasta, small chops\"\n"
        "• \"Laundry washing, ironing, dry cleaning\"\n"
        "• \"Logo design, flyers, video editing\""
    )
    await state.set_state(VendorRegistration.services)

@dp.message(VendorRegistration.services)
async def process_services(message: types.Message, state: FSMContext):
    services = message.text
    keywords = ' '.join(extract_keywords(services))
    
    await state.update_data(services=services, keywords=keywords)
    await message.answer(
        "Got it! How should customers reach you?\n\n"
        "Drop your WhatsApp number or Telegram username."
    )
    await state.set_state(VendorRegistration.contact)

@dp.message(VendorRegistration.contact)
async def process_contact(message: types.Message, state: FSMContext):
    await state.update_data(contact=message.text)
    await message.answer(
        "Great! Quick question:\n\n"
        "Do you have a Telegram bot for your business?\n\n"
        "If yes, share the bot username (like @YourBusinessBot)\n"
        "If no, just type 'no' or 'skip'"
    )
    await state.set_state(VendorRegistration.bot_username)

@dp.message(VendorRegistration.bot_username)
async def process_bot_username(message: types.Message, state: FSMContext):
    text = message.text.lower()
    
    if text in ['no', 'skip', 'none', 'nope', 'na', 'n/a', "don't have", "dont have"]:
        bot_username = None
        next_message = "No problem! Now give me a short pitch about your business."
    else:
        bot_username = message.text.strip()
        if not bot_username.startswith('@'):
            bot_username = f"@{bot_username}"
        next_message = f"Cool! I'll direct customers to {bot_username} when they order.\n\nNow tell me a bit about your business."
    
    await state.update_data(bot_username=bot_username)
    await message.answer(
        f"{next_message}\n\n"
        "What makes you different? Why should students choose you?\n"
        "(Keep it under 200 characters)"
    )
    await state.set_state(VendorRegistration.description)

@dp.message(VendorRegistration.description)
async def process_description(message: types.Message, state: FSMContext):
    await state.update_data(description=message.text[:200])
    await message.answer(
        "Almost there! What's your price range?\n\n"
        "Examples:\n"
        "• \"₦500 - ₦2000\"\n"
        "• \"From ₦1500\"\n"
        "• \"₦300 per item\""
    )
    await state.set_state(VendorRegistration.price_range)

@dp.message(VendorRegistration.price_range)
async def process_price_range(message: types.Message, state: FSMContext):
    data = await state.get_data()
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute("""
            INSERT INTO vendors (telegram_id, business_name, services, keywords, 
                               contact, bot_username, description, price_range)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            message.from_user.id,
            data['business_name'],
            data['services'],
            data['keywords'],
            data['contact'],
            data.get('bot_username'),
            data['description'],
            message.text
        ))
        conn.commit()
        
        bot_info = f"\n\nOrders will be sent to your bot: {data['bot_username']}" if data.get('bot_username') else ""
        
        await message.answer(
            f"✅ You're all set!\n\n"
            f"**{data['business_name']}**\n"
            f"Services: {data['services']}\n"
            f"Price Range: {message.text}{bot_info}\n\n"
            f"Students can now find you when they search! I'll ping you when orders come in.",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Registration error: {e}")
        await message.answer("Hmm, something went wrong. Try /register again?")
    finally:
        conn.close()
        await state.clear()

@dp.message(F.text & ~F.text.startswith('/'))
async def handle_conversation(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    
    if current_state:
        return
    
    text = message.text
    intent = detect_intent(text)
    
    if intent == 'greeting':
        await message.answer(get_greeting_response())
    
    elif intent == 'thanks':
        await message.answer(get_thanks_response())
    
    elif intent == 'help':
        await message.answer(
            "I help you find campus vendors super easily!\n\n"
            "Just tell me what you need:\n"
            "• \"I need food\"\n"
            "• \"Looking for laundry service\"\n"
            "• \"Where can I get my hair done?\"\n\n"
            "I'll find vendors for you, and you can order directly through me.\n\n"
            "Want to list your business? Use /register\n\n"
            "What can I help you find?"
        )
    
    elif intent == 'register':
        await cmd_register(message, state)
    
    elif intent == 'search':
        results = search_vendors(text)
        
        if not results:
            await message.answer(
                "Hmm, couldn't find anyone offering that right now.\n\n"
                "Try:\n"
                "• Using different words\n"
                "• Being more specific\n"
                "• Checking if vendors offer that service yet\n\n"
                "What else you looking for?"
            )
            return
        
        if len(results) == 1:
            vendor = results[0]
            await show_vendor_info(message, vendor)
        else:
            response = f"Found {len(results)} vendor{'s' if len(results) > 1 else ''} for you:\n\n"
            await message.answer(response, reply_markup=vendor_list_keyboard(results))
    
    else:
        await message.answer(
            "Not quite sure what you mean. Try telling me what you're looking for?\n\n"
            "Like \"I need food\" or \"looking for laundry service\""
        )

async def show_vendor_info(message: types.Message, vendor: dict):
    rating_text = f"⭐ {vendor['avg_rating']:.1f} ({vendor['total_orders']} orders)" if vendor['total_orders'] > 0 else "New Vendor"
    bot_badge = " 🤖" if vendor.get('bot_username') else ""
    
    details = (
        f"🏪 **{vendor['business_name']}**{bot_badge}\n\n"
        f"📋 {vendor['services']}\n"
        f"💰 {vendor['price_range']}\n"
        f"📊 {rating_text}\n\n"
        f"{vendor['description']}\n\n"
        f"Ready to order?"
    )
    
    await message.answer(
        details, 
        reply_markup=vendor_action_keyboard(vendor['vendor_id'], bool(vendor.get('bot_username'))),
        parse_mode="Markdown"
    )

@dp.callback_query(F.data.startswith("vendor_"))
async def show_vendor_details(callback: types.CallbackQuery):
    vendor_id = int(callback.data.replace("vendor_", ""))
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT business_name, services, description, contact, bot_username,
               price_range, avg_rating, total_orders
        FROM vendors WHERE vendor_id = ?
    """, (vendor_id,))
    result = cursor.fetchone()
    conn.close()
    
    if result:
        name, services, desc, contact, bot_user, price, rating, orders = result
        rating_text = f"⭐ {rating:.1f} ({orders} orders)" if orders > 0 else "New Vendor"
        bot_badge = " 🤖" if bot_user else ""
        
        details = (
            f"🏪 **{name}**{bot_badge}\n\n"
            f"📋 {services}\n"
            f"💰 {price}\n"
            f"📊 {rating_text}\n\n"
            f"{desc}\n\n"
            f"Ready to order?"
        )
        
        await callback.message.edit_text(
            details, 
            reply_markup=vendor_action_keyboard(vendor_id, bool(bot_user)),
            parse_mode="Markdown"
        )
    
    await callback.answer()

@dp.callback_query(F.data == "search_again")
async def search_again(callback: types.CallbackQuery):
    await callback.message.edit_text(
        "Cool, what else you need?\n\n"
        "Just type what you're looking for."
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("botorder_"))
async def redirect_to_vendor_bot(callback: types.CallbackQuery):
    vendor_id = int(callback.data.replace("botorder_", ""))
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT business_name, bot_username 
        FROM vendors WHERE vendor_id = ?
    """, (vendor_id,))
    result = cursor.fetchone()
    conn.close()
    
    if result and result[1]:
        name, bot_username = result
        bot_link = f"https://t.me/{bot_username.replace('@', '')}"
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"Open {name}'s Bot 🤖", url=bot_link)],
            [InlineKeyboardButton(text="⬅️ Back", callback_data=f"vendor_{vendor_id}")]
        ])
        
        await callback.message.edit_text(
            f"Perfect! {name} has their own bot for orders.\n\n"
            f"Click below to start chatting with their bot:",
            reply_markup=keyboard
        )
    else:
        await callback.answer("Bot not available", show_alert=True)
    
    await callback.answer()

@dp.callback_query(F.data.startswith("order_"))
async def start_order(callback: types.CallbackQuery, state: FSMContext):
    vendor_id = int(callback.data.replace("order_", ""))
    
    await state.update_data(vendor_id=vendor_id)
    await callback.message.edit_text(
        "Alright! What do you want to order?\n\n"
        "Be specific:\n"
        "• Quantity (2 plates, 5 shirts)\n"
        "• Preferences (extra spicy, no starch)\n"
        "• Special requests"
    )
    await state.set_state(OrderPlacement.order_details)
    await callback.answer()

@dp.message(OrderPlacement.order_details)
async def process_order_details(message: types.Message, state: FSMContext):
    await state.update_data(order_details=message.text)
    await message.answer(
        "Got it! When do you need this?\n\n"
        "You can say:\n"
        "• \"Today by 6pm\"\n"
        "• \"Tomorrow afternoon\"\n"
        "• \"In 2 hours\"\n"
        "• \"ASAP\""
    )
    await state.set_state(OrderPlacement.deadline)

@dp.message(OrderPlacement.deadline)
async def complete_order(message: types.Message, state: FSMContext):
    data = await state.get_data()
    vendor_id = data['vendor_id']
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("""
        INSERT INTO orders (vendor_id, buyer_id, details, deadline)
        VALUES (?, ?, ?, ?)
    """, (vendor_id, message.from_user.id, data['order_details'], message.text))
    
    order_id = cursor.lastrowid
    
    cursor.execute("SELECT telegram_id, business_name FROM vendors WHERE vendor_id = ?", (vendor_id,))
    vendor = cursor.fetchone()
    conn.commit()
    conn.close()
    
    if vendor:
        vendor_telegram_id, business_name = vendor
        
        buyer_username = f"@{message.from_user.username}" if message.from_user.username else f"User {message.from_user.id}"
        buyer_name = message.from_user.first_name or "Customer"
        
        vendor_msg = (
            f"🔔 New Order!\n\n"
            f"**Order #{order_id}**\n"
            f"From: {buyer_name} ({buyer_username})\n\n"
            f"📝 Order:\n{data['order_details']}\n\n"
            f"⏰ Deadline: {message.text}\n\n"
            f"Contact them: {buyer_username}"
        )
        
        try:
            await bot.send_message(vendor_telegram_id, vendor_msg, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Failed to notify vendor: {e}")
        
        await message.answer(
            f"✅ Order placed!\n\n"
            f"**Order #{order_id}**\n"
            f"Vendor: {business_name}\n\n"
            f"Your Order:\n{data['order_details']}\n\n"
            f"Deadline: {message.text}\n\n"
            f"They've been notified and will reach out soon.\n\n"
            f"I'll check in with you tomorrow to see how it went!",
            parse_mode="Markdown"
        )
        
        asyncio.create_task(schedule_order_followup(order_id, message.from_user.id))
    
    await state.clear()

async def schedule_order_followup(order_id: int, buyer_id: int):
    await asyncio.sleep(86400)
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT status FROM orders WHERE order_id = ?", (order_id,))
    result = cursor.fetchone()
    conn.close()
    
    if result and result[0] == 'pending':
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Yes", callback_data=f"complete_{order_id}")],
            [InlineKeyboardButton(text="❌ No", callback_data=f"incomplete_{order_id}")]
        ])
        
        try:
            await bot.send_message(
                buyer_id,
                f"Hey! Quick check about Order #{order_id}.\n\n"
                f"Did you get everything okay?",
                reply_markup=keyboard
            )
        except Exception as e:
            logger.error(f"Follow-up failed: {e}")

@dp.callback_query(F.data.startswith("complete_"))
async def order_completed(callback: types.CallbackQuery, state: FSMContext):
    order_id = int(callback.data.replace("complete_", ""))
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE orders 
        SET status = 'completed', completed_at = CURRENT_TIMESTAMP 
        WHERE order_id = ?
    """, (order_id,))
    conn.commit()
    conn.close()
    
    await state.update_data(order_id=order_id)
    await callback.message.edit_text(
        "Nice! How was your experience with them?",
        reply_markup=rating_keyboard()
    )
    await state.set_state(RatingState.stars)
    await callback.answer()

@dp.callback_query(RatingState.stars, F.data.startswith("rate_"))
async def process_rating_stars(callback: types.CallbackQuery, state: FSMContext):
    stars = int(callback.data.replace("rate_", ""))
    await state.update_data(stars=stars)
    
    await callback.message.edit_text(
        f"{'⭐' * stars}\n\n"
        f"Wanna leave a quick review? (optional)\n\n"
        f"Type it out or send /skip"
    )
    await state.set_state(RatingState.review)
    await callback.answer()

@dp.message(RatingState.review)
async def process_review(message: types.Message, state: FSMContext):
    data = await state.get_data()
    order_id = data['order_id']
    stars = data['stars']
    review = None if message.text == "/skip" else message.text
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute("SELECT vendor_id FROM orders WHERE order_id = ?", (order_id,))
    vendor_id = cursor.fetchone()[0]
    
    cursor.execute("""
        INSERT INTO ratings (order_id, vendor_id, buyer_id, stars, review_text)
        VALUES (?, ?, ?, ?, ?)
    """, (order_id, vendor_id, message.from_user.id, stars, review))
    
    conn.commit()
    conn.close()
    
    update_vendor_rating(vendor_id)
    
    await message.answer(
        "Thanks! Your feedback helps other students find good vendors.\n\n"
        "Need anything else? Just let me know!"
    )
    await state.clear()

@dp.callback_query(F.data.startswith("incomplete_"))
async def order_incomplete(callback: types.CallbackQuery):
    order_id = int(callback.data.replace("incomplete_", ""))
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE orders 
        SET status = 'flagged' 
        WHERE order_id = ?
    """, (order_id,))
    conn.commit()
    conn.close()
    
    await callback.message.edit_text(
        "That's not great. I've made a note of it.\n\n"
        "Maybe try reaching out to them directly or check out other vendors?\n\n"
        "What else can I help with?"
    )
    await callback.answer()

@dp.message(Command("orderhistory"))
async def cmd_order_history(message: types.Message):
    vendor = get_vendor_by_telegram_id(message.from_user.id)
    
    if not vendor:
        await message.answer(
            "You're not registered as a vendor yet.\n\n"
            "Use /register to get started!"
        )
        return
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT order_id, details, deadline, status, created_at
        FROM orders
        WHERE vendor_id = ?
        ORDER BY created_at DESC
        LIMIT 15
    """, (vendor['vendor_id'],))
    orders = cursor.fetchall()
    conn.close()
    
    if not orders:
        await message.answer("No orders yet. Keep the hustle going!")
        return
    
    history_text = f"📦 Your Recent Orders:\n\n"
    for order in orders:
        oid, details, deadline, status, created = order
        status_emoji = {"pending": "⏳", "completed": "✅", "flagged": "⚠️"}.get(status, "❓")
        short_details = details[:40] + "..." if len(details) > 40 else details
        history_text += f"{status_emoji} Order #{oid}\n{short_details}\nNeeded: {deadline}\n\n"
    
    await message.answer(history_text)

@dp.message(Command("myrating"))
async def cmd_my_rating(message: types.Message):
    vendor = get_vendor_by_telegram_id(message.from_user.id)
    
    if not vendor:
        await message.answer(
            "You're not registered as a vendor yet.\n\n"
            "Use /register to get set up!"
        )
        return
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT stars, review_text, created_at
        FROM ratings
        WHERE vendor_id = ?
        ORDER BY created_at DESC
    """, (vendor['vendor_id'],))
    ratings = cursor.fetchall()
    conn.close()
    
    if not ratings:
        await message.answer(
            f"📊 **{vendor['business_name']}**\n\n"
            f"No ratings yet.\n\n"
            f"Complete a few orders to build your reputation!",
            parse_mode="Markdown"
        )
        return
    
    rating_text = (
        f"📊 **{vendor['business_name']}**\n\n"
        f"⭐ {vendor['avg_rating']:.1f} average\n"
        f"📦 {vendor['total_orders']} completed orders\n\n"
        f"Recent Reviews:\n\n"
    )
    
    for stars, review, created in ratings[:5]:
        rating_text += f"{'⭐' * stars}\n"
        if review:
            rating_text += f'"{review}"\n'
        rating_text += f"{created[:10]}\n\n"
    
    await message.answer(rating_text, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("contact_"))
async def show_contact(callback: types.CallbackQuery):
    vendor_id = int(callback.data.replace("contact_", ""))
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT business_name, contact FROM vendors WHERE vendor_id = ?", (vendor_id,))
    vendor = cursor.fetchone()
    conn.close()
    
    if vendor:
        name, contact = vendor
        await callback.answer(f"📞 {name}\n{contact}", show_alert=True)
    else:
        await callback.answer("Vendor not found", show_alert=True)

# Webhook handlers
async def on_startup(app):
    """Set webhook on startup"""
    await bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True)
    logger.info(f"Webhook set to {WEBHOOK_URL}")

async def on_shutdown(app):
    """Delete webhook on shutdown"""
    await bot.delete_webhook()
    logger.info("Webhook deleted")

async def handle_webhook(request):
    """Handle incoming webhook requests"""
    update = types.Update(**await request.json())
    await dp.feed_update(bot, update)
    return web.Response()

async def health_check(request):
    """Health check endpoint"""
    return web.Response(text="Bot is running!")

def main():
    """Main entry point for webhook mode"""
    init_db()
    
    app = web.Application()
    app.router.add_post(WEBHOOK_PATH, handle_webhook)
    app.router.add_get("/", health_check)
    
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    
    logger.info(f"Starting webhook server on port {PORT}")
    web.run_app(app, host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()