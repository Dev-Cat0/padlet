"""Drain the same leased outbox through the local bot's normal Discord connection."""
import asyncio
import discord


async def relay_once(store, channel):
    event = (await asyncio.to_thread(store.call, 'audit_next'))['event']
    if event is None:
        return False
    try:
        await channel.send(event['content'], allowed_mentions=discord.AllowedMentions.none())
    except Exception:
        await asyncio.to_thread(store.call, 'audit_finish', event_id=event['id'], delivered=False)
        raise
    await asyncio.to_thread(store.call, 'audit_finish', event_id=event['id'], delivered=True)
    return True
