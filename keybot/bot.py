import asyncio
import logging
from contextlib import suppress
from datetime import datetime

import discord
from discord import app_commands

from .client import KST, OWNER_ID, LOG_CHANNEL_ID, LicenseError
from .audit import relay_once


def grant_label(grant):
    kind, amount = grant["kind"], grant["amount"]
    return {"uses": f"실행 {amount}회", "days": f"{amount}일", "month": "1달",
            "year": "1년", "forever": "영구"}[kind]


def ticket_overwrites(guild, owner, user, bot):
    private = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
    return {guild.default_role: discord.PermissionOverwrite(view_channel=False),
            owner: private, user: private, bot: private}


async def reply_error(interaction, error):
    text = str(error) if isinstance(error, LicenseError) else "처리하지 못했습니다. 봇 권한과 DM 허용 설정을 확인하세요."
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)


class GrantSelect(discord.ui.Select):
    def __init__(self, bot, user_id, grants):
        super().__init__(placeholder="발급할 키의 이용권을 선택하세요", options=[
            discord.SelectOption(label=f"{grant_label(g)} · 발급 가능 {g['remaining']}개", value=str(g["id"]))
            for g in grants])
        self.bot, self.user_id = bot, user_id

    async def callback(self, interaction):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("본인의 발급권만 사용할 수 있습니다.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        await self.bot.deliver(interaction, interaction.user, int(self.values[0]))


class IssuePanel(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="키 발급받기 · DM", style=discord.ButtonStyle.primary, custom_id="padlet:issue:v1")
    async def issue(self, interaction, button):
        await interaction.response.defer(ephemeral=True)
        if interaction.guild_id != self.bot.guild_id:
            return await interaction.followup.send('설정된 서버에서 사용하세요.', ephemeral=True)
        try:
            grants = await asyncio.to_thread(self.bot.store.available, interaction.user.id)
        except Exception as error:
            return await reply_error(interaction, error)
        if not grants:
            return await interaction.followup.send("관리자가 지급한 발급권이 없습니다. 문의 티켓을 열어주세요.", ephemeral=True)
        if len(grants) == 1:
            return await self.bot.deliver(interaction, interaction.user, grants[0]["id"])
        view = discord.ui.View(timeout=120)
        view.add_item(GrantSelect(self.bot, interaction.user.id, grants))
        await interaction.followup.send("이용권을 선택하면 키를 DM으로 보냅니다. 목록은 한 번에 25개까지 표시됩니다.", view=view, ephemeral=True)


class TicketPanel(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="비공개 문의 티켓 열기", style=discord.ButtonStyle.primary, custom_id="padlet:ticket:v1")
    async def ticket(self, interaction, button):
        await interaction.response.defer(ephemeral=True)
        try:
            guild = interaction.guild
            if not guild or guild.id != self.bot.guild_id:
                raise LicenseError("설정된 서버에서 사용하세요.")
            async with self.bot.ticket_lock:
                channel_id = await asyncio.to_thread(self.bot.store.ticket, guild.id, interaction.user.id)
                channel = guild.get_channel(channel_id) if channel_id else None
                if channel_id and channel is None:
                    try:
                        channel = await guild.fetch_channel(channel_id)
                    except discord.NotFound:
                        pass
                if channel is None:
                    owner = guild.get_member(OWNER_ID) or await guild.fetch_member(OWNER_ID)
                    channel = await guild.create_text_channel(
                        name=f"ticket-{interaction.user.id}",
                        category=interaction.channel.category if isinstance(interaction.channel, discord.TextChannel) else None,
                        overwrites=ticket_overwrites(guild, owner, interaction.user, guild.me),
                        reason="User requested private key support")
                    await asyncio.to_thread(self.bot.store.ticket, guild.id, interaction.user.id, channel.id)
                    await channel.send(f"{interaction.user.mention} 문의 내용을 남겨주세요. <@{OWNER_ID}>가 확인합니다.",
                                       allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False))
            await interaction.followup.send(f"문의 채널: {channel.mention}", ephemeral=True)
        except Exception as error:
            await reply_error(interaction, error)


class Bot(discord.Client):
    def __init__(self, store, bridge, guild_id):
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(intents=intents, max_messages=None, allowed_mentions=discord.AllowedMentions.none())
        self.store, self.bridge, self.guild_id = store, bridge, guild_id
        self.tree = app_commands.CommandTree(self)
        self.ticket_lock = asyncio.Lock()
        self.delivery_lock = asyncio.Lock()
        self.logs_configured = False
        self.audit_task = None
        self.audit_signal = asyncio.Event()
        self.register_commands()

    async def setup_hook(self):
        self.add_view(TicketPanel(self))
        self.add_view(IssuePanel(self))
        guild = discord.Object(id=self.guild_id)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)

    async def on_ready(self):
        logging.getLogger(__name__).info("Discord bot ready")
        if not self.logs_configured:
            try:
                await self.configure_logs()
            except Exception as error:
                logging.getLogger(__name__).warning('로그 연결 실패 (%s). 지정 채널의 웹후크 관리 권한 확인 후 /로그설정', type(error).__name__)
        if self.audit_task is None or self.audit_task.done():
            self.audit_task = asyncio.create_task(self.audit_loop())

    async def audit_loop(self):
        while not self.is_closed():
            self.audit_signal.clear()
            try:
                channel = self.get_channel(LOG_CHANNEL_ID) or await self.fetch_channel(LOG_CHANNEL_ID)
                if not isinstance(channel, discord.TextChannel) or channel.guild.id != self.guild_id:
                    raise LicenseError('설정된 로그 채널이 아닙니다.')
                if await relay_once(self.store, channel):
                    await asyncio.sleep(1)
                    continue
            except Exception as error:
                logging.getLogger(__name__).warning('로그 재전송 대기 (%s)', type(error).__name__)
                await asyncio.sleep(30)
                continue
            try:
                await asyncio.wait_for(self.audit_signal.wait(), timeout=30)
            except TimeoutError:
                pass

    async def close(self):
        if self.audit_task:
            self.audit_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.audit_task
        await super().close()

    async def configure_logs(self):
        channel = self.get_channel(LOG_CHANNEL_ID) or await self.fetch_channel(LOG_CHANNEL_ID)
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != self.guild_id:
            raise LicenseError('지정한 로그 채널이 설정된 서버에 없습니다.')
        hooks = await channel.webhooks()
        webhook = next((h for h in hooks if h.user and h.user.id == self.user.id and h.name == 'Padlet 이용권 로그'), None)
        if webhook is None:
            webhook = await channel.create_webhook(name='Padlet 이용권 로그', reason='Requested license issuance and usage audit')
        await asyncio.to_thread(self.store.call, 'configure_log', channel=LOG_CHANNEL_ID, webhook=webhook.url)
        self.logs_configured = True
        logging.getLogger(__name__).info('키 서버 및 로그 채널 연결 완료')

    async def deliver(self, interaction, user, grant_id):
        # Serialize DM delivery; DB transactions also protect duplicate button interactions.
        async with self.delivery_lock:
            license = None
            try:
                license = await asyncio.to_thread(self.store.prepare, user.id, grant_id)
                if license["remaining"] is not None:
                    condition = f"실행 버튼 접수 {license['remaining']}회"
                elif license["expires"]:
                    condition = datetime.fromtimestamp(license["expires"], KST).strftime("%Y-%m-%d %H:%M:%S KST까지")
                else:
                    condition = "영구"
                await user.send(f"Padlet 이용 키: **`{license['key']}`**\n이용 조건: {condition}\n"
                                f"발급 번호: `{license['id']}`\n"
                                "웹사이트에 접속한 뒤 키 입력 창에서 인증하세요.\n"
                                "키를 아는 사람은 이용할 수 있으므로 타인에게 공유하지 마세요.")
                await asyncio.to_thread(self.store.delivered, license["id"], user.id)
                await interaction.followup.send("키를 대상자의 DM으로 보냈습니다.", ephemeral=True)
            except Exception as error:
                if license:
                    try:
                        await asyncio.to_thread(self.store.delivery_failed, license["id"], user.id)
                    except Exception:
                        pass  # The server recovers unconfirmed deliveries after ten minutes.
                await reply_error(interaction, error)
            finally:
                self.audit_signal.set()

    def register_commands(self):
        async def owner(interaction):
            self.store.require_owner(interaction.user.id)
            if interaction.guild_id != self.guild_id:
                raise LicenseError("설정된 서버에서 사용하세요.")
            return True

        @self.tree.error
        async def error_handler(interaction, error):
            await reply_error(interaction, getattr(error, "original", error))

        @self.tree.command(name="문의패널", description="관리자: 비공개 문의 티켓 패널 생성")
        @app_commands.check(owner)
        async def ticket_panel(interaction: discord.Interaction):
            await interaction.response.send_message("키 이용 문의는 아래 버튼으로 비공개 티켓을 열어주세요.", view=TicketPanel(self))

        @self.tree.command(name="발급패널", description="관리자: 발급권으로 키를 받는 패널 생성")
        @app_commands.check(owner)
        async def issue_panel(interaction: discord.Interaction):
            await interaction.response.send_message("관리자에게 받은 발급권으로 키를 DM으로 받을 수 있습니다.", view=IssuePanel(self))

        async def grant(interaction, user, kind, amount, quantity):
            await interaction.response.defer(ephemeral=True)
            await asyncio.to_thread(self.store.grant, interaction.user.id, user.id, kind, amount, quantity)
            self.audit_signal.set()
            await interaction.followup.send(f"{user.mention}에게 {grant_label({'kind': kind, 'amount': amount})} 키 발급권 {quantity}개 지급 완료.", ephemeral=True)

        @self.tree.command(name="다회용티켓지급", description="관리자: 실행 횟수가 정해진 키 발급권 지급")
        @app_commands.check(owner)
        async def uses(interaction: discord.Interaction, 대상: discord.Member,
                       횟수: app_commands.Range[int, 1, 36500], 수량: app_commands.Range[int, 1, 100] = 1):
            await grant(interaction, 대상, "uses", 횟수, 수량)

        @self.tree.command(name="시간제티켓지급", description="관리자: 하루 단위 키 발급권 지급 (발급 시점부터 KST)")
        @app_commands.check(owner)
        async def days(interaction: discord.Interaction, 대상: discord.Member,
                       일수: app_commands.Range[int, 1, 36500], 수량: app_commands.Range[int, 1, 100] = 1):
            await grant(interaction, 대상, "days", 일수, 수량)

        periods = [app_commands.Choice(name=n, value=v) for n, v in
                   [("1일", "1"), ("3일", "3"), ("1주일", "7"), ("1달", "month"), ("1년", "year"), ("영구", "forever")]]

        @self.tree.command(name="기간티켓지급", description="관리자: 1일·3일·1주일·1달·1년·영구 발급권 지급")
        @app_commands.check(owner)
        @app_commands.choices(기간=periods)
        async def period(interaction: discord.Interaction, 대상: discord.Member, 기간: app_commands.Choice[str],
                         수량: app_commands.Range[int, 1, 100] = 1):
            value = 기간.value
            await grant(interaction, 대상, "days" if value.isdigit() else value, int(value) if value.isdigit() else 1, 수량)

        @self.tree.command(name="키폐기", description="관리자: DM에 있는 발급 번호로 키 폐기")
        @app_commands.check(owner)
        async def revoke(interaction: discord.Interaction, 발급번호: str):
            await interaction.response.defer(ephemeral=True)
            count = await asyncio.to_thread(self.store.revoke, interaction.user.id, 발급번호)
            await interaction.followup.send("키를 폐기했습니다." if count else "활성 키를 찾지 못했습니다.", ephemeral=True)

        @self.tree.command(name="발급권회수", description="관리자: 아직 사용하지 않은 발급권 모두 회수")
        @app_commands.check(owner)
        async def revoke_grants(interaction: discord.Interaction, 대상: discord.Member):
            await interaction.response.defer(ephemeral=True)
            await asyncio.to_thread(self.store.revoke_grants, interaction.user.id, 대상.id)
            await interaction.followup.send("미사용 발급권을 회수했습니다. 이미 발급한 키는 별도로 폐기하세요.", ephemeral=True)

        @self.tree.command(name='로그설정', description='관리자: 지정 로그 채널의 서버 웹훅 연결 또는 복구')
        @app_commands.check(owner)
        async def configure_log(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            await self.configure_logs()
            await interaction.followup.send(f'<#{LOG_CHANNEL_ID}> 로그 연결 완료. 봇 종료 후에도 사용 로그를 기록합니다.', ephemeral=True)
