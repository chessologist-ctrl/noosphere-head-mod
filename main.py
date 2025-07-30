
import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
import io
import json
import asyncio
from datetime import datetime
from flask import Flask
import threading

import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from dotenv import load_dotenv

# ------------------ Load ENV ------------------ #
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GOOGLE_DOC_ID = os.getenv("GOOGLE_DOC_ID")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
GOOGLE_CREDS_JSON = json.loads(os.getenv("GOOGLE_CREDS_JSON"))

# ------------------ Discord Setup ------------------ #
intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.dm_messages = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ------------------ Google Clients ------------------ #
creds = Credentials.from_service_account_info(GOOGLE_CREDS_JSON)
drive_service = build("drive", "v3", credentials=creds)
docs_service = build("docs", "v1", credentials=creds)
gc = gspread.authorize(creds)
sheet = gc.open_by_key(GOOGLE_SHEET_ID).sheet1

# ------------------ Announcement Handler ------------------ #
async def send_announcement(channel: discord.TextChannel, role_mentions: str):
    doc = docs_service.documents().get(documentId=GOOGLE_DOC_ID).execute()
    content = doc.get("body").get("content")

    message_text = ""
    image_ids = []

    for element in content:
        if "paragraph" in element:
            for el in element["paragraph"].get("elements", []):
                text_run = el.get("textRun")
                if text_run:
                    message_text += text_run.get("content", "")
        elif "inlineObjectElement" in element:
            obj_id = element["inlineObjectElement"]["inlineObjectId"]
            embedded_obj = doc["inlineObjects"][obj_id]
            img_src = embedded_obj["inlineObjectProperties"]["embeddedObject"].get("imageProperties", {}).get("contentUri")
            if img_src:
                image_ids.append(obj_id)

    images = []
    for obj_id in doc.get("inlineObjects", {}):
        if obj_id in image_ids:
            object_props = doc["inlineObjects"][obj_id]["inlineObjectProperties"]["embeddedObject"]
            object_id = object_props.get("objectId")
            if not object_id:
                continue
            results = drive_service.files().list(q=f"name='{object_id}'", fields="files(id)").execute()
            items = results.get("files", [])
            if not items:
                continue
            file_id = items[0]["id"]
            request = drive_service.files().get_media(fileId=file_id)
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                status, done = downloader.next_chunk()
            fh.seek(0)
            images.append(discord.File(fh, filename=f"{file_id}.png"))

    # Format role mentions
    final_roles = []
    if role_mentions:
        parts = role_mentions.strip().split()
        for p in parts:
            if p.startswith("<@&") and p.endswith(">"):
                final_roles.append(p)
            elif p.startswith("@") and p[1:].isdigit():
                final_roles.append(f"<@&{p[1:]}>")

    roles_string = " ".join(final_roles)
    final_message = f"{roles_string}

{message_text}" if roles_string else message_text

    await channel.send(final_message, files=images if images else None)

# ------------------ Commands ------------------ #
@bot.command(name="announce")
async def announce_prefix(ctx, channel: discord.TextChannel, *, roles: str = None):
    await send_announcement(channel, roles)
    await ctx.send("✅ Announcement sent.", ephemeral=True if hasattr(ctx, "response") else False)

@tree.command(name="announce", description="Send an announcement from the Google Doc")
@app_commands.describe(channel="The channel to send the announcement in", roles="Optional role mentions")
async def announce_slash(interaction: discord.Interaction, channel: discord.TextChannel, roles: str = None):
    await send_announcement(channel, roles)
    await interaction.response.send_message("✅ Announcement sent.", ephemeral=True)

# ------------------ Flask Keep Alive ------------------ #
app = Flask("")

@app.route("/")
def home():
    return "Bot is alive!"

def run():
    app.run(host="0.0.0.0", port=8080)

def keep_alive():
    t = threading.Thread(target=run)
    t.start()

# ------------------ Bot Events ------------------ #
@bot.event
async def on_ready():
    await tree.sync()
    print(f"✅ Logged in as {bot.user}")

# ------------------ Run ------------------ #
keep_alive()
bot.run(DISCORD_TOKEN)
