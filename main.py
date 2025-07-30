import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
import io
import base64
from flask import Flask
from threading import Thread
import gspread
from datetime import datetime
import asyncio
import json
from dotenv import load_dotenv
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials
import requests

# ------------------ ENV ------------------ #
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GOOGLE_DOC_ID = os.getenv("GOOGLE_DOC_ID")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
GOOGLE_CREDS_JSON = json.loads(os.getenv("GOOGLE_CREDS_JSON"))

intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree  # Slash commands

# ------------------ GOOGLE SERVICES ------------------ #
scope = ['https://www.googleapis.com/auth/documents.readonly', 'https://www.googleapis.com/auth/spreadsheets']
creds = Credentials.from_service_account_info(GOOGLE_CREDS_JSON, scopes=scope)
docs_service = build('docs', 'v1', credentials=creds)
sheets_client = gspread.authorize(creds)
sheet = sheets_client.open_by_key(GOOGLE_SHEET_ID).sheet1

# ------------------ FLASK KEEP ALIVE ------------------ #
app = Flask('')

@app.route('/')
def home():
    return "Noosphere Bot is Live!"

def run():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    Thread(target=run).start()

# ------------------ GET DOC CONTENT & IMAGES ------------------ #
async def fetch_doc_content_and_images():
    doc = docs_service.documents().get(documentId=GOOGLE_DOC_ID).execute()
    content = ""
    images = []

    def read_structural_elements(elements):
        nonlocal content, images
        for value in elements:
            if 'paragraph' in value:
                elements = value['paragraph']['elements']
                for elem in elements:
                    text_run = elem.get('textRun')
                    if text_run:
                        content += text_run['content']
            if 'inlineObjectElement' in value:
                obj_id = value['inlineObjectElement']['inlineObjectId']
                obj = doc['inlineObjects'][obj_id]
                img = obj['inlineObjectProperties']['embeddedObject']
                if 'imageProperties' in img:
                    img_source = img['imageProperties']['contentUri']
                    images.append(img_source)

    body = doc.get('body').get('content')
    read_structural_elements(body)

    return content.strip(), images

# ------------------ COMPLAINT SYSTEM ------------------ #
@bot.event
async def on_message(message):
    if message.guild is None and not message.author.bot:
        user_id = str(message.author.id)
        text = message.content
        date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        attachments_text = ""
        for attachment in message.attachments:
            attachments_text += f"{attachment.url}\n"

        complaint = f"{text}\n{attachments_text}".strip()
        sheet.append_row([user_id, complaint, date, "", "", "", "", ""])
        await message.channel.send("✅ Your complaint has been received.")

    await bot.process_commands(message)

# ------------------ REVERT CHECK LOOP ------------------ #
@tasks.loop(minutes=5)
async def check_reverts():
    records = sheet.get_all_records()
    for i, row in enumerate(records, start=2):  # skip header
        if row.get("Revert") and row.get("Revert Sent") != "done":
            user_id = row["User Id"]
            message = row["Revert"]

            try:
                user = await bot.fetch_user(int(user_id))
                # Handle attachments if present in Revert
                urls = [x.strip() for x in message.split() if x.startswith("http")]
                text = "\n".join([x for x in message.split("\n") if not x.strip().startswith("http")])

                files = []
                for url in urls:
                    response = requests.get(url)
                    if response.status_code == 200:
                        file_bytes = io.BytesIO(response.content)
                        filename = url.split("/")[-1]
                        files.append(discord.File(file_bytes, filename=filename))

                await user.send(content=text, files=files)
                sheet.update_cell(i, 9, "done")  # Revert Sent column (I)
            except Exception as e:
                print(f"Error sending revert to {user_id}: {e}")

# ------------------ ANNOUNCE COMMAND ------------------ #
@tree.command(name="announce", description="Send announcement from Google Doc")
@app_commands.describe(channel="Channel to send the announcement in")
async def announce(interaction: discord.Interaction, channel: discord.TextChannel):
    await interaction.response.send_message("📡 Sending announcement...", ephemeral=True)

    text, image_urls = await fetch_doc_content_and_images()

    # Mention formatting: Replace role names with actual mentions if they exist
    for role in interaction.guild.roles:
        if f"@{role.name}" in text:
            text = text.replace(f"@{role.name}", role.mention)

    files = []
    for url in image_urls:
        try:
            response = requests.get(url)
            if response.status_code == 200:
                image_bytes = io.BytesIO(response.content)
                filename = url.split("/")[-1].split("?")[0]
                files.append(discord.File(image_bytes, filename=filename))
        except Exception as e:
            print(f"Failed to fetch image: {e}")

    await channel.send(content=text, files=files)

# ------------------ !announce PREFIX ------------------ #
@bot.command(name="announce")
async def announce_prefix(ctx):
    await ctx.send("⚡ Use the /announce slash command to select the channel!")

# ------------------ EVENTS ------------------ #
@bot.event
async def on_ready():
    await tree.sync()
    print(f"✅ Bot is online as {bot.user}")
    check_reverts.start()

# ------------------ STARTUP ------------------ #
keep_alive()
bot.run(DISCORD_TOKEN)