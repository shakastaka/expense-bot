"""Render HTTP entry point. Telegram updates are committed before acknowledgement."""
import asyncio
import copy
import hmac
import json
import logging
import os
import re
from contextvars import ContextVar

from aiohttp import web
from telegram import Update
import bot
from database import transaction

failure = ContextVar('handler_failure', default=None)

async def handle_error(update, context):
    # Never log Telegram URLs, tokens or statement contents.
    failure.set(context.error)
    logging.error('Telegram handler failed: %s', type(context.error).__name__)

class Webhook:
    def __init__(self, application, secret):
        self.application = application
        self.secret = secret
        self.lock = asyncio.Lock()
        self.ready = False

    async def health(self, request):
        return web.Response(text='ok' if self.ready else 'starting', status=200 if self.ready else 503)

    async def receive(self, request):
        supplied = request.headers.get('X-Telegram-Bot-Api-Secret-Token', '')
        if not hmac.compare_digest(supplied, self.secret):
            raise web.HTTPForbidden()
        try:
            payload = await request.json()
            if not isinstance(payload, dict) or type(payload.get('update_id')) is not int:
                raise ValueError()
            update = Update.de_json(payload, self.application.bot)
        except (ValueError, TypeError, KeyError):
            raise web.HTTPBadRequest()
        async with self.lock:
            user_id = update.effective_user.id if update.effective_user else None
            state = self.application.user_data[user_id] if user_id is not None else None
            before = copy.deepcopy(state)
            error_token = failure.set(None)
            try:
                with transaction() as c:
                    # Cross-process serialization also covers rolling deployments.
                    c.raw.execute('SELECT pg_advisory_xact_lock(748195320)')
                    if c.execute('SELECT 1 FROM processed_updates WHERE update_id=?', (update.update_id,)).fetchone():
                        return web.Response(text='ok')
                    if state is not None:
                        row = c.execute('SELECT state FROM bot_user_state WHERE user_id=?', (user_id,)).fetchone()
                        state.clear()
                        if row: state.update(json.loads(row['state']))
                    await self.application.process_update(update)
                    if failure.get() is not None: raise failure.get()
                    if state is not None:
                        c.execute('INSERT INTO bot_user_state(user_id,state) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET state=EXCLUDED.state', (user_id, json.dumps(state, ensure_ascii=False)))
                    c.execute('INSERT INTO processed_updates(update_id) VALUES(?)', (update.update_id,))
                return web.Response(text='ok')
            except Exception as error:
                if state is not None:
                    state.clear()
                    state.update(before)
                logging.error('Webhook request failed: %s', type(error).__name__)
                raise web.HTTPServiceUnavailable()
            finally:
                failure.reset(error_token)


def create_server():
    base = (os.getenv('WEBHOOK_BASE_URL') or os.getenv('RENDER_EXTERNAL_URL') or '').rstrip('/')
    secret = os.getenv('WEBHOOK_SECRET', '')
    if not os.getenv('DATABASE_URL'):
        raise RuntimeError('Set DATABASE_URL to the Neon PostgreSQL connection string')
    if not base.startswith('https://'):
        raise RuntimeError('Set WEBHOOK_BASE_URL or use Render RENDER_EXTERNAL_URL')
    if not re.fullmatch(r'[A-Za-z0-9_-]{32,256}', secret):
        raise RuntimeError('WEBHOOK_SECRET must contain 32-256 letters, digits, underscores or hyphens')
    application = bot.build_application(webhook=True)
    application.add_error_handler(handle_error)
    endpoint = Webhook(application, secret)
    server = web.Application(client_max_size=4 * 1024 * 1024)
    server.router.add_get('/', endpoint.health)
    server.router.add_get('/health', endpoint.health)
    server.router.add_post('/telegram', endpoint.receive)

    async def lifecycle(server):
        async with application:
            await application.start()
            try:
                await bot.setup_commands(application)
                await application.bot.set_webhook(
                    url=base + '/telegram', secret_token=secret,
                    allowed_updates=['message', 'callback_query'], max_connections=1,
                )
                endpoint.ready = True
                yield
            finally:
                endpoint.ready = False
                await application.stop()
    server.cleanup_ctx.append(lifecycle)
    return server

if __name__ == '__main__':
    web.run_app(create_server(), host='0.0.0.0', port=int(os.getenv('PORT', '10000')), access_log=None)
