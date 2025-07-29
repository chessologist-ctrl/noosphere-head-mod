import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
import os
import base64
import json
import io
import flask
from threading import Thread
from datetime import datetime
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from dotenv import load_dotenv

# ------------------ Flask Keep Alive ------------------ #
app = flask.Flask('')

@app.route('/')
def home():
    return "Bot is alive!"

def run():
    app.run(host='0.0.0.0', port=8080)

Thread(target=run).start()

# ------------------ Load ENV ------------------ #
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GOOGLE_DOC_ID = os.getenv("GOOGLE_DOC_ID")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
creds_json = os.getenv("GOOGLE_CREDS_JSON")

# ------------------ Google Setup ------------------ #
creds_dict = json.loads(creds_json)
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, [
    'https://spreadsheets.google.com/feeds',
    'https://www.googleapis.com/auth/drive',
    'https://www.googleapis.com/auth/documents'
])
gc = gspread.authorize(creds)
sheet = gc.open_by_key(GOOGLE_SHEET_ID).sheet1

drive_service = build('drive', 'v3', credentials=creds)
docs_service = build('docs', 'v1', credentials=creds)

# ------------------ Discord Setup ------------------ #
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ------------------ Announcement Utility ------------------ #
async def fetch_announcement_content():
    doc = docs_service.documents().get(documentId=GOOGLE_DOC_ID).execute()
    content = ""
    image_urls = []

    def extract_text(elements):
        nonlocal content
        for value in elements:
            if "paragraph" in value:
                for elem in value["paragraph"]["elements"]:
                    text_run = elem.get("textRun")
                    if text_run:
                        content += text_run["content"]
            elif "inlineObjectElement" in value:
                object_id = value["inlineObjectElement"]["inlineObjectId"]
                inline_object = doc["inlineObjects"][object_id]
                embedded_object = inline_object["inlineObjectProperties"]["embeddedObject"]
                if "imageProperties" in embedded_object:
                    image_source = embedded_object["imageProperties"]["contentUri"]
                    image_urls.append(image_source)

    extract_text(doc["body"]["content"])
    return content.strip(), image_urls

async def download_images(image_urls):
    files = []
    for url in image_urls:
        try:
            file_id = url.split("d/")[1].split("/")[0]
        except:
            continue
        try:
            request = drive_service.files().get_media(fileId=file_id)
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while done is False:
                status, done = downloader.next_chunk()
            fh.seek(0)
            filename = f"image_{len(files)}.png"
            files.append(discord.File(fh, filename=filename))
        except:
            continue
    return files

# ------------------ Announcement Commands ------------------ #
@bot.command(name="announce")
async def announce_prefix(ctx):
    await send_announcement(ctx.channel)

@tree.command(name="announce", description="Send announcement from Google Doc")
async def announce_slash(interaction: discord.Interaction):
    await interaction.response.defer()
    await send_announcement(interaction.channel)

async def send_announcement(channel):
    try:
        content, image_urls = await fetch_announcement_content()
        files = await download_images(image_urls)
        if files:
            await channel.send(content=content, files=files)
        else:
            embed = discord.Embed(description=content, color=discord.Color.blue())
            await channel.send(embed=embed)
    except Exception as e:
        await channel.send(f"Failed to send announcement: {e}")

# ------------------ DM Complaint Logger ------------------ #
@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    if isinstance(message.channel, discord.DMChannel):
        user_id = str(message.author.id)
        content = message.content
        date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        attachments_text = ""
        for attachment in message.attachments:
            attachments_text += f"\n{attachment.url}"
        complaint = content + attachments_text
        sheet.append_row([user_id, complaint, date, "", "", "", "", "", ""])
        await message.reply("✅ Your complaint has been received and recorded. Thank you!")

    await bot.process_commands(message)

# ------------------ Revert Checker ------------------ #
@tasks.loop(minutes=5)
async def check_reverts():
    records = sheet.get_all_records()
    for i, row in enumerate(records):
        revert_message = row.get("Revert")
        revert_sent = row.get("Revert Sent")
        user_id = row.get("User Id")

        if revert_message and revert_sent != "done":
            try:
                user = await bot.fetch_user(int(user_id))
                text_parts = []
                files = []
                for part in revert_message.split("\n"):
                    if part.strip().startswith("http") and any(ext in part for ext in [".jpg", ".png", ".jpeg", ".gif"]):
                        try:
                            async with bot.http._HTTPClient__session.get(part.strip()) as resp:
                                if resp.status == 200:
                                    data = await resp.read()
                                    file = discord.File(io.BytesIO(data), filename=part.strip().split("/")[-1])
                                    files.append(file)
                        except:
                            text_parts.append(part.strip())
                    else:
                        text_parts.append(part.strip())
                reply_text = "\n".join(text_parts)
                await user.send(content=reply_text or None, files=files if files else None)
                sheet.update_cell(i + 2, 9, "done")  # Column I = 'Revert Sent'
            except Exception as e:
                print(f"Failed to send revert to {user_id}: {e}")

# ------------------ Bot Ready ------------------ #
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    try:
        synced = await tree.sync()
        print(f"✅ Synced {len(synced)} slash commands.")
    except Exception as e:
        print(f"❌ Sync error: {e}")
    check_reverts.start()

bot.run(DISCORD_TOKEN)
