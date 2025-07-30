import os
import json
import asyncio
import io
import threading
from datetime import datetime

import discord
from discord.ext import commands, tasks
from discord import app_commands
from flask import Flask
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from dotenv import load_dotenv
import gspread

# Load environment variables
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GOOGLE_CREDS_JSON = json.loads(os.getenv("GOOGLE_CREDS_JSON"))
GOOGLE_DOC_ID = os.getenv("GOOGLE_DOC_ID")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")

intents = discord.Intents.default()
intents.messages = True
intents.guilds = True
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# -------------------- Flask Keep-Alive -------------------- #
app = Flask(_name_)

@app.route("/")
def index():
    return "Bot is alive!"

def run_flask():
    app.run(host="0.0.0.0", port=8080)

threading.Thread(target=run_flask).start()

# -------------------- Google API Setup -------------------- #
credentials = service_account.Credentials.from_service_account_info(
    GOOGLE_CREDS_JSON,
    scopes=["https://www.googleapis.com/auth/documents.readonly",
            "https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/spreadsheets"]
)
docs_service = build("docs", "v1", credentials=credentials)
drive_service = build("drive", "v3", credentials=credentials)
gc = gspread.authorize(credentials)
sheet = gc.open_by_key(GOOGLE_SHEET_ID).sheet1

# -------------------- Utility Functions -------------------- #
async def fetch_doc_content_and_images():
    doc = docs_service.documents().get(documentId=GOOGLE_DOC_ID).execute()
    body = doc.get("body", {}).get("content", [])

    text_chunks = []
    image_files = []

    for value in body:
        if "paragraph" in value:
            elements = value["paragraph"]["elements"]
            for elem in elements:
                text_run = elem.get("textRun")
                if text_run:
                    text_chunks.append(text_run["content"])
        elif "inlineObjectElement" in value:
            inline_id = value["inlineObjectElement"]["inlineObjectId"]
            try:
                obj = doc["inlineObjects"][inline_id]
                embed = obj["inlineObjectProperties"]["embeddedObject"]
                if "imageProperties" in embed:
                    img_source = embed["imageProperties"]["contentUri"]
                    file_id = img_source.split("=d/")[-1].split("/")[0]
                    request = drive_service.files().get_media(fileId=file_id)
                    fh = io.BytesIO()
                    downloader = MediaIoBaseDownload(fh, request)
                    done = False
                    while not done:
                        _, done = downloader.next_chunk()
                    fh.seek(0)
                    image_files.append(discord.File(fh, filename=f"{file_id}.jpg"))
            except Exception as e:
                print(f"[ERROR] Image fetch failed: {e}")

    text = "".join(text_chunks).strip()
    return text, image_files

def convert_mentions(text, guild: discord.Guild):
    for role in guild.roles:
        if role.name in text:
            text = text.replace(f"@{role.name}", role.mention)
    return text

# -------------------- Announcement Commands -------------------- #
@bot.command(name="announce")
async def prefix_announce(ctx):
    await send_announcement(ctx.channel, ctx.guild)

@tree.command(name="announce", description="Send an announcement from the Google Doc")
@app_commands.describe(channel="Channel to post the announcement in")
async def slash_announce(interaction: discord.Interaction, channel: discord.TextChannel):
    await interaction.response.send_message(f"📢 Sending announcement to {channel.mention}...", ephemeral=True)
    await send_announcement(channel, interaction.guild)

async def send_announcement(channel, guild):
    text, image_files = await fetch_doc_content_and_images()
    text = convert_mentions(text, guild)

    if image_files:
        await channel.send(content=text, files=image_files)
    else:
        await channel.send(content=text)

# -------------------- Revert Message Task -------------------- #
@tasks.loop(minutes=5)
async def check_reverts():
    try:
        all_rows = sheet.get_all_records()
        for i, row in enumerate(all_rows, start=2):  # start=2 to account for header
            revert_message = row.get("Revert", "").strip()
            revert_sent = row.get("Revert Sent", "").strip().lower()
            user_id = row.get("User Id")

            if revert_message and revert_sent != "done":
                try:
                    user = await bot.fetch_user(int(user_id))
                    await user.send(revert_message)
                    sheet.update_cell(i, 9, "done")  # 'Revert Sent' column = I = 9
                    print(f"[INFO] Revert sent to {user_id}")
                except Exception as e:
                    print(f"[ERROR] Failed to send revert to {user_id}: {e}")
    except Exception as e:
        print(f"[ERROR] Revert check failed: {e}")

# -------------------- On Ready -------------------- #
@bot.event
async def on_ready():
    print(f"✅ Bot is online as {bot.user}")
    try:
        synced = await tree.sync()
        print(f"✅ Synced {len(synced)} slash commands")
    except Exception as e:
        print(f"[ERROR] Slash command sync failed: {e}")
    check_reverts.start()

bot.run(TOKEN)