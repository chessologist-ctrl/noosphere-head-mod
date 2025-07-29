import discord
from discord.ext import commands, tasks
from discord import File, app_commands
import os
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from dotenv import load_dotenv
import datetime
import aiohttp
import asyncio
import io

# Load environment variables
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GOOGLE_CREDS_PATH = os.getenv("GOOGLE_CREDS_PATH")
GOOGLE_DOC_ID = os.getenv("GOOGLE_DOC_ID")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")

# Google Sheets Setup
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDS_PATH, scope)
gc = gspread.authorize(creds)
sheet = gc.open_by_key(GOOGLE_SHEET_ID).sheet1

intents = discord.Intents.default()
intents.messages = True
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

@bot.event
async def on_ready():
    print(f"\n✅ Logged in as {bot.user}")
    try:
        synced = await tree.sync()
        print(f"🌐 Synced {len(synced)} slash commands.")
    except Exception as e:
        print(f"Error syncing commands: {e}")
    check_reverts.start()
    print("🔁 Revert checker running every 1 minute.")

# Complaint Intake
@bot.event
async def on_message(message):
    if message.guild is None and not message.author.bot:
        user_id = str(message.author.id)
        complaint_text = message.content
        date = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        attachments_text = ""
        for attachment in message.attachments:
            attachments_text += f"\n{attachment.url}"

        full_complaint = complaint_text + attachments_text
        sheet.append_row([user_id, full_complaint, date, "", "", "", "", "", ""])
        await message.channel.send("📝 Your complaint has been recorded. Thank you!")

# Slash Command: /announce
@tree.command(name="announce", description="Send an announcement from the Google Doc")
@app_commands.describe(channel="Channel to send the announcement in", roles="Mention roles (optional, multiple allowed)")
async def announce(interaction: discord.Interaction, channel: discord.TextChannel, roles: str = ""):
    await interaction.response.defer()
    try:
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaIoBaseDownload
        from google.oauth2 import service_account

        creds = service_account.Credentials.from_service_account_file(
            GOOGLE_CREDS_PATH,
            scopes=['https://www.googleapis.com/auth/drive.readonly', 'https://www.googleapis.com/auth/documents.readonly']
        )
        docs_service = build('docs', 'v1', credentials=creds)
        doc = docs_service.documents().get(documentId=GOOGLE_DOC_ID).execute()
        content = doc.get("body", {}).get("content", [])

        text_chunks = []
        image_urls = []

        inline_objects = doc.get("inlineObjects", {})
        for element in content:
            if "paragraph" in element:
                for run in element["paragraph"].get("elements", []):
                    text = run.get("textRun", {}).get("content", "")
                    if text:
                        text_chunks.append(text)
                    if "inlineObjectElement" in run:
                        obj_id = run["inlineObjectElement"]["inlineObjectId"]
                        if obj_id in inline_objects:
                            embedded = inline_objects[obj_id]["inlineObjectProperties"]["embeddedObject"]
                            if "imageProperties" in embedded and "contentUri" in embedded["imageProperties"]:
                                image_urls.append(embedded["imageProperties"]["contentUri"])

        full_text = "".join(text_chunks).strip()

        role_mentions = ""
        if roles:
            role_ids = [r.strip().strip("<@&>") for r in roles.split()]
            role_mentions = " ".join([f"||<@&{r}>||" for r in role_ids if r.isdigit()])

        if image_urls:
            files = []
            for idx, img_url in enumerate(image_urls):
                async with aiohttp.ClientSession() as session:
                    async with session.get(img_url) as resp:
                        if resp.status == 200:
                            data = await resp.read()
                            file = File(io.BytesIO(data), filename=f"img{idx}.png")
                            files.append(file)
            await channel.send(content=f"{role_mentions}\n{full_text}", files=files)
        else:
            await channel.send(content=f"{role_mentions}\n{full_text}")

        await interaction.followup.send("✅ Announcement sent.")

    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}")

# Revert Background Task
@tasks.loop(minutes=1)
async def check_reverts():
    try:
        data = sheet.get_all_records()
        for i, row in enumerate(data):
            user_id = str(row.get("User Id", "")).strip()
            revert_message = str(row.get("Revert", "")).strip()
            revert_sent = str(row.get("Revert Sent", "")).strip().lower()

            if revert_message and revert_sent != "done":
                try:
                    user = await bot.fetch_user(int(user_id))
                    lines = revert_message.split("\n")
                    text_lines = [line for line in lines if not line.strip().startswith("http")]
                    attachment_urls = [line.strip() for line in lines if line.strip().startswith("http")]

                    msg = None
                    if text_lines:
                        msg = await user.send("\n".join(text_lines))
                    else:
                        msg = await user.send("")

                    for url in attachment_urls:
                        async with aiohttp.ClientSession() as session:
                            async with session.get(url) as resp:
                                if resp.status == 200:
                                    img_data = await resp.read()
                                    file = File(io.BytesIO(img_data), filename=url.split("/")[-1])
                                    if msg:
                                        await msg.reply(file=file)
                                    else:
                                        await user.send(file=file)

                    # ✅ Update column I = Revert Sent
                    sheet.update_cell(i + 2, 9, "done")

                except Exception as e:
                    print(f"❌ Error DMing user {user_id}: {e}")

    except Exception as e:
        print(f"❌ Error in check_reverts loop: {e}")

bot.run(DISCORD_TOKEN)
