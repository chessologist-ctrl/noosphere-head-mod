import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
import os
import base64
import json
import io
from datetime import datetime
import gspread
from flask import Flask
from threading import Thread
from oauth2client.service_account import ServiceAccountCredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from google.oauth2 import service_account
from dotenv import load_dotenv

# ------------------ Flask Keep-Alive ------------------ #
app = Flask('')

@app.route('/')
def home():
    return "Bot is alive!"

def run_flask():
    app.run(host='0.0.0.0', port=8080)

Thread(target=run_flask).start()

# ------------------ Load ENV ------------------ #
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GOOGLE_DOC_ID = os.getenv("GOOGLE_DOC_ID")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")

# --- Get creds from environment variable --- #
creds_json = os.getenv("GOOGLE_CREDS_JSON")
if creds_json is None:
    raise Exception("GOOGLE_CREDS_JSON environment variable not set")

creds_dict = json.loads(creds_json)
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, [
    'https://spreadsheets.google.com/feeds',
    'https://www.googleapis.com/auth/drive',
    'https://www.googleapis.com/auth/documents'
])
gc = gspread.authorize(creds)
sheet = gc.open_by_key(GOOGLE_SHEET_ID).sheet1

# ------------------ Discord Bot Setup ------------------ #
intents = discord.Intents.default()
intents.messages = True
intents.guilds = True
intents.message_content = True
intents.dm_messages = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = app_commands.CommandTree(bot)

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

        sheet.append_row([user_id, complaint, date, "", "", "", "", "", ""])  # Added extra column for Revert Sent

        await message.reply("✅ Your complaint has been received. Thank you!")

    await bot.process_commands(message)

# ------------------ Revert Checker ------------------ #
@tasks.loop(seconds=300)  # Every 5 minutes
async def check_reverts():
    records = sheet.get_all_records()
    for i, row in enumerate(records):
        revert_message = row.get("Revert")
        revert_sent = row.get("Revert Sent")
        user_id = row.get("User Id")

        if revert_message and revert_sent != "done":
            try:
                user = await bot.fetch_user(int(user_id))

                # Parse attachments
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

# ------------------ Announce Slash Command ------------------ #
@tree.command(name="announce", description="Send formatted announcement from Google Doc")
@app_commands.describe(channel="Channel to post in", roles="Mentioned roles (with @)")
async def announce(interaction: discord.Interaction, channel: discord.TextChannel, roles: str):
    await interaction.response.defer()

    try:
        service = build('docs', 'v1', credentials=creds)
        doc = service.documents().get(documentId=GOOGLE_DOC_ID).execute()

        text = ""
        images = []
        for element in doc['body']['content']:
            if 'paragraph' in element:
                for el in element['paragraph'].get('elements', []):
                    text += el.get('textRun', {}).get('content', '')
            elif 'inlineObjectElement' in element:
                obj_id = element['inlineObjectElement']['inlineObjectId']
                obj = doc['inlineObjects'][obj_id]
                img = obj['inlineObjectProperties']['embeddedObject']
                if 'imageProperties' in img:
                    images.append(img)

        mention_text = " ".join([f"||{role}||" for role in roles.split()])
        if images:
            folder_service = build('drive', 'v3', credentials=creds)
            media_files = []

            for i, img in enumerate(images):
                object_id = list(doc['inlineObjects'].keys())[i]
                object_metadata = doc['inlineObjects'][object_id]
                source_uri = object_metadata['inlineObjectProperties']['embeddedObject']['imageProperties']['contentUri']
                request = folder_service.files().get_media(fileId=source_uri)
                fh = io.BytesIO()
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while not done:
                    status, done = downloader.next_chunk()
                fh.seek(0)
                media_files.append(discord.File(fh, filename=f'image_{i + 1}.png'))

            await channel.send(content=mention_text + "\n" + text.strip(), files=media_files)
        else:
            embed = discord.Embed(description=text.strip(), color=0x2ecc71)
            await channel.send(content=mention_text, embed=embed)

        await interaction.followup.send("✅ Announcement sent!")
    except Exception as e:
        await interaction.followup.send(f"❌ Failed to send announcement: {e}")

# ------------------ Prefix (!announce) version ------------------ #
@bot.command(name="announce")
async def prefix_announce(ctx, channel: discord.TextChannel, *, roles: str):
    fake_interaction = type("FakeInteraction", (), {"response": ctx, "channel": ctx.channel, "followup": ctx, "user": ctx.author})
    await announce(fake_interaction, channel, roles)

# ------------------ Bot Ready ------------------ #
@bot.event
async def on_ready():
    print(f"✅ Bot is online as {bot.user}")
    await tree.sync()
    check_reverts.start()
