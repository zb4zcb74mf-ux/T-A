import asyncio
import logging
import os
import shutil
from typing import Any
import discord
import yt_dlp
from discord.ext import commands

# -------------------------------------------------------------
# تم تعديل الكود ليكون آمناً: يسحب التوكن من إعدادات الاستضافة تلقائياً
TOKEN = os.getenv("MTM5MDE0MDYwODkxNTU3NDgxNQ.GW5cTc.tR2qM7jgoKovvt1LIoNYv3UK7FFUuHfEQr4xdk")
# -------------------------------------------------------------

PREFIX = "!"

def get_command_prefix(_bot: commands.Bot, message: discord.Message) -> str:
    content = message.content.strip()
    first_word = content.split(maxsplit=1)
    if first_word and first_word[0] == "ش":
        return ""
    if content in {"وقف", "ك", "س"}:
        return ""
    return PREFIX

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("discord_music_bot")

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
bot = commands.Bot(command_prefix=get_command_prefix, intents=intents)

YTDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "default_search": "ytsearch1",
    "source_address": "0.0.0.0",
}
FFMPEG_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn -af volume=0.5",
}
ytdl = yt_dlp.YoutubeDL(YTDL_OPTIONS)
ytdl_lock = asyncio.Lock()

queues: dict[int, list[tuple[dict[str, str], discord.abc.Messageable]]] = {}
now_playing: dict[int, dict[str, str]] = {}
voice_locks: dict[int, asyncio.Lock] = {}
voice_connection_locks: dict[int, asyncio.Lock] = {}
voice_targets: dict[int, int] = {}
voice_reconnect_tasks: dict[int, asyncio.Task[None]] = {}

async def extract_info(url: str) -> dict[str, Any] | None:
    async with ytdl_lock:
        return await asyncio.to_thread(ytdl.extract_info, url, download=False)

async def find_track(search: str) -> dict[str, str]:
    data = await extract_info(search)
    if not data:
        raise ValueError("لم أجد نتيجة.")
    if data.get("entries") is not None:
        data = next((entry for entry in data["entries"] if entry), None)
    if not data:
        raise ValueError("ما لقيت الأغنية. جرّب اسمًا ثانيًا.")
    webpage = data.get("webpage_url")
    if not webpage:
        raise ValueError("نتيجة البحث لا تحتوي على رابط صالح.")
    return {"title": data.get("title") or "أغنية بدون اسم", "webpage": webpage}

async def send_safely(channel: discord.abc.Messageable, message: str) -> None:
    try:
        await channel.send(message)
    except discord.DiscordException:
        logger.exception("Could not send a message")

async def play_next(guild: discord.Guild) -> None:
    lock = voice_locks.setdefault(guild.id, asyncio.Lock())
    async with lock:
        voice_client = guild.voice_client
        if not voice_client or not voice_client.is_connected():
            return
        queue = queues.setdefault(guild.id, [])
        if not queue:
            if not voice_client.is_playing() and not voice_client.is_paused():
                now_playing.pop(guild.id, None)
            return
        while queue:
            track, requester_channel = queue.pop(0)
            try:
                stream = await extract_info(track["webpage"])
                stream_url = stream.get("url") if stream else None
                if not stream_url:
                    raise ValueError("لم أتمكن من الحصول على رابط البث.")
                source = discord.FFmpegOpusAudio(stream_url, **FFMPEG_OPTIONS)
                now_playing[guild.id] = track

                def after_play(error: Exception | None) -> None:
                    if error:
                        logger.error("Playback error: %s", error)
                    future = asyncio.run_coroutine_threadsafe(advance_after_track(guild, track), bot.loop)
                    future.add_done_callback(lambda d: d.result() if not d.cancelled() else None)

                voice_client.play(source, after=after_play)
                await send_safely(requester_channel, f"🎵 الآن تعمل: **{track['title']}**")
                return
            except Exception:
                logger.exception("Could not play track")
                if now_playing.get(guild.id) is track:
                    now_playing.pop(guild.id, None)
                await send_safely(requester_channel, f"تعذر تشغيل **{track['title']}**؛ سأنتقل للأغنية التالية.")

async def advance_after_track(guild: discord.Guild, finished_track: dict[str, str]) -> None:
    if now_playing.get(guild.id) is finished_track:
        now_playing.pop(guild.id, None)
    await play_next(guild)

def user_shares_bot_voice_channel(ctx: commands.Context) -> bool:
    return bool(ctx.guild and ctx.voice_client and ctx.author.voice and ctx.author.voice.channel == ctx.voice_client.channel)

@bot.event
async def on_ready() -> None:
    logger.info("تم تسجيل الدخول باسم %s", bot.user)

@bot.command(name="ادخل")
@commands.guild_only()
async def join_voice(ctx: commands.Context) -> None:
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("ادخل رومًا صوتيًا أولًا ثم اكتب `!ادخل`.")
        return
    channel = ctx.author.voice.channel
    try:
        if not ctx.voice_client:
            await channel.connect(timeout=25.0, reconnect=True)
        else:
            await ctx.voice_client.move_to(channel, timeout=25.0)
        voice_targets[ctx.guild.id] = channel.id
        await ctx.send(f"✅ دخلت الروم الصوتي: **{channel.name}**")
    except Exception:
        logger.exception("Failed to join voice")
        await ctx.send("تعذر دخول الروم الصوتي. تأكد من الصلاحيات.")

@bot.command(name="ش")
@commands.guild_only()
async def play_song(ctx: commands.Context, *, search: str | None = None) -> None:
    if not search:
        await ctx.send("اكتب اسم الأغنية بعد الأمر، مثل: `ش اسم الأغنية`")
        return
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.send("ادخل رومًا صوتيًا أولًا.")
        return
    try:
        if not ctx.voice_client:
            await ctx.author.voice.channel.connect()
        elif ctx.voice_client.channel != ctx.author.voice.channel:
            await ctx.voice_client.move_to(ctx.author.voice.channel)
        voice_targets[ctx.guild.id] = ctx.author.voice.channel.id
    except Exception:
        await ctx.send("تعذر الاتصال بالروم الصوتي.")
        return

    await ctx.send(f"🔎 أبحث في يوتيوب عن **{search}**...")
    try:
        track = await find_track(search)
    except Exception:
        await ctx.send("ما لقيت الأغنية أو تعذر جلبها. جرّب اسمًا آخر.")
        return

    queues.setdefault(ctx.guild.id, []).append((track, ctx.channel))
    if not ctx.voice_client.is_playing() and not ctx.voice_client.is_paused():
        await play_next(ctx.guild)
    else:
        await ctx.send(f"➕ أُضيفت إلى الطابور: **{track['title']}**")

@bot.command(name="وقف")
@commands.guild_only()
async def pause_song(ctx: commands.Context) -> None:
    if user_shares_bot_voice_channel(ctx) and ctx.voice_client.is_playing():
        ctx.voice_client.pause()
        await ctx.send("⏸️ تم إيقاف الأغنية مؤقتًا.")
    else:
        await ctx.send("يجب أن تكون في نفس الروم الصوتي وتعمل أغنية.")

@bot.command(name="ك", aliases=["كمل"])
@commands.guild_only()
async def resume_song(ctx: commands.Context) -> None:
    if user_shares_bot_voice_channel(ctx) and ctx.voice_client.is_paused():
        ctx.voice_client.resume()
        await ctx.send("▶️ تم استكمال الأغنية.")
    else:
        await ctx.send("ما فيه أغنية متوقفة مؤقتًا.")

@bot.command(name="س", aliases=["سكب"])
@commands.guild_only()
async def skip_song(ctx: commands.Context) -> None:
    if user_shares_bot_voice_channel(ctx) and (ctx.voice_client.is_playing() or ctx.voice_client.is_paused()):
        ctx.voice_client.stop()
        await ctx.send("⏭️ تم تخطي الأغنية.")
    else:
        await ctx.send("ما فيه أغنية لتخطيها.")

@bot.command(name="طابور")
@commands.guild_only()
async def show_queue(ctx: commands.Context) -> None:
    queue = queues.get(ctx.guild.id, [])
    current = now_playing.get(ctx.guild.id)
    heading = f"🎵 تعمل الآن: **{current['title']}**\n" if current else ""
    if not queue:
        await ctx.send(heading or "قائمة الانتظار فارغة.")
        return
    lines = [f"{i}. {t['title']}" for i, (t, _) in enumerate(queue[:15], 1)]
    await ctx.send(heading + "**قائمة الانتظار:**\n" + "\n".join(lines))

@bot.command(name="اخرج")
@commands.guild_only()
async def leave_voice(ctx: commands.Context) -> None:
    if ctx.voice_client:
        await ctx.voice_client.disconnect()
        queues.pop(ctx.guild.id, None)
        now_playing.pop(ctx.guild.id, None)
        await ctx.send("👋 خرجت من الروم الصوتي.")
    else:
        await ctx.send("أنا لست في روم صوتي.")

if not TOKEN:
    raise RuntimeError("الرجاء إضافة متغير البيئة DISCORD_TOKEN في إعدادات منصة الاستضافة.")

bot.run(TOKEN)
