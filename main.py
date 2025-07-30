import discord 
from discord.ext import commands, tasks
from discord import app_commands
import os
import json
import asyncio
import io
from datetime import datetime
from flask import Flask
import threading

import gspread
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from google.oauth2 import service_account
from dotenv import load_dotenv

# ------------------ Load ENV ------------------ #
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GOOGLE_DOC_ID = os.getenv("GOOGLE_DOC_ID")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
creds_json = os.getenv("GOOGLE_CREDS_JSON")

# ------------------ Google Auth ------------------ #
if creds_json is None:
    raise Exception("GOOGLE_CREDS_JSON environment variable not set")

creds_dict = json.loads(creds_json)
creds = service_account.Credentials.from_service_account_info(
    creds_dict,
    scopes=['https://www.googleapis.com/auth/documents',
            'https://www.googleapis.com/auth/drive',
            'https://www.googleapis.com/auth/spreadsheets']
)

drive_service = build('drive', 'v3', credentials=creds)
docs_service = build('docs', 'v1', credentials=creds)
gc = gspread.authorize(creds)
sheet = gc.open_by_key(GOOGLE_SHEET_ID).sheet1

# ------------------ Discord Setup ------------------ #
intents = discord.Intents.default()
intents.messages = True
intents.guilds = True
intents.message_content = True
intents.dm_messages = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents)

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

                # Handle images
                text_parts = []
                files = []
                for part in revert_message.split("\n"):
                    if part.strip().startswith("http") and any(ext in part.lower() for ext in [".jpg", ".jpeg", ".png", ".gif"]):
                        async with bot.http.HTTPClient_session.get(part.strip()) as resp:
                            if resp.status == 200:
                                data = await resp.read()
                                file = discord.File(io.BytesIO(data), filename=part.strip().split("/")[-1])
                                files.append(file)
                    else:
                        text_parts.append(part.strip())

                message_text = "\n".join(text_parts)
                await user.send(content=message_text or None, files=files if files else None)
                sheet.update_cell(i + 2, 9, "done")  # Column I = 'Revert Sent'

            except Exception as e:
                print(f"Failed to send revert to {user_id}: {e}")

# ------------------ On Ready ------------------ #
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"✅ Synced {len(synced)} slash commands")
    except Exception as e:
        print(f"Slash command sync failed: {e}")
    check_reverts.start()

# ------------------ Google Doc Parser (Ordered Images) ------------------ #
async def fetch_doc_content_and_images():
    doc = docs_service.documents().get(documentId=GOOGLE_DOC_ID).execute()
    content = ""
    image_files = []
    ordered_objects = []

    # Map objectId to filename
    inline_objects = doc.get("inlineObjects", {})
    object_id_to_title = {}

    for obj_id, obj in inline_objects.items():
        try:
            embedded_obj = obj["inlineObjectProperties"]["embeddedObject"]
            if "imageProperties" in embedded_obj:
                title = embedded_obj.get("title", f"image_{obj_id}.png")
                object_id_to_title[obj_id] = title
        except Exception as e:
            print(f"Error in inline object {obj_id}: {e}")

    # Track order of appearance
    for element in doc.get("body", {}).get("content", []):
        if "paragraph" in element:
            for elem in element["paragraph"].get("elements", []):
                if "textRun" in elem:
                    content += elem["textRun"]["content"]
                elif "inlineObjectElement" in elem:
                    obj_id = elem["inlineObjectElement"]["inlineObjectId"]
                    if obj_id in object_id_to_title:
                        ordered_objects.append(obj_id)
                        content += f"[image:{obj_id}]"

    # Fetch all images from Drive
    try:
        all_drive_images = drive_service.files().list(
            q="mimeType contains 'image/' and trashed = false",
            fields="files(id, name)"
        ).execute().get("files", [])

        for obj_id in ordered_objects:
            name = object_id_to_title.get(obj_id)
            if not name:
                continue
            matched_file = next((f for f in all_drive_images if f["name"] == name), None)
            if not matched_file:
                print(f"No match found for {name}")
                continue

            file_id = matched_file["id"]
            request = drive_service.files().get_media(fileId=file_id)
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                status, done = downloader.next_chunk()
            fh.seek(0)
            image_files.append(discord.File(fh, filename=matched_file["name"]))
    except Exception as e:
        print(f"Image download failed: {e}")

    return content.strip(), image_files

# ------------------ /announce Slash Command ------------------ #
@bot.tree.command(name="announce", description="Send an announcement from Google Docs")
@app_commands.describe(channel="Choose the channel to send the announcement in")
async def announce(interaction: discord.Interaction, channel: discord.TextChannel):
    await interaction.response.defer(thinking=True)
    try:
        text, image_files = await fetch_doc_content_and_images()
        if not text:
            await interaction.followup.send("⚠ The document is empty.")
            return

        await channel.send(content=text, files=image_files if image_files else None)
        await interaction.followup.send(f"✅ Announcement sent to {channel.mention}")
    except Exception as e:
        print(f"Error in /announce: {e}")
        await interaction.followup.send("❌ Failed to send announcement.")

# ------------------ !announce Prefix Command ------------------ #
@bot.command(name="announce")
async def announce_cmd(ctx):
    try:
        text, image_files = await fetch_doc_content_and_images()
        if not text:
            await ctx.send("⚠ The document is empty.")
            return

        await ctx.send(content=text, files=image_files if image_files else None)
    except Exception as e:
        print(f"Error in !announce: {e}")
        await ctx.send("❌ Failed to send announcement.")

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
        await message.reply("✅ Your complaint has been received. Thank you!")

    await bot.process_commands(message)

# ------------------ Flask Keep-Alive ------------------ #
app = Flask('')

@app.route('/')
def home():
    return "Noosphere Collective Bot is alive!"

def run_flask():
    app.run(host='0.0.0.0', port=8080)

threading.Thread(target=run_flask).start()

# ------------------ Bot Run ------------------ #
bot.run(DISCORD_TOKEN)