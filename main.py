import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
import gspread
from datetime import datetime
import asyncio
from flask import Flask
from threading import Thread
from dotenv import load_dotenv

# Load environment variables
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_CREDS_JSON")
GOOGLE_DOC_ID = os.getenv("GOOGLE_DOC_ID")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")

# Setup Discord bot
intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
client = commands.Bot(command_prefix="!", intents=intents)
tree = app_commands.CommandTree(client)

# Google Sheets setup
gc = gspread.service_account_from_dict(eval(GOOGLE_CREDS_JSON))
sheet = gc.open_by_key(GOOGLE_SHEET_ID).sheet1

# Web server to keep bot alive
app = Flask(__name__)
@app.route("/")
def home():
    return "Bot is running!"
def run():
    app.run(host="0.0.0.0", port=8080)
Thread(target=run).start()

# ====== EVENTS ======
@client.event
async def on_ready():
    print(f"✅ Bot is online as {client.user}")
    try:
        synced = await tree.sync()
        print(f"✅ Synced {len(synced)} command(s).")
    except Exception as e:
        print(f"❌ Sync failed: {e}")
    check_reverts.start()

@client.event
async def on_message(message):
    if message.author == client.user or not isinstance(message.channel, discord.DMChannel):
        return

    user_id = str(message.author.id)
    complaint = message.content
    if message.attachments:
        for attachment in message.attachments:
            complaint += f"\n[Attached file]({attachment.url})"

    date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sheet.append_row([user_id, complaint, date, "", "", "", "", "", ""])
    await message.channel.send("✅ Your complaint has been received. Thank you!")

# ====== SLASH COMMAND ======
@tree.command(name="announce", description="Send announcement from Google Doc")
@app_commands.describe(channel="Select the channel to send announcement to", roles="Mentioned role(s)")
async def announce(interaction: discord.Interaction, channel: discord.TextChannel, roles: str):
    await interaction.response.defer()
    try:
        from googleapiclient.discovery import build
        from google.oauth2.service_account import Credentials
        import base64

        scopes = ['https://www.googleapis.com/auth/documents.readonly']
        creds = Credentials.from_service_account_info(eval(GOOGLE_CREDS_JSON), scopes=scopes)
        service = build('docs', 'v1', credentials=creds)
        document = service.documents().get(documentId=GOOGLE_DOC_ID).execute()

        content = ""
        images = []
        for element in document.get("body", {}).get("content", []):
            if "paragraph" in element:
                for el in element["paragraph"].get("elements", []):
                    if "textRun" in el:
                        content += el["textRun"]["content"]
            elif "inlineObjectElement" in element.get("paragraph", {}).get("elements", [{}])[0]:
                object_id = element["paragraph"]["elements"][0]["inlineObjectElement"]["inlineObjectId"]
                embedded_object = document["inlineObjects"][object_id]["inlineObjectProperties"]["embeddedObject"]
                if "imageProperties" in embedded_object:
                    image_source = embedded_object["imageProperties"]["contentUri"]
                    images.append(image_source)

        # Format roles for mention
        role_mentions = " ".join([f"||<@&{r.strip()}>" for r in roles.split(",")])
        if images:
            files = []
            for i, url in enumerate(images):
                import requests
                img_data = requests.get(url).content
                file = discord.File(fp=bytes(img_data), filename=f"image{i+1}.png")
                files.append(file)
            await channel.send(content=role_mentions + "\n" + content.strip(), files=files)
        else:
            embed = discord.Embed(description=content.strip(), color=discord.Color.blue())
            await channel.send(content=role_mentions, embed=embed)

        await interaction.followup.send("✅ Announcement sent!")
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")
        print(f"[ERROR] announce: {e}")

# ====== BACKGROUND TASK FOR REVERTS ======
@tasks.loop(seconds=300)  # every 5 minutes
async def check_reverts():
    rows = sheet.get_all_values()
    headers = rows[0]
    for i, row in enumerate(rows[1:], start=2):  # skip header
        if len(row) >= 9 and row[7] and (len(row) < 10 or row[8].lower() != "done"):
            user_id = row[0]
            message = row[7]
            try:
                user = await client.fetch_user(int(user_id))
                await user.send(f"📬 Revert: {message}")
                sheet.update_cell(i, 9, "done")  # column I (index 9) = 'Revert Sent'
                print(f"✅ Revert sent to {user_id}")
            except Exception as e:
                print(f"❌ Failed to send revert to {user_id}: {e}")

# ====== START BOT ======
client.run(DISCORD_TOKEN)
