"""
================================================================================
 ENTERPRISE TELEGRAM RELAY USERBOT
 Async Smart Queue with SQLite Persistence, JIT Fetching & FloodWait Resilience
================================================================================

ARCHITECTURE OVERVIEW
----------------------
1. Ingestion Layer   -> NewMessage listener. Writes a lightweight "pointer"
                        row (message_id, channel_id, grouped_id) into SQLite.
                        No text/media is stored -- we never trust stale state.
2. Persistence Layer -> aiosqlite database in WAL mode. Survives crashes:
                        on restart, any 'pending' row is still there and will
                        be picked up by the dispatcher.
3. Dispatcher Layer   -> Background asyncio worker that polls every N seconds,
                        groups album parts by grouped_id, and performs a
                        "Just-In-Time" client.get_messages() fetch to get the
                        LIVE message (freshest text/media, or None if deleted)
                        right before copying it to the target chat.
4. Resilience Layer   -> FloodWaitError pauses ONLY the dispatcher; ingestion
                        keeps listening and queuing uninterrupted. Graceful
                        shutdown on SIGINT/SIGTERM flushes the DB connection
                        and disconnects the Telethon client cleanly. Rows that
                        keep failing (permanent RPCErrors, etc.) are retried a
                        bounded number of times and then parked in a 'failed'
                        state instead of looping forever (see QueueDatabase /
                        DispatcherService retry handling below).

Run with:  python main.py

SESSION SETUP (StringSession) -- SESSION_STRING IS NOW MANDATORY
------------------------------------------------------------------
This build authenticates via Telethon's StringSession instead of a .session
file, because a .session file sitting on a PaaS Persistent Volume is painful
to move across redeploys/host migrations and effectively forces you to redo
the interactive login whenever the volume is gone.

main.py itself NO LONGER falls back to an interactive/file-based login when
SESSION_STRING is missing -- it fails fast with a clear RuntimeError instead.
This is intentional: a PaaS container has no stdin, so a silent interactive
prompt would just hang the process forever (no crash, no log, nothing --
looking exactly like a stuck "building" deploy), and a hung deploy can trick
some platforms into leaving an old instance alive next to the new hung one,
which means two live userbot sessions relaying the same channels and every
message getting delivered twice.

To obtain a SESSION_STRING, run the small one-off helper script
`generate_session.py` (included alongside this file) locally/interactively
ONCE. It logs in, prints the resulting string, and exits. Copy that value
into your PaaS platform's environment variables as SESSION_STRING before
deploying main.py.
================================================================================
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import signal
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

import aiosqlite
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, RPCError
from telethon.sessions import StringSession
from telethon.tl.custom.message import Message

# ==============================================================================
# LOGGING SETUP
# ==============================================================================
_log_handlers: List[logging.Handler] = [logging.StreamHandler(sys.stdout)]
if os.getenv("LOG_TO_FILE", "1") not in ("0", "false", "False"):
    _log_handlers.append(
        RotatingFileHandler(
            os.getenv("LOG_FILE_PATH", "userbot.log"),
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
    )

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    handlers=_log_handlers,
)
logger = logging.getLogger("TelegramRelayBot")


def _log_unhandled_exception(loop: asyncio.AbstractEventLoop, context: Dict[str, Any]) -> None:
    exc = context.get("exception")
    if exc:
        logger.error("Unhandled asyncio exception: %s", context.get("message"), exc_info=exc)
    else:
        logger.error("Unhandled asyncio error: %s", context.get("message"))


# ==============================================================================
# CONFIGURATION (loaded from .env)
# ==============================================================================
load_dotenv()

ChatRef = Union[int, str]


@dataclass(frozen=True)
class Settings:
    api_id: int
    api_hash: str
    session_name: str
    session_string: Optional[str]
    source_channels: List[ChatRef]
    target_chat: ChatRef
    delay_minutes: int
    db_path: str
    poll_interval_seconds: int
    album_wait_seconds: int
    send_delay_min_seconds: float
    send_delay_max_seconds: float
    relay_mode: str
    connection_retries: int
    retry_delay_seconds: int
    fetch_batch_limit: int
    health_check_port: Optional[int]
    max_delivery_retries: int
    journal_mode: str

    @staticmethod
    def _parse_chat_ref(raw: str) -> ChatRef:
        raw = raw.strip()
        try:
            return int(raw)
        except ValueError:
            return raw

    @classmethod
    def _parse_chat_list(cls, raw: str) -> List[ChatRef]:
        return [cls._parse_chat_ref(p) for p in raw.split(",") if p.strip()]

    @classmethod
    def from_env(cls) -> "Settings":
        try:
            api_id = int(os.environ["API_ID"])
            api_hash = os.environ["API_HASH"]
        except KeyError as exc:
            raise RuntimeError(f"Missing required environment variable: {exc}") from exc
        except ValueError as exc:
            raise RuntimeError("API_ID must be an integer") from exc

        source_raw = os.getenv("SOURCE_CHANNELS", "").strip()
        target_raw = os.getenv("TARGET_CHAT", "").strip()
        if not source_raw:
            raise RuntimeError("SOURCE_CHANNELS must be set in .env (comma-separated)")
        if not target_raw:
            raise RuntimeError("TARGET_CHAT must be set in .env")

        relay_mode = os.getenv("RELAY_MODE", "copy").strip().lower()
        if relay_mode not in ("copy", "forward"):
            raise RuntimeError("RELAY_MODE must be either 'copy' or 'forward'")

        journal_mode = os.getenv("JOURNAL_MODE", "WAL").strip().upper()
        if journal_mode not in ("WAL", "TRUNCATE", "DELETE", "PERSIST", "MEMORY", "OFF"):
            raise RuntimeError(
                "JOURNAL_MODE must be one of WAL, TRUNCATE, DELETE, PERSIST, MEMORY, OFF"
            )

        legacy_delay = float(os.getenv("SEND_DELAY_SECONDS", "1.5"))
        send_delay_min_seconds = float(
            os.getenv("SEND_DELAY_MIN_SECONDS", str(legacy_delay))
        )
        send_delay_max_seconds = float(
            os.getenv("SEND_DELAY_MAX_SECONDS", str(legacy_delay * 3))
        )
        if send_delay_max_seconds < send_delay_min_seconds:
            raise RuntimeError("SEND_DELAY_MAX_SECONDS must be >= SEND_DELAY_MIN_SECONDS")

        return cls(
            api_id=api_id,
            api_hash=api_hash,
            session_name=os.getenv("SESSION_NAME", "userbot_session"),
            session_string=os.getenv("SESSION_STRING", "").strip() or None,
            source_channels=cls._parse_chat_list(source_raw),
            target_chat=cls._parse_chat_ref(target_raw),
            delay_minutes=int(os.getenv("DELAY_MINUTES", "0")),
            db_path=os.getenv("DB_PATH", "relay_queue.db"),
            poll_interval_seconds=int(os.getenv("POLL_INTERVAL_SECONDS", "5")),
            album_wait_seconds=int(os.getenv("ALBUM_WAIT_SECONDS", "5")),
            send_delay_min_seconds=send_delay_min_seconds,
            send_delay_max_seconds=send_delay_max_seconds,
            relay_mode=relay_mode,
            connection_retries=int(os.getenv("CONNECTION_RETRIES", "10")),
            retry_delay_seconds=int(os.getenv("RETRY_DELAY_SECONDS", "5")),
            fetch_batch_limit=int(os.getenv("FETCH_BATCH_LIMIT", "200")),
            health_check_port=(
                int(os.environ["PORT"]) if os.getenv("PORT", "").strip().isdigit() else None
            ),
            max_delivery_retries=int(os.getenv("MAX_DELIVERY_RETRIES", "3")),
            journal_mode=journal_mode,
        )


# ==============================================================================
# PERSISTENCE LAYER
# ==============================================================================
class QueueDatabase:
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS message_queue (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        message_id    INTEGER NOT NULL,
        channel_id    INTEGER NOT NULL,
        grouped_id    INTEGER,
        status        TEXT NOT NULL DEFAULT 'pending',
        retry_count   INTEGER NOT NULL DEFAULT 0,
        dispatch_time TEXT NOT NULL,
        created_at    TEXT NOT NULL,
        UNIQUE(message_id, channel_id)
    );
    CREATE INDEX IF NOT EXISTS idx_status_dispatch ON message_queue(status, dispatch_time);
    CREATE INDEX IF NOT EXISTS idx_grouped ON message_queue(channel_id, grouped_id);
    """

    def __init__(self, db_path: str, journal_mode: str = "WAL") -> None:
        self._db_path = db_path
        self._journal_mode = journal_mode
        self._conn: Optional[aiosqlite.Connection] = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row

        await self._conn.execute(f"PRAGMA journal_mode={self._journal_mode};")
        await self._conn.execute("PRAGMA synchronous=NORMAL;")
        await self._conn.execute("PRAGMA busy_timeout=5000;")
        await self._conn.executescript(self.SCHEMA)
        await self._conn.commit()
        logger.info(
            "Database connected at '%s' (journal_mode=%s).", self._db_path, self._journal_mode
        )

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            logger.info("Database connection closed cleanly.")

    async def enqueue(
        self,
        message_id: int,
        channel_id: int,
        grouped_id: Optional[int],
        dispatch_time: datetime,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        assert self._conn is not None
        async with self._lock:
            try:
                await self._conn.execute(
                    """INSERT OR IGNORE INTO message_queue
                       (message_id, channel_id, grouped_id, status, retry_count, dispatch_time, created_at)
                       VALUES (?, ?, ?, 'pending', 0, ?, ?)""",
                    (message_id, channel_id, grouped_id, dispatch_time.isoformat(), now),
                )
                await self._conn.commit()
            except Exception:
                logger.exception(
                    "Failed to enqueue message_id=%s channel_id=%s", message_id, channel_id
                )

    async def fetch_due(self, now: datetime, limit: int = 200) -> List[aiosqlite.Row]:
        assert self._conn is not None
        cursor = await self._conn.execute(
            """SELECT * FROM message_queue
               WHERE status = 'pending' AND dispatch_time <= ?
               ORDER BY created_at ASC
               LIMIT ?""",
            (now.isoformat(), limit),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return rows

    async def group_last_created_at(self, channel_id: int, grouped_id: int) -> Optional[datetime]:
        assert self._conn is not None
        cursor = await self._conn.execute(
            """SELECT MAX(created_at) FROM message_queue
               WHERE channel_id = ? AND grouped_id = ?""",
            (channel_id, grouped_id),
        )
        row = await cursor.fetchone()
        await cursor.close()
        if row and row[0]:
            return datetime.fromisoformat(row[0])
        return None

    async def mark_processed(self, row_ids: Sequence[int]) -> None:
        if not row_ids:
            return
        assert self._conn is not None
        placeholders = ",".join("?" for _ in row_ids)
        async with self._lock:
            await self._conn.execute(
                f"DELETE FROM message_queue WHERE id IN ({placeholders})",
                tuple(row_ids),
            )
            await self._conn.commit()

    async def bump_retry(self, row_ids: Sequence[int], max_retries: int) -> List[int]:
        if not row_ids:
            return []
        assert self._conn is not None
        newly_failed: List[int] = []
        async with self._lock:
            placeholders = ",".join("?" for _ in row_ids)
            retry_delay_time = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
            await self._conn.execute(
                f"""UPDATE message_queue
                    SET retry_count = retry_count + 1,
                        dispatch_time = ?
                    WHERE id IN ({placeholders})""",
                (retry_delay_time,) + tuple(row_ids),
            )
            cursor = await self._conn.execute(
                f"""SELECT id FROM message_queue
                    WHERE id IN ({placeholders}) AND retry_count >= ?""",
                tuple(row_ids) + (max_retries,),
            )
            failed_rows = await cursor.fetchall()
            await cursor.close()
            newly_failed = [r["id"] for r in failed_rows]
            if newly_failed:
                fail_placeholders = ",".join("?" for _ in newly_failed)
                await self._conn.execute(
                    f"""UPDATE message_queue SET status = 'failed'
                        WHERE id IN ({fail_placeholders})""",
                    tuple(newly_failed),
                )
            await self._conn.commit()
        return newly_failed


# ==============================================================================
# INGESTION LAYER
# ==============================================================================
class IngestionService:
    def __init__(self, client: TelegramClient, db: QueueDatabase, settings: Settings) -> None:
        self._client = client
        self._db = db
        self._settings = settings

    def register(self) -> None:
        self._client.add_event_handler(
            self._on_new_message, events.NewMessage(chats=self._settings.source_channels)
        )
        logger.info(
            "Ingestion listener registered for source channels: %s",
            self._settings.source_channels,
        )

    async def _on_new_message(self, event: events.NewMessage.Event) -> None:
        message: Message = event.message
        channel_id: int = event.chat_id
        dispatch_time = datetime.now(timezone.utc) + timedelta(minutes=self._settings.delay_minutes)

        await self._db.enqueue(
            message_id=message.id,
            channel_id=channel_id,
            grouped_id=message.grouped_id,
            dispatch_time=dispatch_time,
        )
        logger.info(
            "Queued message id=%s channel=%s grouped_id=%s -> dispatch_time=%s",
            message.id,
            channel_id,
            message.grouped_id,
            dispatch_time.isoformat(),
        )


# ==============================================================================
# DISPATCHER LAYER
# ==============================================================================
class DispatcherService:
    def __init__(self, client: TelegramClient, db: QueueDatabase, settings: Settings) -> None:
        self._client = client
        self._db = db
        self._settings = settings
        self._stop_event = asyncio.Event()
        self._flood_wait_until: Optional[datetime] = None

    def stop(self) -> None:
        self._stop_event.set()

    async def run(self) -> None:
        logger.info(
            "Dispatcher worker started (poll_interval=%ss).", self._settings.poll_interval_seconds
        )
        while not self._stop_event.is_set():
            try:
                if self._flood_wait_until and datetime.now(timezone.utc) < self._flood_wait_until:
                    await asyncio.sleep(1)
                    continue
                # Widened from 12x to 60x poll_interval: a timeout that fires
                # WHILE a send_file()/send_message() call is in flight but
                # AFTER Telegram has already accepted the upload can cancel
                # the coroutine before it reaches mark_processed(), leaving
                # the row 'pending' and causing a duplicate re-send next
                # cycle. The safety timeout itself stays in place (a truly
                # stuck/hung network call must still be able to time out and
                # free the dispatcher loop) -- only the budget is enlarged so
                # normal large-media uploads have realistic headroom.
                await asyncio.wait_for(self._process_cycle(), timeout=self._settings.poll_interval_seconds * 60)
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                logger.error(
                    "Dispatcher cycle exceeded its time budget and was aborted "
                    "(stalled network call?); will retry next cycle."
                )
            except Exception:
                logger.exception("Unhandled error in dispatcher cycle; will retry next cycle.")

            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._settings.poll_interval_seconds
                )
            except asyncio.TimeoutError:
                pass

        logger.info("Dispatcher worker stopped.")

    async def _process_cycle(self) -> None:
        now = datetime.now(timezone.utc)
        rows = await self._db.fetch_due(now, limit=self._settings.fetch_batch_limit)
        if not rows:
            return

        standalone: List[aiosqlite.Row] = []
        groups: Dict[Tuple[int, int], List[aiosqlite.Row]] = {}
        for row in rows:
            if row["grouped_id"] is None:
                standalone.append(row)
            else:
                groups.setdefault((row["channel_id"], row["grouped_id"]), []).append(row)

        for row in standalone:
            await self._dispatch_single(row)

        for (channel_id, grouped_id), group_rows in groups.items():
            if self._settings.delay_minutes == 0:
                last_created = await self._db.group_last_created_at(channel_id, grouped_id)
                if last_created is not None:
                    elapsed = (now - last_created).total_seconds()
                    if elapsed < self._settings.album_wait_seconds:
                        logger.info(
                            "Album grouped_id=%s (channel=%s) still settling "
                            "(%.1fs elapsed < %ss required); deferring.",
                            grouped_id,
                            channel_id,
                            elapsed,
                            self._settings.album_wait_seconds,
                        )
                        continue
            await self._dispatch_group(channel_id, grouped_id, group_rows)

    async def _fail_or_retry(self, row_ids: Sequence[int], reason: str) -> None:
        newly_failed = await self._db.bump_retry(row_ids, self._settings.max_delivery_retries)
        if newly_failed:
            logger.error(
                "Row(s) %s permanently failed after %s retries (%s); "
                "moved to DLQ status='failed' and will no longer block the queue.",
                newly_failed,
                self._settings.max_delivery_retries,
                reason,
            )

    async def _dispatch_single(self, row: aiosqlite.Row) -> None:
        channel_id = row["channel_id"]
        message_id = row["message_id"]

        try:
            message: Optional[Message] = await self._client.get_messages(channel_id, ids=message_id)
        except FloodWaitError as exc:
            await self._handle_flood_wait(exc)
            return
        except RPCError:
            logger.exception("RPC error fetching message id=%s channel=%s", message_id, channel_id)
            await self._fail_or_retry([row["id"]], "RPCError while fetching")
            return

        if message is None:
            logger.warning(
                "Message id=%s channel=%s no longer exists (deleted upstream). Marking processed.",
                message_id,
                channel_id,
            )
            await self._db.mark_processed([row["id"]])
            return

        try:
            if self._settings.relay_mode == "forward":
                await self._forward_single(channel_id, message)
            else:
                await self._send_copy(message)
        except FloodWaitError as exc:
            await self._handle_flood_wait(exc)
            return
        except Exception:
            logger.exception(
                "Failed to send message id=%s channel=%s; will retry next cycle.",
                message_id,
                channel_id,
            )
            await self._fail_or_retry([row["id"]], "send error")
            return

        await self._db.mark_processed([row["id"]])
        logger.info("Dispatched message id=%s channel=%s -> target.", message_id, channel_id)
        await asyncio.sleep(
            random.uniform(self._settings.send_delay_min_seconds, self._settings.send_delay_max_seconds)
        )

    async def _dispatch_group(
        self, channel_id: int, grouped_id: int, group_rows: List[aiosqlite.Row]
    ) -> None:
        message_ids = [r["message_id"] for r in group_rows]
        row_ids = [r["id"] for r in group_rows]

        try:
            fetched: Any = await self._client.get_messages(channel_id, ids=message_ids)
        except FloodWaitError as exc:
            await self._handle_flood_wait(exc)
            return
        except RPCError:
            logger.exception("RPC error fetching album grouped_id=%s channel=%s", grouped_id, channel_id)
            await self._fail_or_retry(row_ids, "RPCError while fetching album")
            return

        messages: List[Optional[Message]] = fetched if isinstance(fetched, list) else [fetched]
        live_messages: List[Message] = [m for m in messages if m is not None]
        live_messages.sort(key=lambda m: m.id)

        if not live_messages:
            logger.warning(
                "Entire album grouped_id=%s channel=%s was deleted upstream. Marking processed.",
                grouped_id,
                channel_id,
            )
            await self._db.mark_processed(row_ids)
            return

        try:
            if self._settings.relay_mode == "forward":
                await self._forward_group(channel_id, live_messages)
            else:
                await self._send_album_copy(live_messages)
        except FloodWaitError as exc:
            await self._handle_flood_wait(exc)
            return
        except Exception:
            logger.exception(
                "Failed to send album grouped_id=%s channel=%s; will retry next cycle.",
                grouped_id,
                channel_id,
            )
            await self._fail_or_retry(row_ids, "album send error")
            return

        await self._db.mark_processed(row_ids)
        logger.info(
            "Dispatched album grouped_id=%s (%d live parts) channel=%s -> target.",
            grouped_id,
            len(live_messages),
            channel_id,
        )
        await asyncio.sleep(
            random.uniform(self._settings.send_delay_min_seconds, self._settings.send_delay_max_seconds)
        )

    async def _forward_single(self, channel_id: int, message: Message) -> None:
        target = self._settings.target_chat
        await self._client.forward_messages(target, message, from_peer=channel_id)

    async def _forward_group(self, channel_id: int, messages: List[Message]) -> None:
        target = self._settings.target_chat
        await self._client.forward_messages(target, messages, from_peer=channel_id)

    async def _send_copy(self, message: Message) -> None:
        target = self._settings.target_chat
        text = message.raw_text or ""
        entities = message.entities

        if message.media:
            await self._client.send_file(
                target,
                file=message.media,
                caption=text,
                formatting_entities=entities,
                parse_mode=None,
            )
        else:
            if text.strip():
                await self._client.send_message(
                    target, text, formatting_entities=entities, parse_mode=None
                )
            else:
                logger.info("Skipping empty text/no-media message id=%s.", message.id)

    async def _send_album_copy(self, messages: List[Message]) -> None:
        target = self._settings.target_chat
        media_messages = [m for m in messages if m.media]
        caption_source = next((m for m in messages if m.raw_text), None)
        caption = caption_source.raw_text if caption_source else ""
        entities = caption_source.entities if caption_source else None

        if media_messages:
            await self._client.send_file(
                target,
                file=media_messages,
                caption=caption,
                formatting_entities=entities,
                parse_mode=None,
            )
        elif caption.strip():
            await self._client.send_message(
                target, caption, formatting_entities=entities, parse_mode=None
            )

    async def _handle_flood_wait(self, error: FloodWaitError) -> None:
        wait_seconds = error.seconds + 2
        self._flood_wait_until = datetime.now(timezone.utc) + timedelta(seconds=wait_seconds)
        logger.warning(
            "FloodWaitError hit. Pausing DISPATCHER for %ss (ingestion keeps listening).",
            wait_seconds,
        )


# ==============================================================================
# APPLICATION ORCHESTRATION
# ==============================================================================
class RelayBotApp:
    def __init__(self) -> None:
        self._settings = Settings.from_env()
        self._db = QueueDatabase(self._settings.db_path, journal_mode=self._settings.journal_mode)

        self._using_string_session = self._settings.session_string is not None
        if not self._using_string_session:
            # On a PaaS container there is no interactive stdin. If
            # SESSION_STRING isn't set, client.start() below would silently
            # block forever waiting for a phone/code prompt that can never
            # arrive -- the process never crashes, it just hangs, which looks
            # like a stuck "building/deploying" service and can trick the
            # platform into leaving old, still-running instances alive
            # alongside the hung one (multiple userbot sessions relaying the
            # same channels => duplicate messages). Failing fast here turns
            # that silent hang into an immediate, visible, restart-supervised
            # error instead.
            raise RuntimeError(
                "SESSION_STRING is not set. This service must never fall back "
                "to interactive login because there is no stdin available on "
                "a PaaS deployment. Generate a StringSession locally and set "
                "it as the SESSION_STRING environment variable."
            )
        session: Union[str, StringSession] = StringSession(self._settings.session_string)

        self._client = TelegramClient(
            session,
            self._settings.api_id,
            self._settings.api_hash,
            connection_retries=self._settings.connection_retries,
            retry_delay=self._settings.retry_delay_seconds,
            auto_reconnect=True,
            request_retries=5,
            flood_sleep_threshold=0,
        )
        self._ingestion = IngestionService(self._client, self._db, self._settings)
        self._dispatcher = DispatcherService(self._client, self._db, self._settings)
        self._dispatcher_task: Optional[asyncio.Task] = None
        self._shutdown_event = asyncio.Event()

    async def start(self) -> None:
        asyncio.get_running_loop().set_exception_handler(_log_unhandled_exception)

        await self._db.connect()
        await self._client.start()

        me = await self._client.get_me()
        logger.info("Logged in as %s (id=%s).", getattr(me, "username", None) or "unknown", me.id)

        self._ingestion.register()
        self._dispatcher_task = asyncio.create_task(self._dispatcher.run(), name="dispatcher-worker")

        self._install_signal_handlers()
        await self._start_health_check_server()

        logger.info("Userbot is now running. Press Ctrl+C to stop gracefully.")
        await self._shutdown_event.wait()
        await self._shutdown()

    async def _start_health_check_server(self) -> None:
        port = self._settings.health_check_port
        if port is None:
            return

        async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                await reader.read(1024)
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK")
                await writer.drain()
            except Exception:
                pass
            finally:
                writer.close()

        server = await asyncio.start_server(_handle, host="0.0.0.0", port=port)
        logger.info("Health-check responder listening on 0.0.0.0:%s.", port)
        asyncio.create_task(server.serve_forever(), name="health-check-server")

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, lambda s=sig: self._request_shutdown(s))
            except NotImplementedError:
                signal.signal(sig, lambda *_args, s=sig: self._request_shutdown(s))

    def _request_shutdown(self, sig: signal.Signals) -> None:
        sig_name = getattr(sig, "name", str(sig))
        logger.info("Received signal %s. Initiating graceful shutdown...", sig_name)
        self._shutdown_event.set()

    async def _shutdown(self) -> None:
        logger.info("Shutdown sequence started.")

        self._dispatcher.stop()
        if self._dispatcher_task:
            try:
                await asyncio.wait_for(self._dispatcher_task, timeout=15)
            except asyncio.TimeoutError:
                logger.warning("Dispatcher task did not stop in time; cancelling.")
                self._dispatcher_task.cancel()

        await self._client.disconnect()
        logger.info("Telegram client disconnected.")

        await self._db.close()
        logger.info("Shutdown complete. Goodbye.")


async def main() -> None:
    max_backoff = 300
    backoff = 5
    while True:
        started_at = datetime.now(timezone.utc)
        try:
            # RelayBotApp() is constructed *inside* this try block on purpose:
            # __init__ can raise (e.g. missing API_ID/API_HASH, or the new
            # fail-fast RuntimeError for a missing SESSION_STRING). If
            # construction happened outside the try, that exception would
            # escape main() entirely and crash the whole process instead of
            # being logged and retried through the backoff loop below --
            # defeating the very purpose of this loop.
            app = RelayBotApp()
            await app.start()
            break
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Fatal error during startup/runtime; will attempt restart.")
            if (datetime.now(timezone.utc) - started_at).total_seconds() > 300:
                backoff = 5
            logger.warning("Restarting in %ss...", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user (KeyboardInterrupt).")
