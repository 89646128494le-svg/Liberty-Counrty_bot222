# -*- coding: utf-8 -*-
"""
Мощный музыкальный модуль для Liberty Country

Этот cog реализует полноценный аудиоплеер с поддержкой очереди, плейлистов,
перемотки, фильтров, автоплей режима и интеграции с админ‑панелью сайта.

Поддерживаются два режима воспроизведения: локальный (через FFmpeg и yt‑dlp)
и LavaLink. Выбор бэкенда осуществляется через переменную окружения
``LC_MUSIC_BACKEND``. По умолчанию используется локальный режим.

Модуль также экспортирует состояние плеера в JSON файл, который читается
админ‑панелью и позволяет отображать текущий трек, очередь, прогресс и
прочую информацию. Управление из веб‑панели осуществляется посредством
очереди команд в файле ``control_queue.jsonl``. Бот постоянно следит за
этим файлом и выполняет запрошенные действия.
"""

from __future__ import annotations

import asyncio
import functools
import json
import math
import os
import random
import re
import time
from dataclasses import dataclass
from typing import Deque, Dict, List, Optional, Tuple
from collections import deque

import discord
from discord import app_commands, Interaction
from discord.ext import commands

# Опциональная поддержка Lavalink (wavelink)
BACKEND = os.getenv("LC_MUSIC_BACKEND", "local").lower().strip()
try:
    import wavelink  # type: ignore
    WAVLINK_AVAILABLE = True
except Exception:
    WAVLINK_AVAILABLE = False

# yt-dlp для локального бэкенда
try:
    import yt_dlp as ytdlp
except Exception:
    ytdlp = None

FFMPEG_BINARY = os.getenv("FFMPEG_BINARY", "ffmpeg")

# Пути к файлам состояния/команд. Эти значения должны совпадать с
# переменными окружения, используемыми в админ‑панели.
STATE_FILE = os.getenv("LC_STATE_FILE", "lc_nowplaying.json")
CONTROL_FILE = os.getenv("LC_CONTROL_FILE", "control_queue.jsonl")

URL_REGEX = re.compile(r"https?://", re.I)
SPOTIFY_REGEX = re.compile(r"^https?://(?:open\.)?spotify\.com/(track|album|playlist)/", re.I)


@dataclass
class Track:
    """Описание одного аудио трека."""
    title: str
    webpage_url: str
    stream_url: str
    duration: int
    requested_by: int
    source: str = "yt"
    thumb: Optional[str] = None
    uploader: Optional[str] = None

    @property
    def duration_str(self) -> str:
        if self.duration <= 0:
            return "LIVE"
        m, s = divmod(self.duration, 60)
        h, m = divmod(m, 60)
        return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class LoopMode:
    OFF = 0
    ONE = 1
    ALL = 2


@dataclass
class GuildState:
    """Состояние плеера для отдельной гильдии."""
    queue: Deque[Track]
    loop: int = LoopMode.OFF
    volume: float = 1.0  # 0.0 – 2.0
    filter_id: Optional[str] = None
    last_text_channel_id: Optional[int] = None
    idle_since: float = 0.0
    lock: asyncio.Lock = asyncio.Lock()
    now: Optional[Track] = None
    autoplay: bool = False
    radio_query: Optional[str] = None

    def clear(self):
        self.queue.clear()
        self.loop = LoopMode.OFF
        self.filter_id = None
        self.now = None
        self.autoplay = False
        self.radio_query = None


def _ffmpeg_options(filter_id: Optional[str], volume: float) -> Tuple[List[str], List[str]]:
    """Сборка опций для FFmpeg с фильтрами и громкостью."""
    before = [
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "5",
        "-nostdin",
    ]
    filters = {
        None: "",
        "bassboost": "bass=g=8,dynaudnorm=f=200",
        "nightcore": "asetrate=48000*1.25,aresample=48000,atempo=1.0",
        "vaporwave": "asetrate=44100*0.8,aresample=44100,atempo=1.0",
        "karaoke": "stereotools=mlev=0.03",
        "echo": "aecho=0.6:0.6:30:0.25",
    }
    chain = []
    if filter_id and filters.get(filter_id):
        chain.append(filters[filter_id])
    volume = max(0.0, min(2.0, volume))
    chain.append(f"volume={volume:.2f}")
    opts = ["-vn", "-af", ",".join(chain), "-bufsize", "64k"]
    return before, opts


async def _run_in_executor(func, *args):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(func, *args))


def _load_json_state() -> Dict[str, dict]:
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_json_state(state: Dict[str, dict]):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except Exception:
        pass


def _snapshot_state(st: GuildState) -> dict:
    """Подготовка снимка состояния гильдии для JSON."""
    q_preview = [
        {"title": t.title, "web": t.webpage_url, "dur": t.duration}
        for t in list(st.queue)[: int(os.getenv("LC_QUEUE_EXPORT_LIMIT", "100"))]
    ]
    payload = {
        "status": "idle" if not st.now else "playing",
        "loop": st.loop in (LoopMode.ONE, LoopMode.ALL),
        "volume": int(st.volume * 100),
        "filter": st.filter_id or "off",
        "queue_len": len(st.queue),
        "queue": q_preview,
    }
    if st.now:
        payload.update(
            {
                "title": st.now.title,
                "web": st.now.webpage_url,
                "duration": int(st.now.duration),
                "thumb": st.now.thumb,
                "started_at": int(time.time()),
            }
        )
    return payload


def _update_state(guild_id: int, payload: Optional[dict]):
    """Обновление JSON состояния для конкретной гильдии."""
    try:
        state = _load_json_state()
        if payload is None:
            state.pop(str(guild_id), None)
        else:
            state[str(guild_id)] = payload
        _save_json_state(state)
    except Exception:
        pass


def _append_control(action: str, payload: dict):
    """Добавление команды в файл управления (используется панелью)."""
    try:
        os.makedirs(os.path.dirname(os.path.abspath(CONTROL_FILE)), exist_ok=True)
        with open(CONTROL_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({"action": action, "payload": payload}, ensure_ascii=False) + "\n")
    except Exception:
        pass


class LocalBackend:
    """Локальный режим воспроизведения через FFmpeg и yt‑dlp."""
    def __init__(self, cog: "MusicPower"):
        self.cog = cog

    async def search(self, query: str) -> List[Track]:
        if not ytdlp:
            raise RuntimeError("yt-dlp не установлен. pip install -U yt-dlp")

        # Обработка Spotify: извлекаем названия треков и ищем в YouTube
        if SPOTIFY_REGEX.search(query):
            info = await _run_in_executor(self._extract_spotify, query)
            titles: List[str] = []
            titles.extend(info.get("entries_titles") or [])
            if info.get("single_title"):
                titles.insert(0, info["single_title"])
            results: List[Track] = []
            for title in titles[:100]:
                sub = await self.search(f"ytsearch1:{title}")
                if sub:
                    results.append(sub[0])
            return results

        # Обычный поиск по YouTube / прямая ссылка
        info = await _run_in_executor(
            self._extract,
            query if URL_REGEX.search(query) else f"ytsearch1:{query}"
        )
        tracks: List[Track] = []
        if "entries" in info:
            for e in info["entries"]:
                if not e:
                    continue
                tracks.append(self._make_track(e))
        else:
            tracks.append(self._make_track(info))
        return tracks

    def _extract(self, q: str) -> dict:
        ydl_opts = {
            "format": "bestaudio/best",
            "quiet": True,
            "default_search": "ytsearch",
            "source_address": "0.0.0.0",
        }
        with ytdlp.YoutubeDL(ydl_opts) as ydl:
            return ydl.extract_info(q, download=False)

    def _extract_spotify(self, url: str) -> dict:
        with ytdlp.YoutubeDL({"quiet": True}) as ydl:
            info = ydl.extract_info(url, download=False)
        out: dict = {"entries_titles": []}
        if info.get("_type") == "playlist" and info.get("entries"):
            for e in info["entries"]:
                title = " ".join([e.get("track", ""), e.get("artist", "")]).strip() or e.get("title") or ""
                if title:
                    out["entries_titles"].append(title)
        else:
            title = " ".join([info.get("track", ""), info.get("artist", "")]).strip() or info.get("title") or ""
            if title:
                out["single_title"] = title
        return out

    def _make_track(self, d: dict) -> Track:
        title = d.get("title") or "Без названия"
        web = d.get("webpage_url") or d.get("original_url") or ""
        stream = d.get("url") or web
        dur = int(d.get("duration") or 0)
        thumb = d.get("thumbnail")
        up = d.get("uploader") or d.get("channel")
        return Track(
            title=title,
            webpage_url=web,
            stream_url=stream,
            duration=dur,
            requested_by=0,
            source="yt",
            thumb=thumb,
            uploader=up,
        )

    def build_source(self, track: Track, st: GuildState) -> discord.AudioSource:
        before, opts = _ffmpeg_options(st.filter_id, st.volume)
        src = discord.FFmpegPCMAudio(
            track.stream_url,
            executable=FFMPEG_BINARY,
            before_options=" ".join(before),
            options=" ".join(opts),
        )
        return discord.PCMVolumeTransformer(src, volume=1.0)


class LavalinkBackend:
    """Бэкенд на основе Lavalink/Wavelink."""
    def __init__(self, cog: "MusicPower"):
        self.cog = cog
        self.ready = False

    async def ensure_node(self, bot: commands.Bot):
        if not WAVLINK_AVAILABLE:
            raise RuntimeError("wavelink не установлен. pip install -U wavelink")
        if self.ready:
            return
        host = os.getenv("LAVALINK_HOST", "127.0.0.1")
        port = int(os.getenv("LAVALINK_PORT", "2333"))
        password = os.getenv("LAVALINK_PASS", "youshallnotpass")
        try:
            node = wavelink.Node(uri=f"http://{host}:{port}", password=password)
            await wavelink.Pool.connect(client=bot, nodes=[node])
            self.ready = True
        except Exception as e:
            raise RuntimeError(f"Не удалось подключиться к Lavalink: {e}")

    async def search(self, query: str) -> List[Track]:
        await self.ensure_node(self.cog.bot)
        if not URL_REGEX.search(query):
            q = f"ytsearch:{query}"
        else:
            q = query
        results = await wavelink.Pool.fetch_tracks(q)
        out: List[Track] = []
        for t in results[:100]:
            dur = 0 if t.is_stream else int(t.length / 1000)
            out.append(
                Track(
                    title=t.title,
                    webpage_url=t.uri or "",
                    stream_url=t.uri or "",
                    duration=dur,
                    requested_by=0,
                    source="lavalink",
                    thumb=None,
                    uploader=getattr(t, "author", None),
                )
            )
        return out

    def build_source(self, track: Track, st: GuildState):
        return None  # В Lavalink источник создается внутри wavelink


class MusicPower(commands.Cog, name="MusicPower"):
    """Главный Cog, реализующий музыкальные функции."""
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.states: Dict[int, GuildState] = {}
        self.backend = LavalinkBackend(self) if (BACKEND == "lavalink" and WAVLINK_AVAILABLE) else LocalBackend(self)
        # Фоновые задачи
        self.control_task = self.bot.loop.create_task(self._control_queue_watcher())
        self.idle_task = self.bot.loop.create_task(self._idle_disconnect())
        self.playlist_db_path = "music.db"
        self._db_ready = False
        self.bot.loop.create_task(self._init_playlist_db())

        # При использовании Lavalink и наличии wavelink — подключаемся к ноде
        if BACKEND == "lavalink" and WAVLINK_AVAILABLE:
            @bot.event
            async def on_ready():
                try:
                    await self.backend.ensure_node(bot)
                except Exception as e:
                    print("Lavalink init error:", e)

    # --------------- Вспомогательные методы -----------------
    def gs(self, guild_id: int) -> GuildState:
        if guild_id not in self.states:
            self.states[guild_id] = GuildState(queue=deque())
        return self.states[guild_id]

    async def ensure_connected(self, inter: Interaction, channel: Optional[discord.VoiceChannel] = None) -> Optional[discord.VoiceClient]:
        if not inter.guild:
            await inter.response.send_message("❌ Только на сервере.", ephemeral=True)
            return None
        vc = inter.guild.voice_client
        dest = channel
        if not dest:
            if isinstance(inter.user, discord.Member) and inter.user.voice and inter.user.voice.channel:
                dest = inter.user.voice.channel
        if vc and dest and vc.channel.id != dest.id:
            await vc.move_to(dest)
            return vc
        if vc:
            return vc
        if not dest:
            await inter.response.send_message("❌ Зайдите в голосовой канал или укажите его.", ephemeral=True)
            return None
        try:
            return await dest.connect(timeout=10.0, reconnect=True)
        except Exception as e:
            await inter.response.send_message(f"❌ Не удалось подключиться: {e}", ephemeral=True)
            return None

    def _now_embed(self, st: GuildState) -> discord.Embed:
        e = discord.Embed(color=discord.Color.blurple(), title="🎵 Liberty Country — Now Playing")
        if st.now:
            e.add_field(name="Трек", value=f"[{st.now.title}]({st.now.webpage_url})", inline=False)
            e.add_field(name="Длительность", value=st.now.duration_str, inline=True)
        e.add_field(name="Громкость", value=f"{int(st.volume * 100)}%", inline=True)
        e.add_field(name="Петля", value={0: "выкл", 1: "текущий", 2: "очередь"}[st.loop], inline=True)
        if st.filter_id:
            e.add_field(name="Фильтр", value=st.filter_id, inline=True)
        if st.autoplay:
            e.add_field(name="Автоплей", value="вкл", inline=True)
        if st.radio_query:
            e.add_field(name="Радио", value=st.radio_query, inline=True)
        if st.queue:
            preview = list(st.queue)[:5]
            lines = [f"**{i+1}.** {t.title}" for i, t in enumerate(preview)]
            e.add_field(name=f"Очередь ({len(st.queue)})", value="\n".join(lines), inline=False)
        return e

    async def _init_playlist_db(self):
        import aiosqlite
        try:
            async with aiosqlite.connect(self.playlist_db_path) as db:
                await db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS playlists (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        guild_id INTEGER,
                        name TEXT,
                        created_at INTEGER
                    )
                    """
                )
                await db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS playlist_items (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        playlist_id INTEGER,
                        title TEXT,
                        url TEXT,
                        duration INTEGER
                    )
                    """
                )
                await db.commit()
            self._db_ready = True
        except Exception as e:
            print("music.db init error:", e)

    # ----------- Методы для работы с плейлистами ------------
    async def pl_get_id(self, guild_id: int, name: str) -> Optional[int]:
        import aiosqlite
        async with aiosqlite.connect(self.playlist_db_path) as db:
            cur = await db.execute("SELECT id FROM playlists WHERE guild_id=? AND name=?", (guild_id, name))
            row = await cur.fetchone()
            return row[0] if row else None

    async def pl_create(self, guild_id: int, name: str) -> int:
        import aiosqlite
        async with aiosqlite.connect(self.playlist_db_path) as db:
            await db.execute(
                "INSERT INTO playlists (guild_id, name, created_at) VALUES (?, ?, ?)",
                (guild_id, name, int(time.time())),
            )
            await db.commit()
            cur = await db.execute("SELECT id FROM playlists WHERE guild_id=? AND name=?", (guild_id, name))
            row = await cur.fetchone()
            return row[0]

    async def pl_items(self, playlist_id: int) -> List[Tuple[str, str, int]]:
        import aiosqlite
        async with aiosqlite.connect(self.playlist_db_path) as db:
            cur = await db.execute(
                "SELECT title, url, duration FROM playlist_items WHERE playlist_id=?",
                (playlist_id,),
            )
            rows = await cur.fetchall()
            return [(r[0], r[1], r[2]) for r in rows]

    async def pl_add_tracks(self, playlist_id: int, tracks: List[Track]):
        import aiosqlite
        async with aiosqlite.connect(self.playlist_db_path) as db:
            await db.executemany(
                "INSERT INTO playlist_items (playlist_id, title, url, duration) VALUES (?, ?, ?, ?)",
                [(playlist_id, t.title, t.webpage_url, t.duration) for t in tracks],
            )
            await db.commit()

    async def pl_delete(self, guild_id: int, name: str) -> bool:
        import aiosqlite
        async with aiosqlite.connect(self.playlist_db_path) as db:
            cur = await db.execute(
                "SELECT id FROM playlists WHERE guild_id=? AND name=?",
                (guild_id, name),
            )
            row = await cur.fetchone()
            if not row:
                return False
            pid = row[0]
            await db.execute("DELETE FROM playlist_items WHERE playlist_id=?", (pid,))
            await db.execute("DELETE FROM playlists WHERE id=?", (pid,))
            await db.commit()
            return True

    async def pl_list(self, guild_id: int) -> List[str]:
        import aiosqlite
        async with aiosqlite.connect(self.playlist_db_path) as db:
            cur = await db.execute(
                "SELECT name FROM playlists WHERE guild_id=? ORDER BY created_at DESC",
                (guild_id,),
            )
            rows = await cur.fetchall()
            return [r[0] for r in rows]

    # --------------- Логика проигрывания --------------------
    async def play_next(self, inter: Interaction, *, started_by_skip: bool = False):
        guild = inter.guild
        if not guild:
            return
        vc = guild.voice_client
        # При lavalink — переподключаемся к Player, если требуется
        if BACKEND == "lavalink" and WAVLINK_AVAILABLE and vc and not isinstance(vc, wavelink.Player):
            if isinstance(inter.user, discord.Member) and inter.user.voice:
                vc = await inter.user.voice.channel.connect(cls=wavelink.Player)
        st = self.gs(guild.id)
        async with st.lock:
            if st.now and st.loop == LoopMode.ONE and not started_by_skip:
                track = st.now
            else:
                if not st.queue:
                    st.now = None
                    _update_state(guild.id, _snapshot_state(st))
                    st.idle_since = time.time()
                    return
                track = st.queue.popleft()
                # Если включён повтор очереди — предыдущий добавляем в конец
                if st.loop == LoopMode.ALL and st.now and not started_by_skip:
                    st.queue.append(st.now)
            st.now = track
            # Обновим JSON
            payload = _snapshot_state(st)
            if st.now:
                payload.update(
                    {
                        "title": st.now.title,
                        "web": st.now.webpage_url,
                        "duration": int(st.now.duration),
                        "thumb": st.now.thumb,
                        "started_at": int(time.time()),
                        "status": "playing",
                    }
                )
            _update_state(guild.id, payload)
            try:
                if BACKEND == "lavalink" and WAVLINK_AVAILABLE:
                    await self._play_lavalink(inter, st, track)
                else:
                    if not vc:
                        return
                    await self._play_local(inter, st, vc, track)
            except Exception as e:
                try:
                    await inter.followup.send(f"⚠️ Ошибка воспроизведения: {e}", ephemeral=True)
                except Exception:
                    pass
                await self.play_next(inter)

    async def _play_local(self, inter: Interaction, st: GuildState, vc: discord.VoiceClient, track: Track):
        before, opts = _ffmpeg_options(st.filter_id, st.volume)
        source = discord.FFmpegPCMAudio(
            track.stream_url,
            executable=FFMPEG_BINARY,
            before_options=" ".join(before),
            options=" ".join(opts),
        )
        source = discord.PCMVolumeTransformer(source, volume=1.0)

        def after_play(err: Optional[Exception]):
            fut = self._after_finished(inter, err)
            asyncio.run_coroutine_threadsafe(fut, self.bot.loop)

        vc.play(source, after=after_play)

    async def _play_lavalink(self, inter: Interaction, st: GuildState, track: Track):
        await self.backend.ensure_node(self.bot)
        player: wavelink.Player = inter.guild.voice_client  # type: ignore
        if not isinstance(player, wavelink.Player):
            # подключаемся как Player, если нужно
            if isinstance(inter.user, discord.Member) and inter.user.voice:
                player = await inter.user.voice.channel.connect(cls=wavelink.Player)
        results = await wavelink.Pool.fetch_tracks(
            track.webpage_url if URL_REGEX.search(track.webpage_url) else f"ytsearch:{track.title}"
        )
        if results:
            await player.play(results[0])

    async def _after_finished(self, inter: Interaction, err: Optional[Exception]):
        st = self.gs(inter.guild.id)
        # Поддержка автоплей/радио
        if not st.queue and (st.autoplay or st.radio_query):
            try:
                seed = st.radio_query or (st.now.title if st.now else None)
                if seed:
                    candidates = await self.backend.search(f"ytsearch10:{seed} official audio")
                    if candidates:
                        st.queue.append(random.choice(candidates))
            except Exception:
                pass
        await self.play_next(inter)

    # --------------- Slash-команды -------------------
    group = app_commands.Group(name="music", description="Музыкальные команды Liberty Country")

    @group.command(name="join", description="Подключить бота к голосовому каналу")
    async def join(self, inter: Interaction, channel: Optional[discord.VoiceChannel] = None):
        vc = await self.ensure_connected(inter, channel)
        if vc:
            self.gs(inter.guild.id).last_text_channel_id = inter.channel_id
            await inter.response.send_message(f"✅ В голосовом: **{vc.channel.name}**", ephemeral=True)

    @group.command(name="leave", description="Отключить бота от канала")
    async def leave(self, inter: Interaction):
        vc = inter.guild.voice_client if inter.guild else None
        if not vc:
            await inter.response.send_message("ℹ️ Я не в голосовом канале.", ephermal=True)
            return
        await inter.response.defer(ephemeral=True)
        try:
            await vc.disconnect(force=True)
        except Exception:
            pass
        self.gs(inter.guild.id).clear()
        _update_state(inter.guild.id, None)
        await inter.followup.send("👋 Отключился.", ephemeral=True)

    @group.command(name="play", description="Проиграть трек/плейлист (URL или поиск)")
    async def play(self, inter: Interaction, query: str):
        await inter.response.defer(ephemeral=True)
        vc = await self.ensure_connected(inter)
        if not vc:
            return
        st = self.gs(inter.guild.id)
        st.last_text_channel_id = inter.channel_id
        try:
            tracks = await self.backend.search(query)
        except Exception as e:
            await inter.followup.send(f"❌ Не удалось получить аудио: {e}", ephermal=True)
            return
        for t in tracks:
            t.requested_by = inter.user.id
            st.queue.append(t)
        first = tracks[0] if tracks else None
        if not vc.is_playing() and not vc.is_paused():
            await self.play_next(inter)
            msg = f"▶️ Запускаю: **{first.title}**"
            if len(tracks) > 1:
                msg += f" (+{len(tracks) - 1})"
            await inter.followup.send(msg, ephermal=True)
        else:
            msg = f"➕ В очередь: **{first.title}**"
            if len(tracks) > 1:
                msg += f" (+{len(tracks) - 1})"
            await inter.followup.send(msg, ephermal=True)
        # Обновим embed в чате
        try:
            await inter.channel.send(embed=self._now_embed(st), view=self._controls_view(inter.guild.id))
        except Exception:
            pass

    @group.command(name="pause", description="Пауза")
    async def pause(self, inter: Interaction):
        vc = inter.guild.voice_client if inter.guild else None
        if BACKEND == "lavalink" and WAVLINK_AVAILABLE and isinstance(vc, wavelink.Player):
            await vc.pause()
        elif not vc or not vc.is_playing():
            await inter.response.send_message("ℹ️ Нечего ставить на паузу.", ephermal=True)
            return
        else:
            vc.pause()
        _update_state(inter.guild.id, {"status": "paused"})
        await inter.response.send_message("⏸ Пауза.", ephermal=True)

    @group.command(name="resume", description="Продолжить")
    async def resume(self, inter: Interaction):
        vc = inter.guild.voice_client if inter.guild else None
        if BACKEND == "lavalink" and WAVLINK_AVAILABLE and isinstance(vc, wavelink.Player):
            await vc.resume()
        elif not vc or not vc.is_paused():
            await inter.response.send_message("ℹ️ Не на паузе.", ephermal=True)
            return
        else:
            vc.resume()
        _update_state(inter.guild.id, {"status": "playing"})
        await inter.response.send_message("▶️ Продолжил.", ephermal=True)

    @group.command(name="skip", description="Пропустить текущий трек")
    async def skip(self, inter: Interaction):
        vc = inter.guild.voice_client if inter.guild else None
        await inter.response.defer(ephemeral=True)
        if BACKEND == "lavalink" and WAVLINK_AVAILABLE and isinstance(vc, wavelink.Player):
            await vc.stop()
        elif not vc or not vc.is_playing():
            await inter.followup.send("ℹ️ Сейчас ничего не играет.", ephermal=True)
            return
        else:
            vc.stop()
        await self.play_next(inter, started_by_skip=True)
        await inter.followup.send("⏭ Пропустил.", ephermal=True)

    @group.command(name="stop", description="Остановить плеер и очистить очередь")
    async def stop(self, inter: Interaction):
        st = self.gs(inter.guild.id)
        st.clear()
        vc = inter.guild.voice_client if inter.guild else None
        if BACKEND == "lavalink" and WAVLINK_AVAILABLE and isinstance(vc, wavelink.Player):
            await vc.stop()
        elif vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()
        _update_state(inter.guild.id, None)
        await inter.response.send_message("⏹ Остановил и очистил очередь.", ephermal=True)

    @group.command(name="queue", description="Показать очередь")
    async def queue(self, inter: Interaction, page: Optional[int] = 1):
        st = self.gs(inter.guild.id)
        items = list(st.queue)
        if not items:
            await inter.response.send_message("🎶 Очередь пуста.", ephermal=True)
            return
        per = 10
        pages = max(1, math.ceil(len(items) / per))
        page = max(1, min(pages, page))
        start = (page - 1) * per
        embed = discord.Embed(title=f"📜 Очередь — стр. {page}/{pages}", color=discord.Color.dark_teal())
        for i, t in enumerate(items[start:start + per], start=start + 1):
            embed.add_field(name=f"{i}. {t.title}", value=f"{t.duration_str}", inline=False)
        await inter.response.send_message(embed=embed, ephermal=True)

    @group.command(name="now", description="Что играет сейчас")
    async def now(self, inter: Interaction):
        st = self.gs(inter.guild.id)
        await inter.response.send_message(embed=self._now_embed(st), ephermal=True)

    @group.command(name="shuffle", description="Перемешать очередь")
    async def shuffle(self, inter: Interaction):
        st = self.gs(inter.guild.id)
        q = list(st.queue)
        random.shuffle(q)
        st.queue = deque(q)
        _update_state(inter.guild.id, _snapshot_state(st))
        await inter.response.send_message("🔀 Перемешал очередь.", ephermal=True)

    @group.command(name="loop", description="Режим повтора: off/one/all")
    async def loop(self, inter: Interaction, mode: app_commands.Choice[int]):
        st = self.gs(inter.guild.id)
        st.loop = mode.value
        _update_state(inter.guild.id, {"loop": st.loop != LoopMode.OFF})
        await inter.response.send_message(f"🔁 Повтор: **{mode.name}**", ephermal=True)

    @loop.autocomplete("mode")
    async def loop_ac(self, inter: Interaction, current: str):
        options = [
            app_commands.Choice(name="off", value=LoopMode.OFF),
            app_commands.Choice(name="one", value=LoopMode.ONE),
            app_commands.Choice(name="all", value=LoopMode.ALL),
        ]
        return [o for o in options if o.name.startswith(current.lower())]

    @group.command(name="remove", description="Удалить трек из очереди по номеру")
    async def remove(self, inter: Interaction, index: int):
        st = self.gs(inter.guild.id)
        if index < 1 or index > len(st.queue):
            await inter.response.send_message("❌ Неверный номер.", ephermal=True)
            return
        q = list(st.queue)
        t = q.pop(index - 1)
        st.queue = deque(q)
        _update_state(inter.guild.id, _snapshot_state(st))
        await inter.response.send_message(f"🗑 Удалил: **{t.title}**", ephermal=True)

    @group.command(name="move", description="Переместить трек в очереди")
    async def move(self, inter: Interaction, src_index: int, dst_index: int):
        st = self.gs(inter.guild.id)
        n = len(st.queue)
        if src_index < 1 or dst_index < 1 or src_index > n or dst_index > n:
            await inter.response.send_message("❌ Неверные индексы.", ephermal=True)
            return
        q = list(st.queue)
        t = q.pop(src_index - 1)
        q.insert(dst_index - 1, t)
        st.queue = deque(q)
        _update_state(inter.guild.id, _snapshot_state(st))
        await inter.response.send_message(f"↕ Переместил: **{t.title}** → позиция {dst_index}", ephermal=True)

    @group.command(name="seek", description="Перемотка текущего трека (мм:сс)")
    async def seek(self, inter: Interaction, position: str):
        st = self.gs(inter.guild.id)
        vc = inter.guild.voice_client if inter.guild else None
        if not st.now:
            await inter.response.send_message("ℹ️ Сейчас ничего не играет.", ephermal=True)
            return
        try:
            mm, ss = position.split(":")
            pos = int(mm) * 60 + int(ss)
        except Exception:
            await inter.response.send_message("❌ Формат: мм:сс", ephermal=True)
            return
        if BACKEND == "lavalink" and WAVLINK_AVAILABLE and isinstance(vc, wavelink.Player):
            try:
                await vc.seek(pos * 1000)
                await inter.response.send_message(f"⏩ Перемотал на {position}", ephermal=True)
            except Exception as e:
                await inter.response.send_message(f"❌ Seek error: {e}", ephermal=True)
            return
        if not vc:
            await inter.response.send_message("ℹ️ Нет подключения к голосовому.", ephermal=True)
            return
        # Локальный бэкенд — запускаем заново с -ss
        before, opts = _ffmpeg_options(st.filter_id, st.volume)
        args = ["-ss", str(pos), *before]
        try:
            src = discord.FFmpegPCMAudio(
                st.now.stream_url,
                executable=FFMPEG_BINARY,
                before_options=" ".join(args),
                options=" ".join(opts),
            )
            src = discord.PCMVolumeTransformer(src, volume=1.0)
            vc.play(src, after=lambda e: asyncio.run_coroutine_threadsafe(self._after_finished(inter, e), self.bot.loop))
            await inter.response.send_message(f"⏩ Перемотал на {position}", ephermal=True)
        except Exception as e:
            await inter.response.send_message(f"❌ Не удалось перемотать: {e}", ephermal=True)

    @group.command(name="volume", description="Громкость (0–200%)")
    async def volume(self, inter: Interaction, percent: app_commands.Range[int, 0, 200]):
        st = self.gs(inter.guild.id)
        st.volume = percent / 100.0
        _update_state(inter.guild.id, {"volume": percent})
        await inter.response.send_message(f"🔊 Громкость: {percent}%", ephermal=True)

    @group.command(name="filter", description="Аудио-фильтр (bassboost/nightcore/vaporwave/karaoke/echo/off)")
    async def filter(self, inter: Interaction, preset: str):
        preset = preset.lower().strip()
        allowed = {"bassboost", "nightcore", "vaporwave", "karaoke", "echo", "off"}
        if preset not in allowed:
            await inter.response.send_message("❌ Доступно: bassboost, nightcore, vaporwave, karaoke, echo, off", ephermal=True)
            return
        st = self.gs(inter.guild.id)
        st.filter_id = None if preset == "off" else preset
        await inter.response.send_message(f"🎚 Фильтр: **{preset}** (применится к следующему треку)", ephermal=True)

    @group.command(name="autoplay", description="Автодобавление похожих треков (on/off)")
    async def autoplay(self, inter: Interaction, mode: app_commands.Choice[str]):
        st = self.gs(inter.guild.id)
        st.autoplay = mode.value == "on"
        await inter.response.send_message(f"🧠 Autoplay: **{mode.value}**", ephermal=True)

    @autoplay.autocomplete("mode")
    async def autoplay_ac(self, inter: Interaction, current: str):
        opts = [app_commands.Choice(name="on", value="on"), app_commands.Choice(name="off", value="off")]
        return [o for o in opts if o.name.startswith(current.lower())]

    @group.command(name="radio", description="Радио по теме (бесконечная очередь)")
    async def radio(self, inter: Interaction, query: str):
        st = self.gs(inter.guild.id)
        st.radio_query = query
        await inter.response.send_message(f"📻 Радио тема: **{query}** (очередь будет пополняться)", ephermal=True)

    @group.command(name="replay", description="Сыграть текущий ещё раз")
    async def replay(self, inter: Interaction):
        st = self.gs(inter.guild.id)
        if not st.now:
            await inter.response.send_message("ℹ️ Нечего повторять.", ephermal=True)
            return
        st.queue.appendleft(st.now)
        await inter.response.send_message("🔁 Повтор текущего трека поставлен в начало.", ephermal=True)

    @group.command(name="grab", description="Прислать инфо о треке в ЛС")
    async def grab(self, inter: Interaction):
        st = self.gs(inter.guild.id)
        if not st.now:
            await inter.response.send_message("ℹ️ Сейчас ничего не играет.", ephermal=True)
            return
        try:
            await inter.user.send(f"🎧 Сейчас играет: {st.now.title}\n{st.now.webpage_url}")
            await inter.response.send_message("📩 Отправил в ЛС.", ephermal=True)
        except Exception:
            await inter.response.send_message("❌ Не удалось отправить ЛС.", ephermal=True)

    # Плейлист команды
    pl = app_commands.Group(name="pl", description="Плейлисты")

    @pl.command(name="save", description="Сохранить текущую очередь в плейлист")
    async def pl_save(self, inter: Interaction, name: str):
        st = self.gs(inter.guild.id)
        items = list(st.queue)
        if st.now:
            items.insert(0, st.now)
        if not items:
            await inter.response.send_message("ℹ️ Нет треков для сохранения.", ephermal=True)
            return
        if not self._db_ready:
            await inter.response.send_message("⚠️ Плейлисты ещё инициализируются.", ephermal=True)
            return
        pid = await self.pl_get_id(inter.guild.id, name) or await self.pl_create(inter.guild.id, name)
        await self.pl_add_tracks(pid, items)
        await inter.response.send_message(f"💾 Плейлист **{name}** сохранён (+{len(items)})", ephermal=True)

    @pl.command(name="load", description="Загрузить плейлист в очередь")
    async def pl_load(self, inter: Interaction, name: str, replace: Optional[bool] = False):
        if not self._db_ready:
            await inter.response.send_message("⚠️ Плейлисты ещё инициализируются.", ephermal=True)
            return
        pid = await self.pl_get_id(inter.guild.id, name)
        if not pid:
            await inter.response.send_message("❌ Плейлист не найден.", ephermal=True)
            return
        data = await self.pl_items(pid)
        st = self.gs(inter.guild.id)
        if replace:
            st.queue.clear()
        for title, url, dur in data:
            st.queue.append(
                Track(
                    title=title,
                    webpage_url=url,
                    stream_url=url,
                    duration=int(dur),
                    requested_by=inter.user.id,
                    source="yt",
                )
            )
        _update_state(inter.guild.id, _snapshot_state(st))
        await inter.response.send_message(f"📥 Загрузил **{len(data)}** треков из плейлиста **{name}**.", ephermal=True)

    @pl.command(name="add", description="Добавить очередь в плейлист")
    async def pl_add(self, inter: Interaction, name: str):
        await self.pl_save(inter, name)

    @pl.command(name="delete", description="Удалить плейлист")
    async def pl_delete_cmd(self, inter: Interaction, name: str):
        ok = await self.pl_delete(inter.guild.id, name)
        await inter.response.send_message("🗑 Удалил." if ok else "❌ Не найден.", ephermal=True)

    @pl.command(name="list", description="Список плейлистов")
    async def pl_list_cmd(self, inter: Interaction):
        names = await self.pl_list(inter.guild.id)
        if not names:
            await inter.response.send_message("ℹ️ Плейлистов нет.", ephermal=True)
        else:
            await inter.response.send_message("🎶 Плейлисты:\n" + "\n".join(f"• {n}" for n in names), ephermal=True)

    # --------- Кнопки под embed-треком ----------
    def _controls_view(self, guild_id: int) -> discord.ui.View:
        view = discord.ui.View(timeout=180)
        async def wrap(inter: Interaction, fn):
            if inter.guild and inter.guild.id == guild_id:
                await fn(inter)
                try:
                    await inter.response.send_message("✅", ephermal=True)
                except Exception:
                    pass
        @discord.ui.button(emoji="⏸", style=discord.ButtonStyle.secondary)
        async def _p(btn, inter: Interaction): await wrap(inter, self.pause)  # type: ignore
        @discord.ui.button(emoji="▶️", style=discord.ButtonStyle.success)
        async def _r(btn, inter: Interaction): await wrap(inter, self.resume)  # type: ignore
        @discord.ui.button(emoji="⏭", style=discord.ButtonStyle.primary)
        async def _s(btn, inter: Interaction): await wrap(inter, self.skip)  # type: ignore
        @discord.ui.button(emoji="⏹", style=discord.ButtonStyle.danger)
        async def _x(btn, inter: Interaction): await wrap(inter, self.stop)  # type: ignore
        @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.secondary)
        async def _sh(btn, inter: Interaction): await wrap(inter, self.shuffle)  # type: ignore
        return view

    # ---------- Watcher задач: чтение control_queue и idle disconnect ----------
    async def _control_queue_watcher(self):
        pos = 0
        while not self.bot.is_closed():
            try:
                if not os.path.exists(CONTROL_FILE):
                    await asyncio.sleep(1.0)
                    continue
                # Читаем новые строки
                with open(CONTROL_FILE, "r+", encoding="utf-8") as f:
                    f.seek(pos)
                    lines = f.readlines()
                    pos = f.tell()
                    # очищаем файл, чтобы не рос
                    f.truncate(0)
                for line in lines:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    action = (obj.get("action") or "").lower().strip()
                    payload = obj.get("payload") or {}
                    gid = int(payload.get("guild_id") or 0)
                    if not gid:
                        continue
                    guild = self.bot.get_guild(gid)
                    if not guild:
                        continue
                    # создаем заглушку Interaction для вызова методов
                    class _Shim:
                        def __init__(self, guild):
                            self.guild = guild
                            self.user = guild.me
                            # берём первый текстовый канал, где бот может писать
                            self.channel = guild.system_channel or next((c for c in guild.text_channels if c.permissions_for(guild.me).send_messages), None)
                            self.channel_id = self.channel.id if self.channel else None
                        @property
                        def response(self):
                            class _R:
                                async def send_message(self2, *a, **kw): pass
                                async def defer(self2, *a, **kw): pass
                            return _R()
                        @property
                        def followup(self):
                            class _F:
                                async def send(self2, *a, **kw): pass
                            return _F()
                    inter = _Shim(guild)
                    st = self.gs(gid)
                    # Действия
                    if action == "join":
                        await self.join(inter)  # type: ignore
                    elif action == "play":
                        q = str(payload.get("query") or "")
                        if q:
                            await self.play(inter, q)  # type: ignore
                    elif action == "playtop":
                        # Добавляем трек в начало очереди
                        q = str(payload.get("query") or "")
                        if q:
                            tracks = await self.backend.search(q)
                            for t in reversed(tracks):
                                t.requested_by = 0
                                st.queue.appendleft(t)
                            if not st.now:
                                await self.play_next(inter)
                            _update_state(gid, _snapshot_state(st))
                    elif action == "pause":
                        await self.pause(inter)  # type: ignore
                    elif action == "resume":
                        await self.resume(inter)  # type: ignore
                    elif action == "skip":
                        await self.skip(inter)  # type: ignore
                    elif action == "stop":
                        await self.stop(inter)  # type: ignore
                    elif action == "leave":
                        await self.leave(inter)  # type: ignore
                    elif action == "shuffle":
                        await self.shuffle(inter)  # type: ignore
                    elif action == "loop":
                        # Переключатель: off → one → off
                        st.loop = LoopMode.OFF if st.loop else LoopMode.ONE
                        _update_state(gid, {"loop": st.loop != LoopMode.OFF})
                    elif action == "volume":
                        try:
                            pct = int(payload.get("percent", 100))
                            st.volume = max(0, min(200, pct)) / 100.0
                            _update_state(gid, {"volume": pct})
                        except Exception:
                            pass
                    elif action == "queue_remove":
                        idx = int(payload.get("index", 0))
                        if 1 <= idx <= len(st.queue):
                            q = list(st.queue)
                            q.pop(idx - 1)
                            st.queue = deque(q)
                            _update_state(gid, _snapshot_state(st))
                    elif action == "queue_move":
                        src = int(payload.get("src_index", 0))
                        dst = int(payload.get("dst_index", 0))
                        if 1 <= src <= len(st.queue) and 1 <= dst <= len(st.queue):
                            q = list(st.queue)
                            t = q.pop(src - 1)
                            q.insert(dst - 1, t)
                            st.queue = deque(q)
                            _update_state(gid, _snapshot_state(st))
                    elif action == "queue_clear":
                        st.queue.clear()
                        _update_state(gid, _snapshot_state(st))
                    elif action == "queue_play_index":
                        idx = int(payload.get("index", 0))
                        if 1 <= idx <= len(st.queue):
                            q = list(st.queue)
                            track = q.pop(idx - 1)
                            q.insert(0, track)
                            st.queue = deque(q)
                            # остановим текущее и запустим новый
                            vc = guild.voice_client
                            if vc:
                                if BACKEND == "lavalink" and WAVLINK_AVAILABLE and isinstance(vc, wavelink.Player):
                                    await vc.stop()
                                else:
                                    vc.stop()
                            await self.play_next(inter, started_by_skip=True)
                    elif action == "playlist_save":
                        name = str(payload.get("name") or "").strip()
                        if name:
                            if not self._db_ready:
                                continue
                            items = list(st.queue)
                            if st.now:
                                items.insert(0, st.now)
                            if items:
                                pid = await self.pl_get_id(gid, name) or await self.pl_create(gid, name)
                                await self.pl_add_tracks(pid, items)
                    elif action == "playlist_load":
                        name = str(payload.get("name") or "").strip()
                        replace = bool(payload.get("replace", False))
                        if name and self._db_ready:
                            pid = await self.pl_get_id(gid, name)
                            if pid:
                                data = await self.pl_items(pid)
                                if replace:
                                    st.queue.clear()
                                for title, url, dur in data:
                                    st.queue.append(
                                        Track(
                                            title=title,
                                            webpage_url=url,
                                            stream_url=url,
                                            duration=int(dur),
                                            requested_by=0,
                                        )
                                    )
                                _update_state(gid, _snapshot_state(st))
                    elif action == "playlist_delete":
                        name = str(payload.get("name") or "").strip()
                        if name and self._db_ready:
                            await self.pl_delete(gid, name)
                await asyncio.sleep(1.0)
            except Exception:
                await asyncio.sleep(1.0)

    async def _idle_disconnect(self):
        while not self.bot.is_closed():
            try:
                for gid, st in list(self.states.items()):
                    guild = self.bot.get_guild(gid)
                    if not guild:
                        continue
                    vc = guild.voice_client
                    is_playing = False
                    if BACKEND == "lavalink" and WAVLINK_AVAILABLE and isinstance(vc, wavelink.Player):
                        is_playing = vc.is_playing()
                    else:
                        is_playing = vc.is_playing() if vc else False
                    if vc and not is_playing:
                        if not st.idle_since:
                            st.idle_since = time.time()
                        if time.time() - st.idle_since > 120 and not st.queue:
                            try:
                                await vc.disconnect(force=True)
                            except Exception:
                                pass
                            _update_state(gid, None)
                            st.clear()
                            st.idle_since = 0.0
                    else:
                        st.idle_since = 0.0
            except Exception:
                pass
            await asyncio.sleep(5)

async def setup(bot: commands.Bot):
    await bot.add_cog(MusicPower(bot))