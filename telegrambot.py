import asyncio
import html
import logging
from datetime import datetime, timedelta
from dotenv import load_dotenv
import os
import random
import telebot
from telebot.async_telebot import AsyncTeleBot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

import content
import kai
import price
import xfeed

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

load_dotenv()

# All user-facing text lives in content/*.yml (updated via pull request).
TEXTS, CONTENT_COMMANDS, MENUS, PROJECTS = content.load()

bot = AsyncTeleBot(os.environ['TELEGRAM_BOT_TOKEN'])
# Unverified members: user_id -> chat_id of the group that issued the
# captcha. The chat binding stops a pending user from clearing their
# state by answering the captcha somewhere else (e.g. in a DM).
new_users = {}
# Users whose captcha outcome (kick or welcome) is currently being
# processed. They stay in new_users — and therefore gated — until
# enforcement has actually succeeded; the claim only prevents the
# same user from being processed twice concurrently.
captcha_claimed = set()
new_users_lock = asyncio.Lock()

# Configuration
CAPTCHA_TIMEOUT = 180  # 3 minutes
BAN_DURATION_DAYS = 7
REQUEST_TIMEOUT = 10

# Main group for X auto-posts; unset disables the auto-poster.
MAIN_CHAT_ID = os.environ.get('MAIN_CHAT_ID', '').strip()
X_POLL_SECONDS = max(60, int(os.environ.get('X_POLL_SECONDS', '300')))


def mention(user):
    """Readable, HTML-safe reference to a user (usernames are optional)."""
    if user.username:
        return f'@{user.username}'
    if user.first_name:
        return html.escape(user.first_name, quote=False)
    return f'User{user.id}'


def create_main_menu_keyboard():
    """Create a modern inline keyboard for main navigation"""
    keyboard = InlineKeyboardMarkup(row_width=2)

    # Row 1: Essential commands
    keyboard.add(
        InlineKeyboardButton("📚 Guides", callback_data="guides"),
        InlineKeyboardButton("🔗 Projects", callback_data="projects")
    )

    # Row 2: Trading & Info
    keyboard.add(
        InlineKeyboardButton("💱 Exchanges", callback_data="exchanges"),
        InlineKeyboardButton("💳 Wallets", callback_data="wallets")
    )

    # Row 3: Community & Support
    keyboard.add(
        InlineKeyboardButton("🌍 International", callback_data="international"),
        InlineKeyboardButton("📱 Social Media", callback_data="social")
    )

    # Row 4: Advanced
    keyboard.add(
        InlineKeyboardButton("🔥 Stake/Burn", callback_data="stake"),
        InlineKeyboardButton("📄 Whitepaper", callback_data="whitepaper")
    )

    return keyboard


async def send_message(chat_id, message, link_preview=False, html=True, reply_markup=None, reply_to=None, thread_id=None):
    """Universal message sender that uses the provided chat_id."""
    reply_parameters = None
    if reply_to is not None:
        # Replying places the message in the right forum topic;
        # thread_id keeps it there even if the original is deleted
        # while we wait (allow_sending_without_reply).
        reply_parameters = telebot.types.ReplyParameters(
            message_id=reply_to, allow_sending_without_reply=True)
    try:
        return await bot.send_message(
            chat_id,
            message,
            parse_mode='HTML' if html else None,
            link_preview_options=telebot.types.LinkPreviewOptions(is_disabled=not link_preview),
            reply_markup=reply_markup,
            reply_parameters=reply_parameters,
            message_thread_id=thread_id
        )
    except Exception as e:
        logger.error(f"Failed to send message to {chat_id}: {e}")
        return None


async def schedule_message_deletion(chat_id, message_id, delay_seconds=60):
    """Schedules a message to be deleted after a specified delay."""
    await asyncio.sleep(delay_seconds)
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception as e:
        logger.warning(f"Could not delete message {message_id} from chat {chat_id}: {e}")

# --- Main Handlers ---

# The captcha gate MUST be the first registered message handler:
# pyTelegramBotAPI stops at the first match, so registering it first
# means no other handler — commands included — ever runs for an
# unverified user. It covers media too, or a pending spammer could
# simply post a photo/sticker with a phishing caption.
# Everything a user can post: the library's media list plus the
# forwardable giveaway types (classified as "service" upstream) and
# paid media. Unknown names are harmless — they simply never match.
GATED_CONTENT_TYPES = telebot.util.content_type_media + [
    'giveaway', 'giveaway_winners', 'paid_media',
]


@bot.message_handler(func=lambda m: m.from_user is not None and m.from_user.id in new_users,
                     content_types=GATED_CONTENT_TYPES)
async def captcha_gate(message):
    """Intercepts all messages from unverified users, enforcing the captcha."""
    async with new_users_lock:
        pending = message.from_user.id in new_users
    if not pending:
        return

    try:
        await bot.delete_message(message.chat.id, message.id)
    except:
        pass

    # Only a text reply to the captcha counts as an answer attempt —
    # media replies are ordinary violations.
    if message.content_type == 'text' and message.reply_to_message is not None:
        await handle_captcha_response(message)
    else:
        logger.warning(f"User {message.from_user.username} ({message.from_user.id}) tried to send message before completing captcha")
        warning_msg = await send_message(
            message.chat.id,
            f"⚠️ <b>{mention(message.from_user)}</b>, please complete the security check first!"
        )
        await asyncio.sleep(3)
        try:
            await bot.delete_message(warning_msg.chat.id, warning_msg.message_id)
        except:
            pass


@bot.message_handler(content_types=['new_chat_members'])
async def handle_welcome(message):
    """Handles new members, presenting them with a captcha."""
    current_chat_id = message.chat.id

    # Register the members as unverified BEFORE any await, so a member
    # who posts immediately cannot race past the captcha gate.
    async with new_users_lock:
        for member in message.new_chat_members:
            new_users[member.id] = current_chat_id

    try:
        await bot.delete_message(current_chat_id, message.id)
    except:
        pass  # Bot may not have admin rights to delete, proceed anyway

    try:
        from_user = await bot.get_chat_member(current_chat_id, message.from_user.id)
        is_admin = from_user.status in ['creator', 'administrator']
    except Exception as e:
        logger.warning(f"get_chat_member failed, treating adder as non-admin: {e}")
        is_admin = False

    # If added by an admin or the owner, welcome them directly
    if is_admin:
        async with new_users_lock:
            for member in message.new_chat_members:
                new_users.pop(member.id, None)
        await welcome_new_users(message, message.new_chat_members)
        return

    # For all other new members, present the captcha challenge
    markup = telebot.types.ReplyKeyboardMarkup(one_time_keyboard=True, selective=True, resize_keyboard=True)
    options = ['🔮 Koinos', '₿ Bitcoin', '🔷 Ethereum']
    random.shuffle(options)
    markup.add(*options)

    captcha_messages = []
    for member in message.new_chat_members:
        welcome_text = f"""🎉 <b>Welcome {mention(member)}!</b>

🛡️ <i>Quick security check:</i>
What is the name of this blockchain project?

⏰ <i>You have 3 minutes to respond...</i>"""

        captcha_msg = await send_message(current_chat_id, welcome_text, reply_markup=markup)
        if captcha_msg:
            captcha_messages.append(captcha_msg)

    # Wait for the timeout and then clean up
    await asyncio.sleep(CAPTCHA_TIMEOUT)
    for captcha_message in captcha_messages:
        try:
            await bot.delete_message(captcha_message.chat.id, captcha_message.message_id)
        except:
            pass

    async with new_users_lock:
        expired = [m for m in message.new_chat_members
                   if new_users.get(m.id) == current_chat_id
                   and m.id not in captcha_claimed]
        for member in expired:
            captcha_claimed.add(member.id)
    # Kick outside the lock — kick_user awaits the Telegram API. The
    # user stays registered (and gated) until the kick has actually
    # succeeded; on failure they simply remain pending.
    for member in expired:
        kicked = await kick_user(current_chat_id, member)
        async with new_users_lock:
            captcha_claimed.discard(member.id)
            if kicked:
                new_users.pop(member.id, None)


@bot.message_handler(commands=['info', 'start', 'menu'])
async def send_info(message):
    """Displays the main info menu and deletes the user's command."""
    try:
        await bot.delete_message(message.chat.id, message.message_id)
    except Exception as e:
        logger.warning(f"Could not delete command message: {e}")

    sent_message = await send_message(message.chat.id, TEXTS['main_menu'],
                                      reply_markup=create_main_menu_keyboard())
    if sent_message:
        asyncio.create_task(schedule_message_deletion(sent_message.chat.id, sent_message.message_id))


@bot.message_handler(commands=['report'])
async def send_report(message):
    """Alerts administrators."""
    # The moderator handles live in content/commands.yml so the list can be
    # corrected by pull request instead of a rebuild. The fallback keeps
    # /report working if the key is ever removed.
    # content.py only HTML-validates texts.main_menu and texts.welcome, so
    # this value is escaped rather than trusted: it is a list of handles,
    # never markup, and a content edit must not be able to break /report.
    mods = html.escape(str(TEXTS.get('report_mods') or
                           '@kuixihe @weleleliano @saleh_hawi'), quote=False)
    report_text = """🚨 <b>ADMIN ALERT</b> 🚨

<b>Someone needs attention from moderators:</b>
{mods}

⚠️ <i>Reported by:</i> {username}
🕐 <i>Time:</i> {time}""".format(
        mods=mods,
        username=mention(message.from_user),
        time=datetime.now().strftime("%H:%M:%S")
    )

    await send_message(message.chat.id, report_text)


# --- Projects & Updates (rendered from content/projects.yml) ---

@bot.message_handler(commands=['projects'])
async def handle_projects(message):
    """Lists all ecosystem projects, grouped by category."""
    await send_message(message.chat.id, content.render_projects_overview(PROJECTS))


@bot.message_handler(commands=['project'])
async def handle_project(message):
    """Shows details and latest updates for a single project."""
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        ids = ', '.join(p['id'] for p in PROJECTS)
        if len(ids) > 3500:
            ids = ids[:3500] + '…'
        await send_message(
            message.chat.id,
            f'🔎 <b>Usage:</b> /project &lt;name&gt;\n\n<b>Available:</b> {ids}'
        )
        return
    project = content.find_project(PROJECTS, parts[1])
    if project is None:
        safe_query = html.escape(parts[1], quote=False)
        await send_message(
            message.chat.id,
            f'❓ No project found for "{safe_query}". Try /projects for the full list.'
        )
        return
    await send_message(message.chat.id, content.render_project_detail(project))


@bot.message_handler(commands=['updates'])
async def handle_updates(message):
    """Shows the latest updates across all projects."""
    await send_message(message.chat.id, content.render_updates(PROJECTS))


@bot.message_handler(commands=['x'])
async def handle_x(message):
    """Shows the latest X post from @KoinosNetwork."""
    post = await xfeed.get_latest_cached()
    if post is None:
        await send_message(message.chat.id, xfeed.fallback_message(), link_preview=True)
        return
    await send_message(
        message.chat.id,
        xfeed.format_post(post, f'🐦 <b>Latest post from {xfeed.PROFILE_NAME}</b>'),
        link_preview=True,
    )


# --- Static commands (defined in content/commands.yml) ---

def _register_content_commands():
    for name, cfg in CONTENT_COMMANDS.items():
        commands = [name, *cfg.get('aliases', [])]

        async def handler(message, _text=cfg['text'],
                          _preview=cfg.get('link_preview', False)):
            # price.render() is a no-op unless the body carries the
            # {price} placeholder, and never raises.
            body = await price.render(_text)
            await send_message(message.chat.id, body, link_preview=_preview)

        bot.message_handler(commands=commands)(handler)


_register_content_commands()


# --- Menu Redirects ---
# Commands that are part of the main menu buttons redirect to the main menu.
@bot.message_handler(commands=[
    'guides', 'docs', 'international', 'exchange', 'exchanges', 'cex',
    'buy', 'media', 'social', 'stake', 'whitepaper', 'wallets'
])
async def handle_menu_redirects(message):
    """Handles commands that are now buttons in the main menu by showing the menu."""
    await send_info(message)


# --- Kai (@kai) — AI assistant via the Koinos AI worker network ---

async def _typing_loop(chat_id, thread_id):
    """Re-sends the typing indicator while Kai waits on the network;
    without it the bot looks dead during a cold model load."""
    while True:
        try:
            await bot.send_chat_action(chat_id, 'typing', message_thread_id=thread_id)
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.debug(f'typing indicator failed: {e}')
        await asyncio.sleep(4)


@bot.message_handler(func=lambda m: kai.is_trigger(m.text), content_types=['text'])
async def handle_kai(message):
    """Answers @kai mentions in the main group via the Koinos AI network.

    Unverified users never reach this handler — the captcha gate is
    registered first and pyTelegramBotAPI stops at the first match.
    """
    if not kai.enabled():
        return
    chat_id = message.chat.id
    user_id = message.from_user.id
    if not MAIN_CHAT_ID or str(chat_id) != MAIN_CHAT_ID:
        # Kai is exclusive to the official group; in DMs say where to
        # find it (cooldown-gated so DMs can't farm bot output),
        # everywhere else stay silent.
        if message.chat.type == 'private' and kai.user_cooldown_remaining(user_id) == 0:
            await send_message(chat_id, kai.GROUP_ONLY_TEXT)
        return

    thread_id = message.message_thread_id if getattr(message, 'is_topic_message', False) else None

    # Every Kai interaction — including help and error notices —
    # consumes the per-user cooldown, so no reply path can be spammed.
    cooldown = kai.user_cooldown_remaining(user_id)
    if cooldown:
        if kai.should_notify_cooldown(user_id):
            notice = await send_message(
                chat_id,
                f'🕐 {mention(message.from_user)}, one question per '
                f'{kai.cooldown_seconds()}s — try again in {cooldown}s.',
                reply_to=message.message_id, thread_id=thread_id)
            if notice:
                asyncio.create_task(schedule_message_deletion(
                    notice.chat.id, notice.message_id, delay_seconds=8))
        return
    question = kai.extract_question(message.text)
    if not question:
        # Bare "@kai" → the live model list (falls back to plain help
        # when the gateway is unreachable).
        ids = await kai.list_models()
        await send_message(
            chat_id,
            kai.render_models(ids) if ids else kai.HELP_TEXT,
            reply_to=message.message_id, thread_id=thread_id)
        return
    # "@kai <model> <question>" → that model; otherwise the default.
    model, question = await kai.split_model_prefix(question)
    if model and not question:
        await send_message(
            chat_id,
            f'ℹ️ Add a question after the model, e.g. '
            f'<code>@kai {html.escape(model.rsplit(":", 1)[-1], quote=False)} what is mana?</code>',
            reply_to=message.message_id, thread_id=thread_id)
        return
    # Admission first, then the quota window: a busy rejection must not
    # charge the 15-minute window.
    if not kai.acquire_slot():
        if kai.busy_notice_allowed():
            await send_message(chat_id, kai.BUSY_TEXT,
                               reply_to=message.message_id, thread_id=thread_id)
        return
    if not kai.window_allows():
        kai.release_slot()
        if kai.quota_notice_allowed():
            await send_message(chat_id, kai.QUOTA_TEXT,
                               reply_to=message.message_id, thread_id=thread_id)
        return

    typing_task = asyncio.create_task(_typing_loop(chat_id, thread_id))
    try:
        result = await kai.ask(question, model)
    finally:
        kai.release_slot()
        typing_task.cancel()
    await send_message(chat_id, result['text'],
                       reply_to=message.message_id, thread_id=thread_id)


# --- Helper Functions ---

async def handle_captcha_response(message):
    """Handles the user's response to the captcha question."""
    user_id = message.from_user.id
    # Claim the user without unregistering them: they stay gated while
    # their outcome is processed, and a double submission returns here
    # instead of being processed twice. The answer only counts in the
    # chat whose captcha is pending.
    async with new_users_lock:
        if new_users.get(user_id) != message.chat.id or user_id in captcha_claimed:
            return
        captcha_claimed.add(user_id)

    try:
        await bot.delete_message(message.chat.id, message.reply_to_message.id)
        await bot.delete_message(message.chat.id, message.id)
    except:
        pass

    correct_answers = ['🔮 Koinos', 'Koinos', 'koinos', 'KOINOS']
    if message.text not in correct_answers:
        goodbye_msg = await send_message(
            message.chat.id,
            f"❌ <b>Incorrect answer, {mention(message.from_user)}</b>\n\n"
            f"🚪 <i>Please try again when you're ready to join our community!</i>"
        )
        await asyncio.sleep(2)
        try:
            await bot.delete_message(goodbye_msg.chat.id, goodbye_msg.message_id)
        except:
            pass
        kicked = await kick_user(message.chat.id, message.from_user)
        async with new_users_lock:
            captcha_claimed.discard(user_id)
            # Unregister only after a successful kick — otherwise the
            # user stays gated instead of silently verified.
            if kicked:
                new_users.pop(user_id, None)
        return

    async with new_users_lock:
        captcha_claimed.discard(user_id)
        new_users.pop(user_id, None)
    await welcome_new_users(message, [message.from_user])


async def kick_user(chat_id, user):
    """Kicks a user from the chat. Returns True on success."""
    try:
        await bot.kick_chat_member(chat_id, user.id, until_date=datetime.today() + timedelta(days=BAN_DURATION_DAYS))
        logger.info(f"Kicked user {user.username} ({user.id}) for failing captcha")
        return True
    except Exception as e:
        logger.error(f"Failed to kick user {user.username}: {e}")
        return False


async def welcome_new_users(message, users):
    """Sends a welcome message to verified new users."""
    usernames = [mention(user) for user in users]
    if len(usernames) > 1:
        usernames[-1] = 'and ' + usernames[-1]
    username_list = ', '.join(usernames) if len(usernames) > 2 else ' '.join(usernames)

    help_text = TEXTS['welcome'].replace('{usernames}', username_list)

    sent_message = await send_message(
        message.chat.id,
        help_text,
        reply_markup=create_main_menu_keyboard()
    )
    if sent_message:
        asyncio.create_task(schedule_message_deletion(sent_message.chat.id, sent_message.message_id))


@bot.message_handler(content_types=['left_chat_member'])
async def delete_leave_message(message):
    """Cleans up "user has left" messages."""
    try:
        await bot.delete_message(message.chat.id, message.id)
    except:
        pass

# --- Callback Query Handler ---

@bot.callback_query_handler(func=lambda call: True)
async def handle_callback_query(call):
    """Handles all inline keyboard button presses."""
    try:
        if call.data == "main_menu":
            text = TEXTS['main_menu']
        elif call.data == "projects":
            text = content.render_projects_overview(PROJECTS)
        else:
            text = MENUS.get(call.data, "")

        if text:
            await bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                                        parse_mode='HTML', reply_markup=create_main_menu_keyboard())

    except Exception as e:
        logger.error(f"Callback error: {e}")

    await bot.answer_callback_query(call.id)

# --- Main Execution ---

async def main():
    logger.info("🚀 Koinos Bot starting up...")
    if MAIN_CHAT_ID:
        asyncio.create_task(
            xfeed.autopost_loop(send_message, int(MAIN_CHAT_ID), X_POLL_SECONDS))
    else:
        logger.info("MAIN_CHAT_ID not set — X auto-posting disabled")
    if kai.enabled() and MAIN_CHAT_ID:
        logger.info("Kai (@kai) enabled for the main group")
    else:
        logger.info("Kai (@kai) disabled (KAI_API_URL or MAIN_CHAT_ID not set)")
    try:
        await bot.polling(non_stop=True)
    except (KeyboardInterrupt, SystemExit):
        logger.info("🛑 Koinos Bot shutting down...")
        await bot.stop_polling()
        await bot.close_session()

if __name__ == '__main__':
    asyncio.run(main())
