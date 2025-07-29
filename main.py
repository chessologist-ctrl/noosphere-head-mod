import discord
from discord.ext import commands, tasks
from discord import app_commands
from flask import Flask
import asyncio
import os
import json
import io
from datetime import datetime
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from dotenv import load_dotenv
import aiohttp

# ------------------ Load ENV ------------------ #
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GOOGLE_DOC_ID = os.getenv("GOOGLE_DOC_ID")
GOOGLE_SHEET_ID = os.getenv("GOOGLE_SHEET_ID")

# --- Get creds from environment variable --- #
creds_json = os.getenv("GOOGLE_CREDS_JSON")
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

        if revert_message and revert_sent.strip().lower() != "done":
            try:
                user = await bot.fetch_user(int(user_id))

                # Parse attachments
                text_parts = []
                files = []
                for part in revert_message.split("\n"):
                    if part.strip().startswith("http") and any(ext in part.lower() for ext in [".jpg", ".png", ".jpeg", ".gif"]):
                        try:
                            async with aiohttp.ClientSession() as session:
                                async with session.get(part.strip()) as resp:
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
                print(f"❌ Failed to send revert to {user_id}: {e}")

# ------------------ Announcement Slash & Prefix Command ------------------ #
@bot.command(name="announce")
async def announce_prefix(ctx):
    await ctx.send("📢 Announcement command will soon support full doc embedding here too!")

@bot.tree.command(name="announce", description="Send an announcement")
async def announce_slash(interaction: discord.Interaction):
    await interaction.response.send_message("📢 Slash command for announcement is running!", ephemeral=True)

# ------------------ Ready Event ------------------ #
@bot.event
async def on_ready():
    await bot.tree.sync()
    check_reverts.start()
    print(f"✅ Logged in as {bot.user}")

# ------------------ Flask Keep-Alive ------------------ #
app = Flask('')

@app.route('/')
def home():
    return "Bot is alive!"

def run_flask():
    app.run(host='0.0.0.0', port=8080)

# ------------------ Start Bot & Flask ------------------ #
if __name__ == "__main__":
    import threading
    threading.Thread(target=run_flask).start()
    bot.run(DISCORD_TOKEN)
