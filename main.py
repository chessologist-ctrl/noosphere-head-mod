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
from collections import defaultdict

import gspread
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
from google.oauth2 import service_account
from dotenv import load_dotenv

# ------------------ Load ENV ------------------
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GOOGLE_DOC_ID = os.getenv("GOOGLE_DOC_ID")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
creds_json = os.getenv("GOOGLE_CREDS_JSON")

# ------------------ Rate Limit Setup ------------------
RATE_LIMIT = 10  # Adjusted to 10 uses per minute
RATE_LIMIT_WINDOW = 60  # 1 minute in seconds
usage_tracker = defaultdict(list)

# ------------------ Google Auth ------------------
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

# ------------------ Discord Setup ------------------
intents = discord.Intents.default()
intents.messages = True
intents.guilds = True
intents.message_content = True
intents.dm_messages = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents)

# ------------------ Revert Checker ------------------
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

# ------------------ On Ready ------------------
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"✅ Synced {len(synced)} slash commands")
    except Exception as e:
        print(f"Slash command sync failed: {e}")
    check_reverts.start()

# ------------------ Google Doc Parser with Role and Image Handling ------------------
async def fetch_doc_content_and_images(interaction=None, channel=None):
    doc = docs_service.documents().get(documentId=GOOGLE_DOC_ID).execute()
    content = ""
    image_files = []

    # Map inlineObjects (images) to file URLs
    inline_objects = doc.get("inlineObjects", {})
    object_images = {}
    for obj_id, obj in inline_objects.items():
        try:
            source_uri = obj["inlineObjectProperties"]["embeddedObject"]["imageProperties"]["contentUri"]
            object_images[obj_id] = source_uri
            print(f"Found image with contentUri: {source_uri}")  # Debug log
        except KeyError:
            continue

    # Parse the document body
    for element in doc.get("body", {}).get("content", []):
        if "paragraph" in element:
            for elem in element["paragraph"].get("elements", []):
                if "textRun" in elem:
                    text = elem["textRun"]["content"]
                    # Detect and convert role mentions from text
                    if interaction and interaction.guild:
                        guild = interaction.guild
                    elif channel and channel.guild:
                        guild = channel.guild
                    else:
                        guild = None
                    if guild:
                        for role in guild.roles:
                            if f"@{role.name}" in text:
                                text = text.replace(f"@{role.name}", role.mention)
                            if f"@{role.id}" in text:
                                text = text.replace(f"@{role.id}", role.mention)
                    content += text
                elif "inlineObjectElement" in elem:
                    obj_id = elem["inlineObjectElement"]["inlineObjectId"]
                    if obj_id in object_images:
                        content += f"[image:{obj_id}]"  # Temporary placeholder

    # Replace placeholders with Discord images
    for obj_id, url in object_images.items():
        try:
            async with bot.http.HTTPClient_session.get(url) as resp:
                print(f"Fetching {url}, Status: {resp.status}, Headers: {resp.headers}, Content-Type: {resp.headers.get('content-type')}")  # Enhanced debug log
                if resp.status == 200 and resp.headers.get('content-type', '').startswith('image'):
                    data = await resp.read()
                    filename = f"image_{obj_id}.png"  # Default filename
                    image_files.append(discord.File(io.BytesIO(data), filename=filename))
                else:
                    print(f"Failed to fetch {url}, Invalid status or content type")
        except Exception as e:
            print(f"Image download failed for {url}: {e}")

    # Replace placeholders with empty string (images are handled separately)
    for obj_id in object_images.keys():
        content = content.replace(f"[image:{obj_id}]", "")

    return content.strip(), image_files

# ------------------ Permission Check ------------------
ALLOWED_ROLE_IDS = {1397015557185867799, 123456789012345678}  # Replace with your comma-separated role IDs
def is_allowed(interaction_or_ctx):
    if isinstance(interaction_or_ctx, discord.Interaction):
        user = interaction_or_ctx.user
        member = interaction_or_ctx.guild.get_member(user.id)
        if member and member.guild_permissions.administrator:
            return True
        return any(role.id in ALLOWED_ROLE_IDS for role in member.roles) if member else False
    elif isinstance(interaction_or_ctx, commands.Context):
        member = interaction_or_ctx.author
        if member.guild_permissions.administrator:
            return True
        return any(role.id in ALLOWED_ROLE_IDS for role in member.roles)

# ------------------ Rate Limit Check ------------------
def check_rate_limit(user_id):
    current_time = datetime.now().timestamp()
    user_usage = usage_tracker[user_id]
    user_usage = [t for t in user_usage if current_time - t < RATE_LIMIT_WINDOW]
    usage_tracker[user_id] = user_usage
    return len(user_usage) < RATE_LIMIT

# ------------------ /announce Slash Command ------------------
@bot.tree.command(name="announce", description="Send an announcement from Google Docs")
@app_commands.describe(channel="Choose the channel to send the announcement in", roles="Optional role mentions (e.g., @role or @role_id)")
async def announce(interaction: discord.Interaction, channel: discord.TextChannel, roles: str = None):
    if not is_allowed(interaction):
        await interaction.response.send_message("❌ You do not have permission to use this command.", ephemeral=True)
        return
    user_id = str(interaction.user.id)
    if not check_rate_limit(user_id):
        await interaction.response.send_message("❌ Rate limit exceeded. Try again in 1 minute.", ephemeral=True)
        return
    usage_tracker[user_id].append(datetime.now().timestamp())
    await interaction.response.defer(thinking=True)
    try:
        text, image_files = await fetch_doc_content_and_images(interaction, channel)
        if not text and not image_files:
            await interaction.followup.send("⚠ The document is empty.", ephemeral=True)
            return

        # Parse roles from argument
        final_roles = []
        if roles:
            for part in roles.strip().split():
                if part.startswith("@") and part[1:].isdigit():
                    role_id = int(part[1:])
                    role = discord.utils.get(channel.guild.roles, id=role_id)
                    if role:
                        final_roles.append(role.mention)
                elif part.startswith("@"):
                    role_name = part[1:]
                    role = discord.utils.get(channel.guild.roles, name=role_name)
                    if role:
                        final_roles.append(role.mention)

        roles_string = " ".join(final_roles) if final_roles else ""
        final_message = f"{roles_string}\n{text}" if roles_string else text

        # Send text first, then images separately
        await channel.send(content=final_message or None)
        if image_files:
            await channel.send(files=image_files)

        await interaction.followup.send(f"✅ Announcement sent to {channel.mention}", ephemeral=True)
    except Exception as e:
        print(f"Error in /announce: {e}")
        await interaction.followup.send("❌ Failed to send announcement.", ephemeral=True)

# ------------------ !announce Prefix Command ------------------
@bot.command(name="announce")
async def announce_cmd(ctx, channel: discord.TextChannel, *, roles: str = None):
    if not is_allowed(ctx):
        await ctx.send("❌ You do not have permission to use this command.")
        return
    user_id = str(ctx.author.id)
    if not check_rate_limit(user_id):
        await ctx.send("❌ Rate limit exceeded. Try again in 1 minute.")
        return
    usage_tracker[user_id].append(datetime.now().timestamp())
    try:
        text, image_files = await fetch_doc_content_and_images(None, channel)
        if not text and not image_files:
            await ctx.send("⚠ The document is empty.")
            return

        # Parse roles from argument
        final_roles = []
        if roles:
            for part in roles.strip().split():
                if part.startswith("@") and part[1:].isdigit():
                    role_id = int(part[1:])
                    role = discord.utils.get(channel.guild.roles, id=role_id)
                    if role:
                        final_roles.append(role.mention)
                elif part.startswith("@"):
                    role_name = part[1:]
                    role = discord.utils.get(channel.guild.roles, name=role_name)
                    if role:
                        final_roles.append(role.mention)

        roles_string = " ".join(final_roles) if final_roles else ""
        final_message = f"{roles_string}\n{text}" if roles_string else text

        # Send text first, then images separately
        await channel.send(content=final_message or None)
        if image_files:
            await channel.send(files=image_files)
    except Exception as e:
        print(f"Error in !announce: {e}")
        await ctx.send("❌ Failed to send announcement.")

# ------------------ DM Complaint Logger ------------------
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

# ------------------ Flask Keep-Alive ------------------
app = Flask('')

@app.route('/')
def home():
    return "Noosphere Collective Bot is alive!"

def run_flask():
    app.run(host='0.0.0.0', port=8080)

threading.Thread(target=run_flask).start()

# ------------------ Bot Run ------------------
bot.run(DISCORD_TOKEN)