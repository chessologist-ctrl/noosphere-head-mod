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
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_CREDS_JSON")
GOOGLE_DOC_ID = os.getenv("GOOGLE_DOC_ID")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")

intents = discord.Intents.default()
intents.messages = True
intents.guilds = True
intents.members = True
intents.message_content = True
client = commands.Bot(command_prefix='!', intents=intents)
tree = client.tree

# ------------------ Google Setup ------------------ #
creds_dict = json.loads(GOOGLE_CREDS_JSON)
creds = service_account.Credentials.from_service_account_info(creds_dict)
drive_service = build('drive', 'v3', credentials=creds)
docs_service = build('docs', 'v1', credentials=creds)
sheet_client = gspread.authorize(creds)
spreadsheet = sheet_client.open_by_key(GOOGLE_SHEET_ID)
worksheet = spreadsheet.sheet1

# ------------------ Flask Keep Alive ------------------ #
app = Flask('')

@app.route('/')
def home():
    return "Bot is running!"

def run_flask():
    app.run(host='0.0.0.0', port=8080)

threading.Thread(target=run_flask).start()

# ------------------ Fetch Doc Content & Images ------------------ #
async def fetch_doc_content_and_images():
    doc = docs_service.documents().get(documentId=GOOGLE_DOC_ID).execute()
    body = doc.get("body").get("content")
    text_content = ""
    image_ids = []
    ordered_images = []

    for element in body:
        if "paragraph" in element:
            for el in element["paragraph"]["elements"]:
                if "textRun" in el:
                    text = el["textRun"].get("content", "")
                    text_content += text
                elif "inlineObjectElement" in el:
                    inline_id = el["inlineObjectElement"]["inlineObjectId"]
                    image_ids.append(inline_id)
                    ordered_images.append(inline_id)
                    text_content += "\n"
        elif "table" in element:
            for row in element["table"]["tableRows"]:
                for cell in row["tableCells"]:
                    for cell_content in cell["content"]:
                        for el in cell_content.get("paragraph", {}).get("elements", []):
                            if "textRun" in el:
                                text = el["textRun"].get("content", "")
                                text_content += text
                            elif "inlineObjectElement" in el:
                                inline_id = el["inlineObjectElement"]["inlineObjectId"]
                                image_ids.append(inline_id)
                                ordered_images.append(inline_id)
                                text_content += "\n"

    images = []
    for inline_id in ordered_images:
        obj = doc["inlineObjects"][inline_id]
        embed = obj["inlineObjectProperties"]["embeddedObject"]
        img_source = embed.get("imageProperties", {}).get("contentUri")
        if img_source:
            img_id = img_source.split("=d/")[-1].split("/")[0] if "=d/" in img_source else ""
            if not img_id:
                continue
            request = drive_service.files().get_media(fileId=img_id)
            fh = io.BytesIO()
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                status, done = downloader.next_chunk()
            fh.seek(0)
            images.append(discord.File(fp=fh, filename=f"image_{inline_id}.png"))
    return text_content.strip(), images

# ------------------ Handle Announcements ------------------ #
async def handle_announcement(channel: discord.TextChannel, roles_text: str = None):
    content, images = await fetch_doc_content_and_images()

    # Role mention replacements from raw ID text in document
    for word in content.split():
        if word.startswith("@") and word[1:].isdigit():
            content = content.replace(word, f"<@&{word[1:]}>")

    # Format role pings if roles_text is provided
    ping_line = ""
    if roles_text:
        mentions = []
        for role_str in roles_text.split():
            role_id = role_str.strip("<@&>")
            if role_id.isdigit():
                mentions.append(f"<@&{role_id}>")
        if mentions:
            ping_line = " ".join(mentions) + "\n\n"

    # Send with images
    if images:
        await channel.send(ping_line + content, files=images)
    else:
        embed = discord.Embed(description=ping_line + content, color=0x2b2d31)
        await channel.send(embed=embed)

# ------------------ Slash Command ------------------ #
@tree.command(name="announce", description="Post announcement from Google Doc")
@app_commands.describe(channel="Channel to post in", roles="Optional space-separated role IDs to mention")
async def slash_announce(interaction: discord.Interaction, channel: discord.TextChannel, roles: str = None):
    await interaction.response.send_message("📢 Posting announcement...", ephemeral=True)
    await handle_announcement(channel, roles)

# ------------------ Prefix Command ------------------ #
@client.command(name="announce")
async def prefix_announce(ctx, channel: discord.TextChannel, *roles):
    roles_text = " ".join(roles)
    await handle_announcement(channel, roles_text)

# ------------------ Log Complaints from DM ------------------ #
@client.event
async def on_message(message):
    if message.guild is None and not message.author.bot:
        content = message.content
        attachments = [att.url for att in message.attachments]
        full_complaint = content + "\n\nAttachments:\n" + "\n".join(attachments) if attachments else content
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        worksheet.append_row([str(message.author.id), full_complaint, now, "", "", "", ""])
        await message.channel.send("✅ Your complaint has been received. Thank You!")
    await client.process_commands(message)

# ------------------ Revert Checker ------------------ #
@tasks.loop(minutes=5)
async def check_reverts():
    data = worksheet.get_all_records()
    for i, row in enumerate(data):
        if row.get("Revert") and row.get("Revert Sent") != "done":
            user_id = row["User Id"]
            try:
                user = await client.fetch_user(int(user_id))
                await user.send(row["Revert"])
                worksheet.update_cell(i + 2, list(row.keys()).index("Revert Sent") + 1, "done")
            except:
                pass

@client.event
async def on_ready():
    await tree.sync()
    print(f"✅ Bot is ready. Logged in as {client.user}")
    check_reverts.start()

client.run(DISCORD_TOKEN)
