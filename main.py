from flask import Flask
import threading
import io
import os
import tempfile

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes
)

from PIL import Image
from mutagen.id3 import ID3, APIC, TIT2, TPE1, error as ID3Error
from mutagen.mp3 import MP3
from mutagen.mp3 import HeaderNotFoundError as MP3HeaderNotFoundError

try:
    from pydub import AudioSegment
    HAVE_PYDUB = True
except Exception:
    HAVE_PYDUB = False

# Session data
user_audio_path = {}
user_image_path = {}
user_title = {}
user_artist = {}
user_waiting_for = {}  # "title", "artist", "image"
user_processed = {}    # To track if user has finished processing

INTRO_TEXT = (
    "Hi! 😎 I can add an image on an audio file, or change its title and artist name. Just send me an audio file to start! 🎵\n\n"
    "Developer: Rayan"
)

# Flask keep-alive
app_server = Flask('')

@app_server.route('/')
def home():
    return "Bot is running!"

def run_server():
    app_server.run(host='0.0.0.0', port=8080)

def keep_alive():
    thread = threading.Thread(target=run_server)
    thread.start()

# Utilities
def _is_mp3(path: str) -> bool:
    try:
        _ = MP3(path)
        return True
    except MP3HeaderNotFoundError:
        return False
    except Exception:
        return False

def _convert_to_mp3_if_needed(src_path: str) -> str:
    if _is_mp3(src_path):
        return src_path
    if not HAVE_PYDUB:
        raise RuntimeError("Your audio isn't MP3 and conversion is unavailable.")
    audio = AudioSegment.from_file(src_path)
    mp3_path = src_path + ".mp3"
    audio.export(mp3_path, format="mp3", bitrate="192k")
    return mp3_path

def _prepare_cover_image_to_jpeg_bytes(image_path: str, max_size: int = 1000) -> bytes:
    with Image.open(image_path) as im:
        im = im.convert("RGB")
        im.thumbnail((max_size, max_size))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=90, optimize=True)
        return buf.getvalue()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(INTRO_TEXT)

async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    tg_file = await msg.audio.get_file()
    ext = os.path.splitext(tg_file.file_path or "")[1].lower() or ".bin"
    tmp_audio = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
    tmp_audio_path = tmp_audio.name
    tmp_audio.close()
    await tg_file.download_to_drive(tmp_audio_path)

    user_id = msg.from_user.id
    user_audio_path[user_id] = tmp_audio_path
    user_image_path.pop(user_id, None)
    user_title.pop(user_id, None)
    user_artist.pop(user_id, None)
    user_processed[user_id] = False

    await ask_next_action(update, user_id)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()

    if user_processed.get(user_id):
        # If already processed, no further action needed
        await query.message.reply_text("✅ You’ve already processed this audio. Send a new one to start again.")
        return

    if query.data == "setimage":
        user_waiting_for[user_id] = "image"
        await query.message.reply_text("Please send me the image you want as cover.")
    elif query.data == "settitle":
        user_waiting_for[user_id] = "title"
        await query.message.reply_text("Please type the title you want.")
    elif query.data == "setartist":
        user_waiting_for[user_id] = "artist"
        await query.message.reply_text("Please type the artist name you want.")
    elif query.data == "finish":
        await process_and_send(update, context, user_id)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_id not in user_waiting_for:
        return
    mode = user_waiting_for.pop(user_id)
    if mode == "title":
        user_title[user_id] = update.message.text
        await update.message.reply_text(f"Title set to: {update.message.text}")
    elif mode == "artist":
        user_artist[user_id] = update.message.text
        await update.message.reply_text(f"Artist set to: {update.message.text}")
    await ask_next_action(update, user_id)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if user_waiting_for.get(user_id) != "image":
        return
    user_waiting_for.pop(user_id, None)
    tg_file = await update.message.photo[-1].get_file()
    tmp_img = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
    tmp_img_path = tmp_img.name
    tmp_img.close()
    await tg_file.download_to_drive(tmp_img_path)
    user_image_path[user_id] = tmp_img_path
    await update.message.reply_text("Image saved!")
    await ask_next_action(update, user_id)

async def ask_next_action(update_or_msg, user_id):
    if user_processed.get(user_id):
        return  # no menu after finishing

    keyboard = [
        [InlineKeyboardButton("🎨 Set Image", callback_data="setimage")],
        [InlineKeyboardButton("🎵 Set Title", callback_data="settitle")],
        [InlineKeyboardButton("🎤 Set Artist", callback_data="setartist")],
        [InlineKeyboardButton("✅ Finish & Get File", callback_data="finish")]
    ]
    if hasattr(update_or_msg, "message"):
        await update_or_msg.message.reply_text("What next?",
            reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update_or_msg.reply_text("What next?",
            reply_markup=InlineKeyboardMarkup(keyboard))

async def process_and_send(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id):
    if user_id not in user_audio_path:
        await update.callback_query.message.reply_text("No audio found in your session.")
        return

    audio_path_original = user_audio_path[user_id]
    image_path = user_image_path.get(user_id)

    try:
        audio_path_for_tagging = _convert_to_mp3_if_needed(audio_path_original)
    except Exception as e:
        await update.callback_query.message.reply_text(f"❌ Error converting audio: {e}")
        return

    try:
        audio = MP3(audio_path_for_tagging, ID3=ID3)
        if audio.tags is None:
            audio.add_tags()

        # Only overwrite metadata if user set it
        if image_path:
            cover_bytes = _prepare_cover_image_to_jpeg_bytes(image_path, max_size=1000)
            for k in list(audio.tags.keys()):
                if k.startswith("APIC"):
                    del audio.tags[k]
            audio.tags.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover_bytes))

        # Preserve existing tags if user didn't set them
        existing_title = audio.tags.get('TIT2')
        existing_artist = audio.tags.get('TPE1')

        if user_id in user_title and user_title[user_id]:
            audio.tags.add(TIT2(encoding=3, text=[user_title[user_id]]))
        elif existing_title:
            audio.tags.add(existing_title)

        if user_id in user_artist and user_artist[user_id]:
            audio.tags.add(TPE1(encoding=3, text=[user_artist[user_id]]))
        elif existing_artist:
            audio.tags.add(existing_artist)

        audio.save(v2_version=3)

        filename = os.path.basename(audio_path_original)
        if not filename.lower().endswith(".mp3"):
            filename = os.path.splitext(filename)[0] + ".mp3"

        with open(audio_path_for_tagging, "rb") as f:
            await update.callback_query.message.reply_document(document=f, filename=filename)
        await update.callback_query.message.reply_text("✅ Done! Your audio is ready.")

        user_processed[user_id] = True

    except Exception as e:
        await update.callback_query.message.reply_text(f"❌ Tagging failed: {e}")
    finally:
        # cleanup
        try: os.unlink(audio_path_for_tagging)
        except: pass
        if audio_path_for_tagging != audio_path_original:
            try: os.unlink(audio_path_original)
            except: pass
        if image_path:
            try: os.unlink(image_path)
            except: pass
        user_audio_path.pop(user_id, None)
        user_image_path.pop(user_id, None)
        user_title.pop(user_id, None)
        user_artist.pop(user_id, None)

import os  # make sure this is at the top of your file

def main():
    print("Starting bot…")
    app = Application.builder().token(os.environ["BOT_TOKEN"]).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.AUDIO, handle_audio))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    print("Bot is polling.")
    app.run_polling()

if __name__ == "__main__":
    keep_alive()
    main()
