import asyncio
import glob
import inspect
import json
import os
import shlex
import sys
import subprocess
import time
import traceback
from collections import defaultdict
from datetime import timedelta
from functools import wraps
from io import BytesIO
from random import choice, shuffle
from textwrap import dedent

import aiofiles
import aiohttp
from aiohttp import web
import discord
from apscheduler import events
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from discord import utils
from discord.enums import ChannelType, Status
from discord.ext.commands.bot import _get_variable
from discord.object import Object
from discord.voice_client import VoiceClient

from musicbot.config import Config, ConfigDefaults
from musicbot.local_song import sort_songs
from musicbot.permissions import Permissions, PermissionsDefaults
from musicbot.player import MusicPlayer
from musicbot.playlist import Playlist
from musicbot.utils import load_file, write_file, sane_round_int, paginate, slugify, get_next
from . import downloader
from . import exceptions
from .constants import DISCORD_MSG_CHAR_LIMIT
from .constants import VERSION as BOTVERSION
from .opus_loader import load_opus_lib
from musicbot.db import init_db, Server, User, PermissionsGroup

load_opus_lib()


class SkipState:
    def __init__(self):
        self.skippers = set()
        self.skip_msgs = set()

    @property
    def skip_count(self):
        return len(self.skippers)

    def reset(self):
        self.skippers.clear()
        self.skip_msgs.clear()

    def add_skipper(self, skipper, msg):
        self.skippers.add(skipper)
        self.skip_msgs.add(msg)
        return self.skip_count


class Response:
    def __init__(self, content="", reply=False, delete_after=0, embed=None):
        self.content = content
        self.embed = embed
        self.reply = reply
        self.delete_after = delete_after


class MusicBot(discord.Client):
    cycle_length = 100
    sleep_time = 0.1
    host = "127.0.0.1"
    port = 5002

    def __init__(self, config_file=ConfigDefaults.options_file, perms_file=PermissionsDefaults.perms_file, agreelist_file=ConfigDefaults.agreelist_file):
        MusicBot.bot = self
        self.players = {}
        self.the_voice_clients = {}
        self.locks = defaultdict(asyncio.Lock)
        self.voice_client_connect_lock = asyncio.Lock()
        self.voice_client_move_lock = asyncio.Lock()

        self.config = Config(config_file)
        self.permissions = Permissions(perms_file, grant_all=[])
        self.agreelist_file = agreelist_file
        with open(agreelist_file) as agreelist_f:
            self.agree_list = set(json.load(agreelist_f))

        self.blacklist = set(load_file(self.config.blacklist_file))
        self.default_autoplaylist = load_file(self.config.auto_playlist_file)
        self.downloader = downloader.Downloader(download_folder='audio_cache')

        self.exit_signal = None
        self.init_ok = False
        self.cached_client_id = None

        if not self.default_autoplaylist:
            print("Warning: Autoplaylist is empty, disabling.")
        self.default_autoplaylist = self.parse_playlist(self.default_autoplaylist)

        # TODO: Do these properly
        ssd_defaults = {'last_np_msg': None, 'auto_paused': False, 'autoplaylist': self.default_autoplaylist}
        self.server_specific_data = defaultdict(lambda: dict(ssd_defaults))

        super().__init__()
        self.aiosession = aiohttp.ClientSession(loop=self.loop)
        self.http.user_agent += ' MusicBot/%s' % BOTVERSION

        self.db, self.session = init_db()
        #self.jobstore = SQLAlchemyJobStore(engine=self.db)
        self.jobstore = SQLAlchemyJobStore(url='sqlite:///jobs.sqlite')
        jobstores = {"default": self.jobstore}
        self.scheduler = AsyncIOScheduler(jobstores=jobstores)
        self.scheduler.add_listener(self.job_missed, events.EVENT_JOB_MISSED)

        self.scheduler.start()
        self.scheduler.print_jobs()

        self.should_restart = False

        self.app = web.Application()
        self.app.router.add_post('/update', self.handle_update)
        self.app.router.add_get('/update', self.handle_update)
        self.handler = self.app.make_handler()
        self.web_server = self.loop.create_server(self.handler,
                                                  MusicBot.host,
                                                  MusicBot.port)
        self.srv, startup_res = self.loop.run_until_complete(asyncio.gather(self.web_server,
                                                             self.app.startup(),
                                                             loop=self.loop))

    def owner_only(func):
        @wraps(func)
        async def wrapper(self, *args, **kwargs):
            # Only allow the owner to use these commands
            orig_msg = _get_variable('message')

            if not orig_msg or orig_msg.author.id == self.owner.id:
                return await func(self, *args, **kwargs)
            else:
                raise exceptions.PermissionsError("only the owner can use this command", expire_in=30)

        return wrapper

    @staticmethod
    def _fixg(x, dp=2):
        return ('{:.%sf}' % dp).format(x).rstrip('0').rstrip('.')

    async def _autojoin_channels(self, channels):
        joined_servers = []

        for channel in channels:
            if channel.server in joined_servers:
                print("Already joined a channel in %s, skipping" % channel.server.name)
                continue

            if channel and channel.type == discord.ChannelType.voice:
                self.safe_print("Attempting to autojoin %s in %s" % (channel.name, channel.server.name))

                chperms = channel.permissions_for(channel.server.me)

                if not chperms.connect:
                    self.safe_print("Cannot join channel \"%s\", no permission." % channel.name)
                    continue

                elif not chperms.speak:
                    self.safe_print("Will not join channel \"%s\", no permission to speak." % channel.name)
                    continue

                try:
                    player = await self.get_player(channel, create=True)

                    if player.is_stopped:
                        player.play()

                    await self.on_player_finished_playing(player)

                    joined_servers.append(channel.server)
                except Exception as e:
                    traceback.print_exc()
                    print("Failed to join", channel.name)

            elif channel:
                print("Not joining %s on %s, that's a text channel." % (channel.name, channel.server.name))

            else:
                print("Invalid channel thing: " + channel)

    async def _wait_delete_msg(self, message, after):
        await asyncio.sleep(after)
        await self.safe_delete_message(message)

    # TODO: Check to see if I can just move this to on_message after the response check
    async def _manual_delete_check(self, message, *, quiet=False):
        if self.config.delete_invoking:
            await self.safe_delete_message(message, quiet=quiet)

    async def _check_ignore_non_voice(self, msg):
        vc = msg.server.me.voice_channel

        # If we've connected to a voice chat and we're in the same voice channel
        if not vc or vc == msg.author.voice_channel:
            return True
        else:
            raise exceptions.PermissionsError(
                "you cannot use this command when not in the voice channel (%s)" % vc.name, expire_in=30)

    async def generate_invite_link(self, *, permissions=None, server=None):
        return discord.utils.oauth_url(self.user.id, permissions=permissions, server=server)

    async def get_voice_client(self, channel):
        if isinstance(channel, Object):
            channel = self.get_channel(channel.id)

        if getattr(channel, 'type', ChannelType.text) != ChannelType.voice:
            raise AttributeError('Channel passed must be a voice channel')

        with await self.voice_client_connect_lock:
            server = channel.server
            if server.id in self.the_voice_clients:
                return self.the_voice_clients[server.id]

            s_id = self.ws.wait_for('VOICE_STATE_UPDATE', lambda d: d.get('user_id') == self.user.id)
            _voice_data = self.ws.wait_for('VOICE_SERVER_UPDATE', lambda d: True)

            await self.ws.voice_state(server.id, channel.id)

            s_id_data = await asyncio.wait_for(s_id, timeout=10, loop=self.loop)
            voice_data = await asyncio.wait_for(_voice_data, timeout=10, loop=self.loop)
            session_id = s_id_data.get('session_id')

            kwargs = {
                'user': self.user,
                'channel': channel,
                'data': voice_data,
                'loop': self.loop,
                'session_id': session_id,
                'main_ws': self.ws
            }
            voice_client = VoiceClient(**kwargs)
            self.the_voice_clients[server.id] = voice_client

            retries = 3
            for x in range(retries):
                try:
                    print("Attempting connection...")
                    await asyncio.wait_for(voice_client.connect(), timeout=10, loop=self.loop)
                    print("Connection established.")
                    break
                except:
                    traceback.print_exc()
                    print("Failed to connect, retrying (%s/%s)..." % (x+1, retries))
                    await asyncio.sleep(1)
                    await self.ws.voice_state(server.id, None, self_mute=True)
                    await asyncio.sleep(1)

                    if x == retries-1:
                        raise exceptions.HelpfulError(
                            "Cannot establish connection to voice chat.  "
                            "Something may be blocking outgoing UDP connections.",

                            "This may be an issue with a firewall blocking UDP.  "
                            "Figure out what is blocking UDP and disable it.  "
                            "It's most likely a system firewall or overbearing anti-virus firewall.  "
                        )

            return voice_client

    async def mute_voice_client(self, channel, mute):
        await self._update_voice_state(channel, mute=mute)

    async def deafen_voice_client(self, channel, deaf):
        await self._update_voice_state(channel, deaf=deaf)

    async def move_voice_client(self, channel):
        await self._update_voice_state(channel)

    async def reconnect_voice_client(self, server):
        if server.id not in self.the_voice_clients:
            return

        vc = self.the_voice_clients.pop(server.id)
        _paused = False

        player = None
        if server.id in self.players:
            player = self.players[server.id]
            if player.is_playing:
                player.pause()
                _paused = True

        try:
            await vc.disconnect()
        except:
            print("Error disconnecting during reconnect")
            traceback.print_exc()

        await asyncio.sleep(0.1)

        if player:
            new_vc = await self.get_voice_client(vc.channel)
            player.reload_voice(new_vc)

            if player.is_paused and _paused:
                player.resume()

    async def disconnect_voice_client(self, server):
        if server.id not in self.the_voice_clients:
            return

        if server.id in self.players:
            self.players.pop(server.id).kill()

        await self.the_voice_clients.pop(server.id).disconnect()

    async def disconnect_all_voice_clients(self):
        for vc in self.the_voice_clients.copy().values():
            await self.disconnect_voice_client(vc.channel.server)

    async def _update_voice_state(self, channel, *, mute=False, deaf=False):
        if isinstance(channel, Object):
            channel = self.get_channel(channel.id)

        if getattr(channel, 'type', ChannelType.text) != ChannelType.voice:
            raise AttributeError('Channel passed must be a voice channel')

        # I'm not sure if this lock is actually needed
        with await self.voice_client_move_lock:
            server = channel.server

            payload = {
                'op': 4,
                'd': {
                    'guild_id': server.id,
                    'channel_id': channel.id,
                    'self_mute': mute,
                    'self_deaf': deaf
                }
            }

            await self.ws.send(utils.to_json(payload))
            self.the_voice_clients[server.id].channel = channel

    async def get_player(self, channel, create=False) -> MusicPlayer:
        server = channel.server

        if server.id not in self.players:
            if not create:
                raise exceptions.CommandError(
                    'The bot is not in a voice channel.  '
                    'Use %ssummon to summon it to your voice channel.' % self.config.command_prefix)

            voice_client = await self.get_voice_client(channel)

            playlist = Playlist(self)
            player = MusicPlayer(self, voice_client, playlist) \
                .on('play', self.on_player_play) \
                .on('resume', self.on_player_resume) \
                .on('pause', self.on_player_pause) \
                .on('stop', self.on_player_stop) \
                .on('finished-playing', self.on_player_finished_playing) \
                .on('entry-added', self.on_player_entry_added)

            player.skip_state = SkipState()
            self.players[server.id] = player

        return self.players[server.id]

    async def on_player_play(self, player, entry):
        player.skip_state.reset()
        await self.update_now_playing(entry)

        channel = entry.meta.get('channel', None)
        author = entry.meta.get('author', None)

        if channel and author:
            last_np_msg = self.server_specific_data[channel.server]['last_np_msg']
            if last_np_msg and last_np_msg.channel == channel:

                async for lmsg in self.logs_from(channel, limit=1):
                    if lmsg != last_np_msg and last_np_msg:
                        await self.safe_delete_message(last_np_msg)
                        self.server_specific_data[channel.server]['last_np_msg'] = None
                    break  # This is probably redundant

            if self.config.now_playing_mentions:
                newmsg = '%s - your song **%s** is now playing in %s!' % (
                    entry.meta['author'].mention, entry.title, player.voice_client.channel.name)
            else:
                newmsg = 'Now playing in %s: **%s**' % (
                    player.voice_client.channel.name, entry.title)
            if self.server_specific_data[channel.server]['last_np_msg']:
                self.server_specific_data[channel.server]['last_np_msg'] = await self.safe_edit_message(last_np_msg, newmsg, send_if_fail=True)
            else:
                self.server_specific_data[channel.server]['last_np_msg'] = await self.safe_send_message(channel, newmsg)

    async def on_player_resume(self, entry, **_):
        await self.update_now_playing(entry)

    async def on_player_pause(self, entry, **_):
        await self.update_now_playing(entry, True)

    async def on_player_stop(self, **_):
        await self.update_now_playing()

    async def on_player_finished_playing(self, player, **_):
        if not player.playlist.entries and not player.current_entry:
            if self.should_restart:
                await self.send_message(self.report_channel_dj,
                                        "Restarting now. If I don't come back soon, I'm ded and ping Blue :3")
                raise exceptions.RestartSignal
            server = player.voice_client.channel.server
            autoplaylist = self.server_specific_data[server]["autoplaylist"]
            if not autoplaylist:
                autoplaylist = self.server_specific_data[server]["autoplaylist"] = self.default_autoplaylist
            if not autoplaylist:
                print("No autoplaylist")
                return
            done = False
            while not done:
                done = True
                song_url = choice(autoplaylist)
                local = not song_url.startswith("http")
                if not local:
                    info = await self.downloader.safe_extract_info(player.playlist.loop,
                                                                   song_url,
                                                                   download=False,
                                                                   process=False)
                    if not info:
                        self.safe_print("[Info] Skipping {}".format(song_url))
                        done = False
                try:
                    await player.playlist.add_entry(song_url,
                                                    channel=None,
                                                    author=server.me,
                                                    local=local)
                except exceptions.ExtractionError as e:
                    print("Error adding song from autoplaylist:", e)
                    done = False

    async def on_player_entry_added(self, playlist, entry, **_):
        pass

    async def update_now_playing(self, entry=None, is_paused=False):
        game = None
        activeplayers = sum(1 for p in self.players.values() if p.is_playing)
        if activeplayers > 1:
            game = discord.Game(name=f"music on {activeplayers} servers")
            entry = None

        elif activeplayers == 1:
            player = discord.utils.get(self.players.values(), is_playing=True)
            entry = player.current_entry

        if entry:
            prefix = '\u275A\u275A ' if is_paused else ''

            name = f'{prefix}{entry.title}'[:128]
            game = discord.Game(name=name)

        await self.change_presence(game=game)

    async def handle_update(self, request):
        update = await request.json()
        response = await self.cmd_update(update)
        await self.safe_send_message(self.report_channel_dj,
                                     "",
                                     embed=response.embed)
        return web.Response(text="")

    async def safe_send_message(self, dest, content=None, *, embed=None, tts=False, expire_in=0, also_delete=None, quiet=False):
        msg = None
        try:
            msg = await self.send_message(dest, content, embed=embed, tts=tts)

            if msg and expire_in:
                asyncio.ensure_future(self._wait_delete_msg(msg, expire_in))

            if also_delete and isinstance(also_delete, discord.Message):
                asyncio.ensure_future(self._wait_delete_msg(also_delete, expire_in))

        except discord.Forbidden:
            if not quiet:
                self.safe_print(f"Warning: Cannot send message to {dest.name}, no permission")

        except discord.NotFound:
            if not quiet:
                self.safe_print(f"Warning: Cannot send message to {dest.name}, invalid channel?")

        return msg

    async def safe_delete_message(self, message, *, quiet=False):
        try:
            return await self.delete_message(message)

        except discord.Forbidden:
            if not quiet:
                self.safe_print(f"Warning: Cannot delete message \"{message.clean_content}\", no permission")

        except discord.NotFound:
            if not quiet:
                self.safe_print(f"Warning: Cannot delete message \"{message.clean_content}\", message not found")

    async def safe_edit_message(self, message, new, *, send_if_fail=False, quiet=False):
        try:
            return await self.edit_message(message, new)

        except discord.NotFound:
            if not quiet:
                self.safe_print(f"Warning: Cannot edit message \"{message.clean_content}\", message not found")
            if send_if_fail:
                if not quiet:
                    print("Sending instead")
                return await self.safe_send_message(message.channel, new)

    def safe_print(self, content, *, end='\n', flush=True):
        sys.stdout.buffer.write((content + end).encode('utf-8', 'replace'))
        if flush:
            sys.stdout.flush()

    async def send_typing(self, destination):
        try:
            return await super().send_typing(destination)
        except discord.Forbidden:
            if self.config.debug_mode:
                print(f"Could not send typing to {destination}, no permssion")

    async def edit_profile(self, **fields):
        return await super().edit_profile(**fields)

    def _cleanup(self):
        try:
            self.loop.run_until_complete(self.logout())
        except:
            pass

        self.srv.close()
        self.loop.run_until_complete(self.srv.wait_closed())
        self.loop.run_until_complete(self.app.shutdown())
        self.loop.run_until_complete(self.handler.finish_connections(60.0))
        self.loop.run_until_complete(self.app.cleanup())

        pending = asyncio.Task.all_tasks()
        gathered = asyncio.gather(*pending)

        try:
            gathered.cancel()
            self.loop.run_until_complete(gathered)
            gathered.exception()
        except:
            pass

    async def get_owner(self):
        return (await self.application_info()).owner

    # noinspection PyMethodOverriding
    def run(self):
        try:
            self.loop.run_until_complete(self.start(*self.config.auth))

        except discord.errors.LoginFailure:
            # Add if token, else
            raise exceptions.HelpfulError(
                "Bot cannot login, bad credentials.",
                "Fix your Email or Password or Token in the options file.  "
                "Remember that each field should be on their own line.")

        finally:
            try:
                self._cleanup()
            except Exception as e:
                print("Error in cleanup:", e)

            self.loop.close()
            if self.exit_signal:
                raise self.exit_signal

    async def logout(self):
        await self.disconnect_all_voice_clients()
        return await super().logout()

    async def on_error(self, event, *args, **kwargs):
        ex_type, ex, stack = sys.exc_info()

        if ex_type == exceptions.HelpfulError:
            print("Exception in", event)
            print(ex.message)

            await asyncio.sleep(2)  # don't ask
            await self.logout()

        elif issubclass(ex_type, exceptions.Signal):
            self.exit_signal = ex_type
            await self.logout()

        else:
            traceback.print_exc()

    async def on_resumed(self):
        for vc in self.the_voice_clients.values():
            vc.main_ws = self.ws

    async def on_ready(self):
        self.init_ok = True
        print(f'\rConnected!  Musicbot v{BOTVERSION}\n')
        self.safe_print(f"Bot:   {self.user.id}/{self.user.name}#{self.user.discriminator}")
        self.owner = await self.get_owner()
        self.safe_print(f"Owner: {self.owner.id}/{self.owner.name}#{self.owner.discriminator}\n")

        print('Server List:')
        for s in self.servers:
            self.safe_print(' - ' + s.name)

        print()

        if self.config.bound_channels:
            chlist = set(self.get_channel(i) for i in self.config.bound_channels if i)
            chlist.discard(None)
            invalids = set()

            invalids.update(c for c in chlist if c.type == discord.ChannelType.voice)
            chlist.difference_update(invalids)
            self.config.bound_channels.difference_update(invalids)

            print("Bound to text channels:")
            for ch in chlist:
                if ch:
                    self.safe_print(f' - {ch.server.name.strip()}/{ch.name.strip()}')

            if invalids and self.config.debug_mode:
                print("\nNot binding to voice channels:")
                [self.safe_print(' - %s/%s' % (ch.server.name.strip(), ch.name.strip())) for ch in invalids if ch]

            print()

        else:
            print("Not bound to any text channels")

        if self.config.autojoin_channels:
            chlist = set(self.get_channel(i) for i in self.config.autojoin_channels if i)
            chlist.discard(None)
            invalids = set()

            invalids.update(c for c in chlist if c.type == discord.ChannelType.text)
            chlist.difference_update(invalids)
            self.config.autojoin_channels.difference_update(invalids)

            print("Autojoining voice chanels:")
            [self.safe_print(' - %s/%s' % (ch.server.name.strip(), ch.name.strip())) for ch in chlist if ch]

            if invalids and self.config.debug_mode:
                print("\nCannot join text channels:")
                [self.safe_print(' - %s/%s' % (ch.server.name.strip(), ch.name.strip())) for ch in invalids if ch]

            autojoin_channels = chlist

        else:
            print("Not autojoining any voice channels")
            autojoin_channels = set()

        print()
        print("Options:")

        self.safe_print("  Command prefix: " + self.config.command_prefix)
        print("  Default volume: %s%%" % int(self.config.default_volume * 100))
        print("  Skip threshold: %s votes or %s%%" % (
            self.config.skips_required, self._fixg(self.config.skip_ratio_required * 100)))
        print("  Now Playing @mentions: " + ['Disabled', 'Enabled'][self.config.now_playing_mentions])
        print("  Auto-Summon: " + ['Disabled', 'Enabled'][self.config.auto_summon])
        print("  Auto-Pause: " + ['Disabled', 'Enabled'][self.config.auto_pause])
        print("  Delete Messages: " + ['Disabled', 'Enabled'][self.config.delete_messages])
        if self.config.delete_messages:
            print("    Delete Invoking: " + ['Disabled', 'Enabled'][self.config.delete_invoking])
        print("  Debug Mode: " + ['Disabled', 'Enabled'][self.config.debug_mode])
        print("  Downloaded songs will be %s" % ['deleted', 'saved'][self.config.save_videos])
        print()

        self.report_channel = self.get_channel(self.config.report_channel)
        self.report_channel_dj = self.get_channel("283362758509199370")
        self.survey_channel = self.get_channel("347369267869777920")

        print()
        print("Setup report channel to:", self.report_channel)

        if self.config.autojoin_channels:
            await self._autojoin_channels(autojoin_channels)

        await self.db_load()

        await self.check_new_members()

    async def db_load(self):
        for server in self.servers:
            if self.get_server_db(server) is None:
                if server.id == "365116542574395393":
                    await self.configure_new_server(server)

    def get_server_db(self, server):
        return self.session.query(Server).filter(Server.discord_id == server.id).first()

    async def configure_new_server(self, server):
        sendable_channels = await self.get_sendable_channels(server)
        await self.safe_send_message(sendable_channels[0],
                                     "Hello, and thanks for deciding to use this bot.\n"
                                     "To start off, let's choose a channel to continue the configuration process.\n"
                                     "This bot must have both read and send message permissions in that channel.\n"
                                     "Please reply with a mention for a channel.\n"
                                    f"This channel would be {sendable_channels[0].mention}.\n"
                                     "To continue, the next command requires users to have the Manage Server "
                                     "permission.\n"
                                     "This is to protect your server whilst we're setting up permissions.\n"
                                     "Note that after the channel is decided, anybody with access to that channel can "
                                     "continue setup")
        channel = await self.get_sendable_channel(sendable_channels[0])
        report_here = await self.ask_yn(channel,
                                        "Ok, let's continue here, shall we?\n"
                                        "Firstly, would it be ok to use this channel as a general report channel for "
                                        "various things, such as automated messages, some of which might be sensitive.",
                                        check=self.has_manage_server)
        if report_here:
            report_channel = channel
        else:
            await self.safe_send_message(channel, "Please mention a channel where I can report various things, some of "
                                                  "which might be considered sensitive.")
            report_channel = await self.get_sendable_channel(channel)
            await self.safe_send_message(channel, f"Set report channel to {report_channel.mention}")
        enable_fresh = await self.ask_yn(channel, "Do you want to enable the removal of a 'new user' role 7 days after joining the server?")
        while not server.me.server_permissions.manage_roles and enable_fresh:
            enable_fresh = await self.ask_yn(channel, "I need to be able to manage roles for this.\n"
                                                      "Please give me manage  roles and select 🇾 or select 🇳 and I "
                                                      "won't be able to do this.")
        regular_role = fresh_role = None
        if enable_fresh:
            await self.safe_send_message(channel, "Now I've got to find your role for regulars.")
            regular_role = await self.choose_role(channel)
            await self.safe_send_message(channel, "Next is the role that new users get assigned for a week after the "
                                                  "regular role is assigned.")
            fresh_role = await self.choose_role(channel)
        auto_connect = await self.ask_yn(channel, "Do you want me to automatically connect to a voice channel in case "
                                                  "I crash and restart?")
        connect_channel = None
        if auto_connect:
            connect_channel = await self.get_voice_channel(channel)
            await self.safe_send_message(channel, f"Set autojoin channel to {connect_channel.mention}")
        await self.safe_send_message(channel, "Please choose the maximum skip votes to pass a skip")
        while 1:
            message = await self.wait_for_message(channel=channel,
                                                  check=lambda message: message.author.id != self.user.id)
            if message.content.isdigit():
                skip_max = int(message.content)
                break
            await self.safe_send_message(channel, "Please enter a number")
        await self.safe_send_message(channel, "Please choose the skip ratio to skip otherwise. (In the form 1/3)")
        while 1:
            message = await self.wait_for_message(channel=channel,
                                                  check=lambda message: message.author.id != self.user.id)
            digits = message.content.split("/")
            if len(digits) == 2:
                if all(map(str.isdigit, digits)):
                    skip_ratio_num, skip_ratio_den = map(int, digits)
                    if skip_ratio_num / skip_ratio_den < 1:
                        break
            await self.safe_send_message(channel, "Please respond in a fractional form (1/3, 3/5 and 1/2 would all be valid. 4/3, 2/2 are not.)")
        files = [os.path.split(path)[-1][:-4] for path in glob.glob(os.path.join("playlists", "*.txt"))]
        files = files[:19]
        emotes = ['0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'[i]+"\u20E3" if i<10 else chr(i+127452) for i in range(36)]
        joined_files = "\n".join(f"{emotes[i]}: {f}" for i, f in enumerate(["No autoplaylist"] + files))
        message = await self.safe_send_message(channel, "I come with a couple built-in playlists.\n"
                                                        "Please react with the one you want to use by default:\n"
                                                       f"{joined_files}")
        for i in range(len(files)+1):
            await self.add_reaction(message, f"{emotes[i]}")
        react, user = await self.wait_for_reaction([emotes[i] for i in range(len(files)+1)],
                                                     message=message,
                                                     check=lambda reaction, user: user.id != self.user.id)
        index = emotes.index(react.emoji)
        autoplaylist = None
        if index:
            autoplaylist = files[index-1]
        await self.safe_send_message(channel, "Please choose the prefix for commands (one character)")
        while 1:
            message = await self.wait_for_message(channel=channel,
                                                  check=lambda message: message.author.id != self.user.id)
            if len(message.content) == 1:
                prefix = message.content
                break
            await self.safe_send_message(channel, "Please enter a single character")

        #server = Server(discord_id=channel.server.id)

    async def choose_role(self, channel):
        await self.safe_send_message(channel, "Either mention a role or type the name of a role.")
        while True:
            message = await self.wait_for_message(channel=channel,
                                                  check=lambda message: message.author.id != self.user.id)
            if len(message.mentions) == 1:
                return message.mentions[0]
            elif len(message.mentions) == 0:
                roles = []
                for server_role in channel.server.roles:
                    if server_role.name.lower() == message.content.lower():
                        roles.append(server_role)

                if len(roles) == 1:
                    return roles[0]
                elif len(roles) >= 2:
                    message = await self.safe_send_message(channel,
                                                           "Multiple roles were found with that name.\n"
                                                           "Please move the role you wish to be the regular role below the rest.\n"
                                                           "You may return the role to it's original position later\n"
                                                           "Once you are happy, react with 🇾")
                    await self.add_reaction(message, "🇾")
                    await self.wait_for_reaction("🇾",
                                                 message=message,
                                                 check=lambda reaction, user: user.id != self.user.id)
                    return max(roles, key=lambda role: role.position)
                else:
                    await self.safe_send_message(channel, "No roles were found with that name.")
            else:
                await self.safe_send_message(channel, "You mentioned multiple roles. Please only mention a single role "
                                                      "or a name of a role.")

    async def get_sendable_channel(self, current):
        is_valid = False
        while not is_valid:
            mentions = []
            while len(mentions) != 1:
                message = await self.wait_for_message(channel=current,
                                                      check=lambda message: self.has_manage_server(message.author))
                mentions = message.channel_mentions
                if len(mentions) != 1:
                    await self.safe_send_message(current, "Please mention a single channel to continue setup")
            channel = mentions[0]
            if channel not in (await self.get_sendable_channels(current.server)):
                await self.send_message(current, f"I cannot read and send messages in {channel.mention}")
            else:
                is_valid = True
        return channel

    async def get_voice_channel(self, current):
        def verify_channel(reaction, user):
            if user.id == self.user.id:
                return False
            channel = user.voice_channel
            if not channel:
                asyncio.ensure_future(self.send_message(current, "Please remove your reaction, connect to a voice "
                                                                 "channel and then add it again."))
                return False
            permissions = channel.permissions_for(current.server.me)
            if permissions.connect and permissions.speak:
                return True
            asyncio.ensure_future(self.send_message(current, f"I cannot connect and speak in {channel.mention}.\n"
                                                             f"Please remove your reaction and move to a channel I can "
                                                             f"connect and speak"))
            return False

        message = await self.send_message(current, "Please join the voice channel you to select. I must have connect "
                                                   "and speak permissions in that channel. Once you're done, react "
                                                   "with 🇾")
        await self.add_reaction(message, "🇾")
        react, user = await self.wait_for_reaction("🇾",
                                                   message=message,
                                                   check=verify_channel)
        return user.voice_channel

    async def get_sendable_channels(self, server):
        sendable_channels = []
        for channel in server.channels:
            if channel.type == discord.ChannelType.text:
                permissions = channel.permissions_for(server.me)
                if permissions.send_messages and permissions.read_messages:
                    sendable_channels.append(channel)
        return sendable_channels

    def has_manage_server(self, user):
        if user.id == self.user.id:
            return False
        return user.server_permissions.manage_server

    async def cmd_help(self, command=None):
        """
        Usage:
            {command_prefix}help [command]

        Prints a help message.
        If a command is specified, it prints a help message for that command.
        Otherwise, it lists the available commands.
        """

        if command:
            cmd = getattr(self, 'cmd_' + command, None)
            if cmd:
                embed = discord.Embed(title=f"Help for {command}",
                                      description=dedent(cmd.__doc__).format(command_prefix=self.config.command_prefix))
                return Response(embed=embed,
                                delete_after=60)
            else:
                return Response("No such command", delete_after=10)

        else:
            helpmsg = "**Commands**\n```"
            commands = []

            for att in dir(self):
                if att.startswith('cmd_') and att != 'cmd_help':
                    command_name = att.replace('cmd_', '').lower()
                    commands.append("{}{}".format(self.config.command_prefix, command_name))

            helpmsg += ", ".join(commands)
            helpmsg += "```"
            helpmsg += "https://github.com/SexualRhinoceros/MusicBot/wiki/Commands-list"

            return Response(helpmsg, reply=True, delete_after=60)

    async def cmd_blacklist(self, user_mentions, option):
        """
        Usage:
            {command_prefix}blacklist [ + | - | add | remove ] @UserName [@UserName2 ...]

        Add or remove users to the blacklist.
        Blacklisted users are forbidden from using bot commands.
        """

        if not user_mentions:
            raise exceptions.CommandError("No users listed.", expire_in=20)

        if option not in ['+', '-', 'add', 'remove']:
            raise exceptions.CommandError(
                'Invalid option "%s" specified, use +, -, add, or remove' % option, expire_in=20
            )

        for user in user_mentions.copy():
            if user.id == (await self.get_owner()).id:
                print("[Commands:Blacklist] The owner cannot be blacklisted.")
                user_mentions.remove(user)

        old_len = len(self.blacklist)

        if option in ['+', 'add']:
            self.blacklist.update(user.id for user in user_mentions)

            write_file(self.config.blacklist_file, self.blacklist)

            return Response(
                '%s users have been added to the blacklist' % (len(self.blacklist) - old_len),
                reply=True, delete_after=10
            )

        else:
            if self.blacklist.isdisjoint(user.id for user in user_mentions):
                return Response('none of those users are in the blacklist.', reply=True, delete_after=10)

            else:
                self.blacklist.difference_update(user.id for user in user_mentions)
                write_file(self.config.blacklist_file, self.blacklist)

                return Response(
                    '%s users have been removed from the blacklist' % (old_len - len(self.blacklist)),
                    reply=True, delete_after=10
                )

    async def cmd_id(self, author, user_mentions):
        """
        Usage:
            {command_prefix}id [@user]

        Tells the user their id or the id of another user.
        """
        if not user_mentions:
            return Response('your id is `%s`' % author.id, reply=True, delete_after=35)
        else:
            usr = user_mentions[0]
            return Response("%s's id is `%s`" % (usr.name, usr.id), reply=True, delete_after=35)

    async def cmd_joinserver(self):
        """
        Usage:
            {command_prefix}joinserver invite_link

        Asks the bot to join a server.  Note: Bot accounts cannot use invite links.
        """

        url = await self.generate_invite_link()
        return Response("Click here to invite me: \n{}".format(url),
                        reply=True,
                        delete_after=30)

    async def cmd_play(self, player, channel, author, message, permissions, leftover_args, song_url=""):
        """
        Usage:
            {command_prefix}play song_link
            {command_prefix}play text to search for

        Adds the song to the playlist.  If a link is not provided, the first
        result from a youtube search is added to the queue.
        """

        if hasattr(message, "attachments") and message.attachments:
            song_url = message.attachments[0]["url"]

        song_url = song_url.strip('<>')

        if not song_url:
            return Response("```{}```".format(self.cmd_play.__doc__))

        if permissions.max_songs and player.playlist.count_for_user(author) >= permissions.max_songs:
            raise exceptions.PermissionsError(
                "You have reached your enqueued song limit (%s)" % permissions.max_songs, expire_in=30
            )

        if leftover_args:
            song_url = ' '.join([song_url, *leftover_args])
            await self.send_typing(channel)

        try:
            info = await self.downloader.extract_info(player.playlist.loop, song_url, download=False, process=False)
        except Exception as e:
            raise exceptions.CommandError(e, expire_in=30)

        if not info:
            raise exceptions.CommandError("That video cannot be played.", expire_in=30)

        # abstract the search handling away from the user
        # our ytdl options allow us to use search strings as input urls
        if info.get('url', '').startswith('ytsearch'):
            # print("[Command:play] Searching for \"%s\"" % song_url)
            info = await self.downloader.extract_info(
                player.playlist.loop,
                song_url,
                download=False,
                process=True,    # ASYNC LAMBDAS WHEN
                on_error=lambda e: asyncio.ensure_future(
                    self.safe_send_message(channel, "```\n%s\n```" % e, expire_in=120), loop=self.loop),
                retry_on_error=True
            )

            if not info:
                raise exceptions.CommandError(
                    "Error extracting info from search string, youtubedl returned no data.  "
                    "You may need to restart the bot if this continues to happen.", expire_in=30
                )

            if not all(info.get('entries', [])):
                # empty list, no data
                return

            song_url = info['entries'][0]['webpage_url']
            info = await self.downloader.extract_info(player.playlist.loop, song_url, download=False, process=False)
            # Now I could just do: return await self.cmd_play(player, channel, author, song_url)
            # But this is probably fine

        # TODO: Possibly add another check here to see about things like the bandcamp issue
        # TODO: Where ytdl gets the generic extractor version with no processing, but finds two different urls

        if 'entries' in info:
            # I have to do exe extra checks anyways because you can request an arbitrary number of search results
            if not permissions.allow_playlists and ':search' in info['extractor'] and len(info['entries']) > 1:
                raise exceptions.PermissionsError("You are not allowed to request playlists", expire_in=30)

            # The only reason we would use this over `len(info['entries'])` is if we add `if _` to this one
            num_songs = sum(1 for _ in info['entries'])

            if permissions.max_playlist_length and num_songs > permissions.max_playlist_length:
                raise exceptions.PermissionsError(
                    "Playlist has too many entries (%s > %s)" % (num_songs, permissions.max_playlist_length),
                    expire_in=30
                )

            # This is a little bit weird when it says (x + 0 > y), I might add the other check back in
            if permissions.max_songs and player.playlist.count_for_user(author) + num_songs > permissions.max_songs:
                raise exceptions.PermissionsError(
                    "Playlist entries + your already queued songs reached limit (%s + %s > %s)" % (
                        num_songs, player.playlist.count_for_user(author), permissions.max_songs),
                    expire_in=30
                )

            if info['extractor'].lower() in ['youtube:playlist', 'soundcloud:set', 'bandcamp:album']:
                try:
                    return await self._cmd_play_playlist_async(player, channel, author, permissions, song_url, info['extractor'])
                except exceptions.CommandError:
                    raise
                except Exception as e:
                    traceback.print_exc()
                    raise exceptions.CommandError("Error queuing playlist:\n%s" % e, expire_in=30)

            t0 = time.time()

            # My test was 1.2 seconds per song, but we maybe should fudge it a bit, unless we can
            # monitor it and edit the message with the estimated time, but that's some ADVANCED SHIT
            # I don't think we can hook into it anyways, so this will have to do.
            # It would probably be a thread to check a few playlists and get the speed from that
            # Different playlists might download at different speeds though
            wait_per_song = 1.2

            procmesg = await self.safe_send_message(
                channel,
                'Gathering playlist information for {} songs{}'.format(
                    num_songs,
                    ', ETA: {} seconds'.format(self._fixg(
                        num_songs * wait_per_song)) if num_songs >= 10 else '.'))

            # We don't have a pretty way of doing this yet.  We need either a loop
            # that sends these every 10 seconds or a nice context manager.
            await self.send_typing(channel)

            # TODO: I can create an event emitter object instead, add event functions, and every play list might be asyncified
            #       Also have a "verify_entry" hook with the entry as an arg and returns the entry if its ok

            entry_list, position = await player.playlist.import_from(song_url, channel=channel, author=author)

            tnow = time.time()
            ttime = tnow - t0
            listlen = len(entry_list)
            drop_count = 0

            if permissions.max_song_length:
                for e in entry_list.copy():
                    if e.duration > permissions.max_song_length:
                        player.playlist.entries.remove(e)
                        entry_list.remove(e)
                        drop_count += 1
                        # Im pretty sure there's no situation where this would ever break
                        # Unless the first entry starts being played, which would make this a race condition
                if drop_count:
                    print("Dropped %s songs" % drop_count)

            print("Processed {} songs in {} seconds at {:.2f}s/song, {:+.2g}/song from expected ({}s)".format(
                listlen,
                self._fixg(ttime),
                ttime / listlen,
                ttime / listlen - wait_per_song,
                self._fixg(wait_per_song * num_songs))
            )

            await self.safe_delete_message(procmesg)

            if not listlen - drop_count:
                raise exceptions.CommandError(
                    "No songs were added, all songs were over max duration (%ss)" % permissions.max_song_length,
                    expire_in=30
                )

            reply_text = "Enqueued **%s** songs to be played. Position in queue: %s"
            btext = str(listlen - drop_count)

        else:
            if permissions.max_song_length and info.get('duration', 0) > permissions.max_song_length:
                raise exceptions.PermissionsError(
                    "Song duration exceeds limit (%s > %s)" % (info['duration'], permissions.max_song_length),
                    expire_in=30
                )

            try:
                entry, position = await player.playlist.add_entry(song_url,
                                                                  channel=channel,
                                                                  author=author)

            except exceptions.WrongEntryTypeError as e:
                if e.use_url == song_url:
                    print("[Warning] Determined incorrect entry type, but suggested url is the same.  Help.")

                if self.config.debug_mode:
                    print("[Info] Assumed url \"%s\" was a single entry, was actually a playlist" % song_url)
                    print("[Info] Using \"%s\" instead" % e.use_url)

                return await self.cmd_play(player, channel, author, permissions, leftover_args, e.use_url)

            reply_text = "Enqueued **%s** to be played. Position in queue: %s"
            btext = entry.title

        if position == 1 and player.is_stopped:
            position = 'Up next!'
            reply_text %= (btext, position)

        else:
            try:
                time_until = await player.playlist.estimate_time_until(position, player)
                reply_text += ' - estimated time until playing: %s'
            except:
                traceback.print_exc()
                time_until = ''

            reply_text %= (btext, position, time_until)

        return Response(reply_text, delete_after=30)

    async def cmd_play_local(self, player, author, channel, path):
        """
        Usage: Play a file or files given a local path.
        Expands wildcards.
        """
        files = [path]
        if "*" in path:
            files = glob.glob(path, recursive=True)
        return await self._cmd_queue_song_list(player, author, channel, files)

    async def cmd_awsw(self, player, author, channel, song_id):
        if not song_id.isdigit():
            return Response("Must be the soundtrack number (44 - Spring).")
        files = glob.glob("./music/AWSW/{}.*".format(song_id))
        if not files:
            return Response("No file found with that id")
        return await self._cmd_queue_song_list(player, author, channel, files)

    async def cmd_queue_playlist(self, player, author, channel, path):
        """
        Usage: Add a predefined playlist to the queue.
        """
        safe_path = slugify(path)
        playlist = load_file(os.path.join("playlists", safe_path+".txt"))
        song_urls = self.parse_playlist(playlist)
        return await self._cmd_queue_song_list(player, author, channel, song_urls)

    async def cmd_get_playlists(self, path=None):
        """
        Usage: Get all predefined playlists.
        Optional: Specific playlist: lists songs in playlist
        """
        if path is None:
            files = [os.path.split(path)[-1][:-4] for path in glob.glob(os.path.join("playlists", "*.txt"))]
            return Response(", ".join(files), reply=True)
        safe_path = slugify(path)
        playlist = load_file(os.path.join("playlists", safe_path+".txt"))
        song_urls = [os.path.split(path)[-1] for path in self.parse_playlist(playlist)]
        return Response("\n".join(song_urls), delete_after=35)

    async def cmd_create_playlist_search(self, player, channel, author, playlist):
        name, *songs = playlist.split("\n")
        name = slugify(name)
        urls = []
        for search_term in songs:
            search_query = 'ytsearch10:{}'.format(search_term)

            search_msg = await self.send_message(channel, "Searching for videos...")
            await self.send_typing(channel)

            try:
                info = await self.downloader.extract_info(player.playlist.loop, search_query, download=False,
                                                          process=True)
            except Exception as e:
                await self.safe_edit_message(search_msg, str(e), send_if_fail=True)
                return
            else:
                await self.safe_delete_message(search_msg)

            if not info:
                await self.safe_send_message(channel, "No videos found for {}".format(search_term))

            for e in info['entries']:
                result_message = await self.safe_send_message(channel, "Result %s/%s: %s" % (
                    info['entries'].index(e) + 1, len(info['entries']), e['webpage_url']))

                confirm_message = await self.safe_send_message(channel, "Is this ok? Type `y`, `n`")
                response_message = await self.wait_for_message(30, author=author, channel=channel, check=lambda m: m.content.lower()[0] in 'yn')
                if response_message.content.lower().startswith('y'):
                    await self.safe_delete_message(result_message)
                    await self.safe_delete_message(confirm_message)
                    await self.safe_delete_message(response_message)
                    urls.append(e['webpage_url'])
                    break
                else:
                    await self.safe_delete_message(result_message)
                    await self.safe_delete_message(confirm_message)
                    await self.safe_delete_message(response_message)
            else:
                await self.safe_send_message(channel, "No videos accepted for {}".format(search_term))
        write_file(os.path.join("playlists", name+".txt"), urls)
        await self.safe_send_message(channel, "Added playlist and saved as {}".format(name))

    async def cmd_create_playlist_urls(self, channel, playlist):
        name, *urls = playlist.split("\n")
        name = slugify(name)
        write_file(os.path.join("playlists", name+".txt"), urls)
        await self.safe_send_message(channel, "Added playlist and saved as {}".format(name))

    async def cmd_clear_audiocache(self):
        errors = []
        for f in os.listdir(self.downloader.download_folder):
            path = os.path.join(self.downloader.download_folder, f)
            try:
                if os.path.isfile(path):
                    os.unlink(path)
            except Exception as e:
                errors.append((f, str(e)))
        if errors:
            embed = discord.Embed(title="Errors",
                                  description=f"When removing errors, {len(errors)} were raised",
                                  colour=0x3485e7)
            for filename, error in errors[:15]:
                embed.add_field(name=filename, value=error)
            return Response(embed=embed)
        return Response("The audiocache was cleared successfully")

    async def cmd_set_autoplaylist(self, server, path):
        """
        Set the autoplaylist to a playlist
        """
        safe_path = slugify(path)
        playlist = load_file(os.path.join("playlists", safe_path+".txt"))
        if not playlist:
            return Response("Playlist {} not found".format(safe_path))
        self.server_specific_data[server]["autoplaylist"] = self.parse_playlist(playlist)
        return Response("Changed the autoplaylist to {}".format(safe_path))

    async def cmd_history(self, player):
        text = []
        for i, entry in enumerate(player.history):
            text.append("{}. [{}]({})".format(i+1, entry.title, getattr(entry, "url", "")))
        text = "\n".join(text) or "There have been no played songs"
        embed = discord.Embed(title="History",
                              description=text,
                              colour=0x3485e7)
        return Response(embed=embed, delete_after=60)

    async def _cmd_queue_song_list(self, player, author, channel, song_list):
        replies = []
        for path in song_list:
            entry, position = await player.playlist.add_entry(path,
                                                              channel=channel,
                                                              author=author,
                                                              local=os.path.exists(path))

            song_text = "Enqueued **%s** to be played. Position in queue: %s"
            btext = entry.title

            if position == 1 and player.is_stopped:
                position = 'Up next!'
                song_text %= (btext, position)

            else:
                try:
                    time_until = await player.playlist.estimate_time_until(position, player)
                    song_text += ' - estimated time until playing: %s'
                except:
                    traceback.print_exc()
                    time_until = ''

                song_text %= (btext, position, time_until)
            replies.append(song_text)
        return Response("\n".join(replies), delete_after=30)

    async def _cmd_play_playlist_async(self, player, channel, author, permissions, playlist_url, extractor_type):
        """
        Secret handler to use the async wizardry to make playlist queuing non-"blocking"
        """

        await self.send_typing(channel)
        info = await self.downloader.extract_info(player.playlist.loop, playlist_url, download=False, process=False)

        if not info:
            raise exceptions.CommandError("That playlist cannot be played.")

        num_songs = sum(1 for _ in info['entries'])
        t0 = time.time()

        busymsg = await self.safe_send_message(
            channel, "Processing %s songs..." % num_songs)  # TODO: From playlist_title
        await self.send_typing(channel)

        entries_added = 0
        if extractor_type == 'youtube:playlist':
            try:
                entries_added = await player.playlist.async_process_youtube_playlist(
                    playlist_url, channel=channel, author=author)
                # TODO: Add hook to be called after each song
                # TODO: Add permissions

            except Exception:
                traceback.print_exc()
                raise exceptions.CommandError('Error handling playlist %s queuing.' % playlist_url, expire_in=30)

        elif extractor_type.lower() in ['soundcloud:set', 'bandcamp:album']:
            try:
                entries_added = await player.playlist.async_process_sc_bc_playlist(
                    playlist_url, channel=channel, author=author)
                # TODO: Add hook to be called after each song
                # TODO: Add permissions

            except Exception:
                traceback.print_exc()
                raise exceptions.CommandError('Error handling playlist %s queuing.' % playlist_url, expire_in=30)

        songs_processed = len(entries_added)
        drop_count = 0
        skipped = False

        if permissions.max_song_length:
            for e in entries_added.copy():
                if e.duration > permissions.max_song_length:
                    try:
                        player.playlist.entries.remove(e)
                        entries_added.remove(e)
                        drop_count += 1
                    except:
                        pass

            if drop_count:
                print("Dropped %s songs" % drop_count)

            if player.current_entry and player.current_entry.duration > permissions.max_song_length:
                await self.safe_delete_message(self.server_specific_data[channel.server]['last_np_msg'])
                self.server_specific_data[channel.server]['last_np_msg'] = None
                skipped = True
                player.skip()
                entries_added.pop()

        await self.safe_delete_message(busymsg)

        songs_added = len(entries_added)
        tnow = time.time()
        ttime = tnow - t0
        wait_per_song = 1.2
        # TODO: actually calculate wait per song in the process function and return that too

        # This is technically inaccurate since bad songs are ignored but still take up time
        print("Processed {}/{} songs in {} seconds at {:.2f}s/song, {:+.2g}/song from expected ({}s)".format(
            songs_processed,
            num_songs,
            self._fixg(ttime),
            ttime / num_songs,
            ttime / num_songs - wait_per_song,
            self._fixg(wait_per_song * num_songs))
        )

        if not songs_added:
            basetext = "No songs were added, all songs were over max duration (%ss)" % permissions.max_song_length
            if skipped:
                basetext += "\nAdditionally, the current song was skipped for being too long."

            raise exceptions.CommandError(basetext, expire_in=30)

        return Response("Enqueued {} songs to be played in {} seconds".format(
            songs_added, self._fixg(ttime, 1)), delete_after=30)

    async def cmd_search(self, player, channel, author, message, permissions, leftover_args):
        """
        Usage:
            {command_prefix}search [service] [number] query

        Searches a service for a video and adds it to the queue.
        - service: any one of the following services:
            - youtube (yt) (default if unspecified)
            - soundcloud (sc)
            - yahoo (yh)
        - number: return a number of video results and waits for user to choose one
          - defaults to 1 if unspecified
          - note: If your search query starts with a number,
                  you must put your query in quotes
            - ex: {command_prefix}search 2 "I ran seagulls"
        """

        if permissions.max_songs and player.playlist.count_for_user(author) > permissions.max_songs:
            raise exceptions.PermissionsError(
                "You have reached your playlist item limit (%s)" % permissions.max_songs,
                expire_in=30
            )

        def argcheck():
            if not leftover_args:
                raise exceptions.CommandError(
                    "Please specify a search query.\n%s" % dedent(
                        self.cmd_search.__doc__.format(command_prefix=self.config.command_prefix)),
                    expire_in=60
                )

        argcheck()

        try:
            leftover_args = shlex.split(' '.join(leftover_args))
        except ValueError:
            raise exceptions.CommandError("Please quote your search query properly.", expire_in=30)

        service = 'youtube'
        items_requested = 3
        max_items = 10  # this can be whatever, but since ytdl uses about 1000, a small number might be better
        services = {
            'youtube': 'ytsearch',
            'soundcloud': 'scsearch',
            'yahoo': 'yvsearch',
            'yt': 'ytsearch',
            'sc': 'scsearch',
            'yh': 'yvsearch'
        }

        if leftover_args[0] in services:
            service = leftover_args.pop(0)
            argcheck()

        if leftover_args[0].isdigit():
            items_requested = int(leftover_args.pop(0))
            argcheck()

            if items_requested > max_items:
                raise exceptions.CommandError("You cannot search for more than %s videos" % max_items)

        # Look jake, if you see this and go "what the fuck are you doing"
        # and have a better idea on how to do this, i'd be delighted to know.
        # I don't want to just do ' '.join(leftover_args).strip("\"'")
        # Because that eats both quotes if they're there
        # where I only want to eat the outermost ones
        if leftover_args[0][0] in '\'"':
            lchar = leftover_args[0][0]
            leftover_args[0] = leftover_args[0].lstrip(lchar)
            leftover_args[-1] = leftover_args[-1].rstrip(lchar)

        search_query = '%s%s:%s' % (services[service], items_requested, ' '.join(leftover_args))

        search_msg = await self.send_message(channel, "Searching for videos...")
        await self.send_typing(channel)

        try:
            info = await self.downloader.extract_info(player.playlist.loop, search_query, download=False, process=True)

        except Exception as e:
            await self.safe_edit_message(search_msg, str(e), send_if_fail=True)
            return
        else:
            await self.safe_delete_message(search_msg)

        if not info:
            return Response("No videos found.", delete_after=30)

        def check(m):
            return (
                m.content.lower()[0] in 'yn' or
                # hardcoded function name weeee
                m.content.lower().startswith('{}{}'.format(self.config.command_prefix, 'search')) or
                m.content.lower().startswith('exit'))

        for e in info['entries']:
            result_message = await self.safe_send_message(channel, "Result %s/%s: %s" % (
                info['entries'].index(e) + 1, len(info['entries']), e['webpage_url']))

            confirm_message = await self.safe_send_message(channel, "Is this ok? Type `y`, `n` or `exit`")
            response_message = await self.wait_for_message(30, author=author, channel=channel, check=check)

            if not response_message:
                await self.safe_delete_message(result_message)
                await self.safe_delete_message(confirm_message)
                return Response("Ok nevermind.", delete_after=30)

            # They started a new search query so lets clean up and bugger off
            elif response_message.content.startswith(self.config.command_prefix) or \
                    response_message.content.lower().startswith('exit'):

                await self.safe_delete_message(result_message)
                await self.safe_delete_message(confirm_message)
                return

            if response_message.content.lower().startswith('y'):
                await self.safe_delete_message(result_message)
                await self.safe_delete_message(confirm_message)
                await self.safe_delete_message(response_message)
                return await self.cmd_play(player, channel, author, message, permissions, [], e['webpage_url'])
            else:
                await self.safe_delete_message(result_message)
                await self.safe_delete_message(confirm_message)
                await self.safe_delete_message(response_message)

        return Response("Oh well :frowning:", delete_after=30)

    async def cmd_np(self, player, channel, server, message):
        """
        Usage:
            {command_prefix}np

        Displays the current song in chat.
        """

        if player.current_entry:
            if self.server_specific_data[server]['last_np_msg']:
                await self.safe_delete_message(self.server_specific_data[server]['last_np_msg'])
                self.server_specific_data[server]['last_np_msg'] = None

            song_progress = str(timedelta(seconds=player.progress)).lstrip('0').lstrip(':')
            song_total = str(timedelta(seconds=player.current_entry.duration)).lstrip('0').lstrip(':')
            prog_str = '`[%s/%s]`' % (song_progress, song_total)

            if player.current_entry.meta.get('channel', False) and player.current_entry.meta.get('author', False):
                np_text = "Now Playing: **%s** added by **%s** %s\n" % (
                    player.current_entry.title, player.current_entry.meta['author'].name, prog_str)
            else:
                np_text = "Now Playing: **%s** %s\n" % (player.current_entry.title, prog_str)

            self.server_specific_data[server]['last_np_msg'] = await self.safe_send_message(channel, np_text)
            await self._manual_delete_check(message)
        else:
            return Response(
                'There are no songs queued! Queue something with {}play.'.format(self.config.command_prefix),
                delete_after=30
            )

    async def cmd_summon(self, channel, author):
        """
        Usage:
            {command_prefix}summon

        Call the bot to the summoner's voice channel.
        """

        if not author.voice_channel:
            raise exceptions.CommandError('You are not in a voice channel!')

        voice_client = self.the_voice_clients.get(channel.server.id, None)
        if voice_client and voice_client.channel.server == author.voice_channel.server:
            await self.move_voice_client(author.voice_channel)
            return

        # move to _verify_vc_perms?
        chperms = author.voice_channel.permissions_for(author.voice_channel.server.me)

        if not chperms.connect:
            self.safe_print("Cannot join channel \"%s\", no permission." % author.voice_channel.name)
            return Response(
                "```Cannot join channel \"%s\", no permission.```" % author.voice_channel.name,
                delete_after=25
            )

        elif not chperms.speak:
            self.safe_print("Will not join channel \"%s\", no permission to speak." % author.voice_channel.name)
            return Response(
                "```Will not join channel \"%s\", no permission to speak.```" % author.voice_channel.name,
                delete_after=25
            )

        player = await self.get_player(author.voice_channel, create=True)

        if player.is_stopped:
            player.play()

        await self.on_player_finished_playing(player)

    async def cmd_pause(self, player):
        """
        Usage:
            {command_prefix}pause

        Pauses playback of the current song.
        """

        if player.is_playing:
            player.pause()

        else:
            raise exceptions.CommandError('Player is not playing.', expire_in=30)

    async def cmd_resume(self, player):
        """
        Usage:
            {command_prefix}resume

        Resumes playback of a paused song.
        """

        if player.is_paused:
            player.resume()
        else:
            raise exceptions.CommandError('Player is not paused.', expire_in=30)

    async def cmd_shuffle(self, channel, player):
        """
        Usage:
            {command_prefix}shuffle

        Shuffles the playlist.
        """

        player.playlist.shuffle()

        cards = [':spades:', ':clubs:', ':hearts:', ':diamonds:']
        hand = await self.send_message(channel, ' '.join(cards))
        await asyncio.sleep(0.6)

        for x in range(4):
            shuffle(cards)
            await self.safe_edit_message(hand, ' '.join(cards))
            await asyncio.sleep(0.6)

        await self.safe_delete_message(hand, quiet=True)
        return Response(":ok_hand:", delete_after=15)

    async def cmd_clear(self, player):
        """
        Usage:
            {command_prefix}clear

        Clears the playlist.
        """

        player.playlist.clear()
        return Response(':put_litter_in_its_place:', delete_after=20)

    async def cmd_top(self, player, queue_id):
        try:
            int(queue_id)
        except:
            return Response("Enter a number.  NUMBER.  That means digits. `15`. Etc.", reply=True, delete_after=8)
        entries = player.playlist.entries
        entry = entries[int(queue_id)-1]
        del entries[int(queue_id)-1]
        entries.appendleft(entry)
        return Response("{} was moved to the top of the queue.".format(entry.title))

    async def cmd_next(self, player, permissions):
        if permissions.instaskip:
            player.skip()
            return Response("The next song will start playing shortly", delete_after=10)
        return Response("You don't have permission to use this command")

    async def cmd_skip(self, player, author, message, voice_channel):
        """
        Usage:
            {command_prefix}skip

        Skips the current song when enough votes are cast, or by the bot owner.
        """

        if player.is_stopped:
            raise exceptions.CommandError("Can't skip! The player is not playing!", expire_in=20)

        if not player.current_entry:
            if player.playlist.peek():
                if player.playlist.peek()._is_downloading:
                    # print(player.playlist.peek()._waiting_futures[0].__dict__)
                    return Response("The next song (%s) is downloading, please wait." % player.playlist.peek().title)

                elif player.playlist.peek().is_downloaded:
                    print("The next song will be played shortly.  Please wait.")
                else:
                    print("Something odd is happening.  "
                          "You might want to restart the bot if it doesn't start working.")
            else:
                print("Something strange is happening.  "
                      "You might want to restart the bot if it doesn't start working.")

        if author == player.current_entry.meta.get('author', None):
            player.skip()  # check autopause stuff here
            await self._manual_delete_check(message)
            return

        num_voice = sum(1 for m in voice_channel.voice_members if not (
            m.deaf or m.self_deaf or m.id in [self.owner.id, self.user.id]))

        num_skips = player.skip_state.add_skipper(author.id, message)

        skips_remaining = min(self.config.skips_required,
                              sane_round_int(num_voice * self.config.skip_ratio_required)) - num_skips

        if skips_remaining <= 0:
            player.skip()  # check autopause stuff here
            return Response(
                'your skip for **{}** was acknowledged.'
                '\nThe vote to skip has been passed.{}'.format(
                    player.current_entry.title,
                    ' Next song coming up!' if player.playlist.peek() else ''
                ),
                reply=True,
                delete_after=20
            )

        else:
            # TODO: When a song gets skipped, delete the old x needed to skip messages
            return Response(
                'your skip for **{}** was acknowledged.'
                '\n**{}** more {} required to vote to skip this song.'.format(
                    player.current_entry.title,
                    skips_remaining,
                    'person is' if skips_remaining == 1 else 'people are'
                ),
                reply=True,
                delete_after=20
            )

    async def cmd_volume(self, player, new_volume=None):
        """
        Usage:
            {command_prefix}volume (+/-)[volume]

        Sets the playback volume. Accepted values are from 1 to 100.
        Putting + or - before the volume will make the volume change relative to the current volume.
        """

        if not new_volume:
            return Response('Current volume: `%s%%`' % int(player.volume * 100), reply=True, delete_after=20)

        relative = False
        if new_volume[0] in '+-':
            relative = True

        try:
            new_volume = int(new_volume)

        except ValueError:
            raise exceptions.CommandError('{} is not a valid number'.format(new_volume), expire_in=20)

        if relative:
            vol_change = new_volume
            new_volume += (player.volume * 100)

        old_volume = int(player.volume * 100)

        if 0 < new_volume <= 100:
            player.volume = new_volume / 100.0

            return Response('updated volume from %d to %d' % (old_volume, new_volume), reply=True, delete_after=20)

        else:
            if relative:
                raise exceptions.CommandError(
                    'Unreasonable volume change provided: {}{:+} -> {}%.  Provide a change between {} and {:+}.'.format(
                        old_volume, vol_change, old_volume + vol_change, 1 - old_volume, 100 - old_volume), expire_in=20)
            else:
                raise exceptions.CommandError(
                    'Unreasonable volume provided: {}%. Provide a value between 1 and 100.'.format(new_volume), expire_in=20)

    async def cmd_queue(self, player):
        """
        Usage:
            {command_prefix}queue

        Prints the current song queue.
        """

        lines = []
        unlisted = 0
        andmoretext = '* ... and %s more*' % ('x' * len(player.playlist.entries))

        if player.current_entry:
            song_progress = str(timedelta(seconds=player.progress)).lstrip('0').lstrip(':')
            song_total = str(timedelta(seconds=player.current_entry.duration)).lstrip('0').lstrip(':')
            prog_str = f"[{song_progress}/{song_total}]"
            if hasattr(player.current_entry, "url"):
                name = f"[{player.current_entry.title}]({player.current_entry.url})"
            else:
                name = player.current_entry.title
            if player.current_entry.meta.get('channel', False) and player.current_entry.meta.get('author', False):
                lines.append(f"Now Playing: **{name}** added by **{player.current_entry.meta['author'].name}** {prog_str}\n")
            else:
                lines.append(f"Now Playing: **{name}** {prog_str}\n")
        for i, item in enumerate(player.playlist, 1):
            if hasattr(item, "url"):
                name = f"[{item.title}]({item.url})"
            else:
                name = item.title
            song_total = str(timedelta(seconds=item.duration)).lstrip('0').lstrip(':')
            if item.meta.get('channel', False) and item.meta.get('author', False):
                nextline = f"{i}. **{name}** added by **{item.meta['author'].name}** [{song_total}]".strip()
            else:
                nextline = f"{i}. **{name}** [{song_total}]".strip()

            currentlinesum = sum(len(x) + 1 for x in lines)  # +1 is for newline char

            if currentlinesum + len(nextline) + len(andmoretext) > DISCORD_MSG_CHAR_LIMIT:
                unlisted += 1
            else:
                lines.append(nextline)

        if unlisted:
            lines.append(f"\n*... and {unlisted} more*")

        if not lines:
            lines.append(f"There are no songs queued! Queue something with `{self.config.command_prefix}play`")

        embed = discord.Embed(title="Queue",
                              description="\n".join(lines),
                              colour=0x3485e7)
        return Response(embed=embed, delete_after=30)

    async def cmd_remove_queue(self, player, remove_id):
        """
        Usage:
            {command_prefix}remove_queue [remove_id]

        Removes the song with id [remove_id] from the queue
        """
        try:
            remove_id = int(remove_id, 0)
        except:
            return Response("Enter a number. NUMBER. That means digits. `15`. Etc.",
                            reply=True,
                            delete_after=8)
        if remove_id > len(player.playlist.entries):
            return Response("Number too big. Please enter a number less than the number of entries queued",
                            reply=True,
                            delete_after=10)
        if remove_id <= 0:
            return Response("Number too small.",
                            reply=True,
                            delete_after=10)
        entry = player.playlist.entries[remove_id-1]
        del player.playlist.entries[remove_id-1]
        return Response(f"Removed `{entry.title.replace('`', '')}` from the queue.")

    async def cmd_clean(self, message, channel, server, author, search_range=50):
        """
        Usage:
            {command_prefix}clean [range]

        Removes up to [range] messages the bot has posted in chat. Default: 50, Max: 1000
        """

        try:
            float(search_range)  # lazy check
            search_range = min(int(search_range), 1000)
        except:
            return Response("enter a number.  NUMBER.  That means digits.  `15`.  Etc.", reply=True, delete_after=8)

        await self.safe_delete_message(message, quiet=True)

        def is_possible_command_invoke(entry):
            valid_call = any(
                entry.content.startswith(prefix) for prefix in [self.config.command_prefix])  # can be expanded
            return valid_call and not entry.content[1:2].isspace()

        delete_invokes = True
        delete_all = channel.permissions_for(author).manage_messages or self.owner.id == author.id

        def check(message):
            if is_possible_command_invoke(message) and delete_invokes:
                return delete_all or message.author == author
            return message.author == self.user

        if channel.permissions_for(server.me).manage_messages:
            deleted = await self.purge_from(channel, check=check, limit=search_range, before=message)
            return Response(f"Cleaned up {len(deleted)} message{'s' if deleted else ''}.", delete_after=15)

        deleted = 0
        async for entry in self.logs_from(channel, search_range, before=message):
            if entry == self.server_specific_data[channel.server]['last_np_msg']:
                continue

            if entry.author == self.user:
                await self.safe_delete_message(entry)
                deleted += 1
                await asyncio.sleep(0.21)

            if is_possible_command_invoke(entry) and delete_invokes:
                if delete_all or entry.author == author:
                    try:
                        await self.delete_message(entry)
                        await asyncio.sleep(0.21)
                        deleted += 1

                    except discord.Forbidden:
                        delete_invokes = False
                    except discord.HTTPException:
                        pass

        return Response('Cleaned up {} message{}.'.format(deleted, 's' * bool(deleted)), delete_after=15)

    async def cmd_listids(self, server, author, leftover_args, cat='all'):
        """
        Usage:
            {command_prefix}listids [categories]

        Lists the ids for various things.  Categories are:
           all, users, roles, channels
        """

        cats = ['channels', 'roles', 'users']

        if cat not in cats and cat != 'all':
            return Response(
                "Valid categories: " + ' '.join(['`%s`' % c for c in cats]),
                reply=True,
                delete_after=25
            )

        if cat == 'all':
            requested_cats = cats
        else:
            requested_cats = [cat] + [c.strip(',') for c in leftover_args]

        data = ['Your ID: %s' % author.id]

        for cur_cat in requested_cats:
            rawudata = None

            if cur_cat == 'users':
                data.append("\nUser IDs:")
                rawudata = ['%s #%s: %s' % (m.name, m.discriminator, m.id) for m in server.members]

            elif cur_cat == 'roles':
                data.append("\nRole IDs:")
                rawudata = ['%s: %s' % (r.name, r.id) for r in server.roles]

            elif cur_cat == 'channels':
                data.append("\nText Channel IDs:")
                tchans = [c for c in server.channels if c.type == discord.ChannelType.text]
                rawudata = ['%s: %s' % (c.name, c.id) for c in tchans]

                rawudata.append("\nVoice Channel IDs:")
                vchans = [c for c in server.channels if c.type == discord.ChannelType.voice]
                rawudata.extend('%s: %s' % (c.name, c.id) for c in vchans)

            if rawudata:
                data.extend(rawudata)

        with BytesIO() as sdata:
            sdata.writelines(d.encode('utf8') + b'\n' for d in data)
            sdata.seek(0)

            # TODO: Fix naming (Discord20API-ids.txt)
            await self.send_file(author, sdata, filename='%s-ids-%s.txt' % (server.name.replace(' ', '_'), cat))

        return Response(":mailbox_with_mail:", delete_after=20)

    async def cmd_perms(self, author, server, permissions):
        """
        Usage:
            {command_prefix}perms

        Sends the user a list of their permissions.
        """

        lines = ['Command permissions in %s\n' % server.name, '```', '```']

        for perm in permissions.__dict__:
            if perm in ['user_list'] or permissions.__dict__[perm] == set():
                continue

            lines.insert(len(lines) - 1, "%s: %s" % (perm, permissions.__dict__[perm]))

        await self.send_message(author, '\n'.join(lines))
        return Response(":mailbox_with_mail:", delete_after=20)


    @owner_only
    async def cmd_setname(self, leftover_args, name):
        """
        Usage:
            {command_prefix}setname name

        Changes the bot's username.
        Note: This operation is limited by discord to twice per hour.
        """

        name = ' '.join([name, *leftover_args])

        try:
            await self.edit_profile(username=name)
        except Exception as e:
            raise exceptions.CommandError(e, expire_in=20)

        return Response(":ok_hand:", delete_after=20)

    @owner_only
    async def cmd_setnick(self, server, channel, leftover_args, nick):
        """
        Usage:
            {command_prefix}setnick nick

        Changes the bot's nickname.
        """

        if not channel.permissions_for(server.me).change_nickname:
            raise exceptions.CommandError("Unable to change nickname: no permission.")

        nick = ' '.join([nick, *leftover_args])

        try:
            await self.change_nickname(server.me, nick)
        except Exception as e:
            raise exceptions.CommandError(e, expire_in=20)

        return Response(":ok_hand:", delete_after=20)

    @owner_only
    async def cmd_setavatar(self, message, url=None):
        """
        Usage:
            {command_prefix}setavatar [url]

        Changes the bot's avatar.
        Attaching a file and leaving the url parameter blank also works.
        """

        if message.attachments:
            thing = message.attachments[0]['url']
        else:
            thing = url.strip('<>')

        try:
            with aiohttp.Timeout(10):
                async with self.aiosession.get(thing) as res:
                    await self.edit_profile(avatar=await res.read())

        except Exception as e:
            raise exceptions.CommandError("Unable to change avatar: %s" % e, expire_in=20)

        return Response(":ok_hand:", delete_after=20)

    async def cmd_disconnect(self, server):
        """
        Usage:
            {command_prefix}disconnect
        Disconnect from the voice channel
        """
        await self.disconnect_voice_client(server)
        return Response(":hear_no_evil:", delete_after=20)

    async def cmd_restart(self, channel):
        """
        Usage:
            {command_prefix}restart
        Restart the bot.
        """
        await self.safe_send_message(channel, ":wave:")
        await self.disconnect_all_voice_clients()
        raise exceptions.RestartSignal

    async def cmd_update(self, update=None):
        """Check for updates and apply them if any"""
        if update is None:
            update = {"commits": [{"message": "Manually triggered"}]}
        result = subprocess.run(['git', 'pull'],
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)
        self.should_restart |= result.stdout != b'Already up-to-date.\n'
        if self.should_restart:
            max_time = timedelta(seconds=0)
            for player in self.players.values():
                playlist = player.playlist
                max_time = max((max_time, await playlist.estimate_time_until(len(playlist.entries), player)))
            description = f"Scheduled a restart after the queue is empty, restart estimated to be in {max_time}."
        else:
            description = "Already up to date; not restarting"
        embed = discord.Embed(title=f"Running an update",
                              description=description,
                              colour=0x3485e7)
        embed.add_field(name="Output", value=result.stdout.decode("utf-8") or "None", inline=False)
        embed.add_field(name="Errors", value=result.stderr.decode("utf-8") or "None", inline=False)
        embed.add_field(name="Message", value="\n".join(commit["message"] for commit in update["commits"]), inline=False)
        return Response(embed=embed)

    async def cmd_shutdown(self, channel):
        """
        Usage:
            {command_prefix}shutdown
        Shut down the bot without a restart. Please do not use unless I'm doing something *very* wrong."""
        await self.safe_send_message(channel, ":wave:")
        await self.disconnect_all_voice_clients()
        raise exceptions.TerminateSignal

    async def cmd_agree(self, author):
        """
        Usage:
            {command_prefix}agree
        Agree to the terms of usage for the bot and allows access to commands
        """
        await self.agree(author.id)
        return Response("You have agreed to this bot storing information about you.\n"
                        "You may now use commands",
                        delete_after=10,
                        reply=True)

    async def agree(self, id):
        self.agree_list.add(id)
        async with aiofiles.open(self.agreelist_file, "w") as agreelist_f:
            await agreelist_f.write(json.dumps(list(self.agree_list)))

    async def cmd_disagree(self, author):
        """
        Usage:
            {command_prefix}disagree
        Disagree to the terms of usage for the bot and disallows access to commands. Also removes all stored data
        about you. You must not any 'fresh' roles to perform this command"""
        jobs = self.jobstore.get_all_jobs()
        for job in jobs:
            user_id = job.id.split()[-1]
            if author.id == user_id:
                return Response("You still have the fresh role.\n"
                                "You may not opt out of this bot storing information about you until a week passes from gaining the Ambassador role.\n"
                                "You may only opt out by leaving the server at the moment.",
                                delete_after=10,
                                reply=True)
        self.agree_list.remove(author.id)
        async with aiofiles.open(self.agreelist_file, "w") as agreelist_f:
            await agreelist_f.write(json.dumps(list(self.agree_list)))
        return Response("This bot will no longer store information about you.",
                        delete_after=10,
                        reply=True)

    async def alt_cmd_iam(self, author, role_name, server):
        for role in server.roles:
            if role.name.lower() == role_name.lower():
                if role.id not in self.config.giveable_roles:
                    return Response("{} is not in the whitelisted group of roles".format(
                                        role.name
                                    ),
                                    delete_after=20,
                                    reply=True)
                break
        else:
            return Response("{} was not found".format(role_name),
                            delete_after=20,
                            reply=True)
        await self.add_roles(author, role)
        return Response("you are now {}".format(role_name),
                        delete_after=20,
                        reply=True)

    async def alt_cmd_iamn(self, author, role_name, server):
        for role in server.roles:
            if role.name.lower() == role_name.lower():
                if role.id not in self.config.giveable_roles:
                    return Response("{} is not in the whitelisted group of roles".format(
                                        role.name
                                    ),
                                    delete_after=20,
                                    reply=True)
                break
        else:
            return Response("{} was not found".format(role_name),
                            delete_after=20,
                            reply=True)
        await self.remove_roles(author, role)
        return Response("you are no longer {}".format(role_name),
                        delete_after=20,
                        reply=True)

    async def on_message(self, message):
        await self.wait_until_ready()
        message_content = message.content.strip()
        if message.channel.is_private and message.author.id != self.user.id:
            if self.survey_channel:
                await self.handle_survey(message.author, message.channel, message_content)

        uses_alternate = message_content.startswith(self.config.alternate_command_prefix)
        if uses_alternate:
            other_bot = message.server.get_member(user_id=self.config.other_bot)
            if other_bot:
                if other_bot.status != Status.offline:
                    return
        if not message_content.startswith(self.config.command_prefix):
            if not uses_alternate:
                return

        if uses_alternate:
            if self.config.alternate_bound_channels and message.channel.id not in self.config.alternate_bound_channels and not message.channel.is_private:
                return
        else:
            if self.config.bound_channels and message.channel.id not in self.config.bound_channels and not message.channel.is_private:
                return  # if I want to log this I just move it under the prefix check

        try:
            command, *args = shlex.split(message_content)  # Uh, doesn't this break prefixes with spaces in them (it doesn't, config parser already breaks them)
        except ValueError:
            command, *args = message_content.split()
        command = command[len(self.config.command_prefix):].lower().strip()

        if (message.author.id not in self.agree_list) and command != "agree":
            embed = discord.Embed(tile="Terms of Service Update",
                                  description="This bot stores information about users in order to function.\n"
                                    "In order to use any commands with this bot, you have to explicitly agree to this bot storing information about your Discord account.\n"
                                    "This is in order to comply with the new terms of service for bot developers.\n"
                                    "Type {}agree to use commands for this bot.".format(self.config.command_prefix),
                                  colour=0x3485e7)
            await self.safe_send_message(message.channel,
                                         message.author.mention,
                                         embed=embed,
                                         expire_in=30 if self.config.delete_messages else 0,
                                         also_delete=message if self.config.delete_invoking else None)
            return

        if message.channel.is_private:
            if message.author.id in [self.owner.id, 186955497671360512, 104445625562570752, 152303040970489856, 279857235444760586, 140419299092201472]:
                await self.handle_dms(command, args, message)
            elif command != 'joinserver':
                await self.send_message(message.channel, 'You cannot use this bot in private messages.')
                return
            else:
                self.safe_print("[User blacklisted] {0.id}/{0.name} ({1})".format(message.author, message_content))
                return
        else:
            self.safe_print("[Command] {0.id}/{0.name} ({1})".format(message.author, message_content))
        if uses_alternate:
            handler = getattr(self, 'alt_cmd_%s' % command, None)
        else:
            handler = getattr(self, 'cmd_%s' % command, None)
        if not handler:
            return

        user_permissions = self.permissions.for_user(message.author)

        argspec = inspect.signature(handler)
        params = argspec.parameters.copy()

        # noinspection PyBroadException
        try:
            if user_permissions.ignore_non_voice and command in user_permissions.ignore_non_voice:
                await self._check_ignore_non_voice(message)

            handler_kwargs = {}
            if params.pop('message', None):
                handler_kwargs['message'] = message

            if params.pop('channel', None):
                handler_kwargs['channel'] = message.channel

            if params.pop('author', None):
                handler_kwargs['author'] = message.author

            if params.pop('server', None):
                handler_kwargs['server'] = message.server

            if params.pop('player', None):
                handler_kwargs['player'] = await self.get_player(message.channel)

            if params.pop('permissions', None):
                handler_kwargs['permissions'] = user_permissions

            if params.pop('user_mentions', None):
                handler_kwargs['user_mentions'] = list(map(message.server.get_member, message.raw_mentions))

            if params.pop('channel_mentions', None):
                handler_kwargs['channel_mentions'] = list(map(message.server.get_channel, message.raw_channel_mentions))

            if params.pop('voice_channel', None):
                handler_kwargs['voice_channel'] = message.server.me.voice_channel

            if params.pop('leftover_args', None):
                handler_kwargs['leftover_args'] = args

            args_expected = []
            for key, param in list(params.items()):
                doc_key = '[%s=%s]' % (key, param.default) if param.default is not inspect.Parameter.empty else key
                args_expected.append(doc_key)

                if not args and param.default is not inspect.Parameter.empty:
                    params.pop(key)
                    continue

                if args:
                    arg_value = args.pop(0)
                    handler_kwargs[key] = arg_value
                    params.pop(key)

            if message.author.id != self.owner.id:
                if user_permissions.command_whitelist and command not in user_permissions.command_whitelist:
                    raise exceptions.PermissionsError(
                        "This command is not enabled for your group (%s)." % user_permissions.name,
                        expire_in=20)

                elif user_permissions.command_blacklist and command in user_permissions.command_blacklist:
                    raise exceptions.PermissionsError(
                        "This command is disabled for your group (%s)." % user_permissions.name,
                        expire_in=20)

            if params:
                docs = getattr(handler, '__doc__', None)
                if not docs:
                    docs = 'Usage: {}{} {}'.format(
                        self.config.command_prefix,
                        command,
                        ' '.join(args_expected)
                    )

                docs = '\n'.join(l.strip() for l in docs.split('\n'))
                await self.safe_send_message(
                    message.channel,
                    '```\n%s\n```' % docs.format(command_prefix=self.config.command_prefix),
                    expire_in=60
                )
                return

            response = await handler(**handler_kwargs)
            if response and isinstance(response, Response):
                for content in paginate(response.content):
                    if response.reply:
                        content = '%s, %s' % (message.author.mention, content)
                    await self.safe_send_message(message.channel,
                                                 content,
                                                 embed=response.embed,
                                                 expire_in=response.delete_after if self.config.delete_messages else 0,
                                                 also_delete=message if self.config.delete_invoking else None)

        except (exceptions.CommandError, exceptions.HelpfulError, exceptions.ExtractionError) as e:

            expirein = e.expire_in if self.config.delete_messages else None
            alsodelete = message if self.config.delete_invoking else None

            await self.safe_send_message(message.channel,
                                         '```\n%s\n```' % e.message,
                                         expire_in=expirein,
                                         also_delete=alsodelete)
        except exceptions.Signal:
            raise
        except Exception:
            traceback.print_exc()
            if self.config.debug_mode:
                await self.safe_send_message(message.channel, '```\n%s\n```' % traceback.format_exc())

    async def check_new_members(self):
        for server in self.servers:
            fresh = discord.utils.get(server.roles, name="Fresh")
            ambassador = discord.utils.get(server.roles, name="Ambassador")
            for member in server.members:
                if len(set(member.roles) & {fresh, ambassador}) == 2:
                    await self.schedule_removal(member, complain=False, days=7)

    async def on_member_update(self, before, after):
        before_roles = [role.name for role in before.roles]
        after_roles = [role.name for role in after.roles]
        if "Ambassador" not in before_roles and "Ambassador" in after_roles:
            #Ambassador was just given
            await self.replace_roles(after, discord.utils.get(after.roles, name="Ambassador"),
                                            discord.utils.get(after.server.roles, name="Fresh"))
            await self.agree(after.id)
            await self.schedule_removal(after, days=7)
        if "Enlightened" in after_roles and "Uninformed" in after_roles:
            role = discord.utils.get(after.roles, name="Uninformed")
            await self.remove_roles(after, role)

    async def handle_dms(self, command, args, message):
        awsw = discord.utils.get(self.servers, name="AwSW Fan Discord")
        if command == "echo":
            channel = discord.utils.get(awsw.channels, name=args[0])
            await self.send_message(channel, args[1], tts="tts" in args)
        elif command in ["edit", "delete"]:
            channel = discord.utils.get(awsw.channels, name=args[0])
            async for message in self.logs_from(channel):
                try:
                    if message.id == args[1]:
                        if command == "edit":
                            await self.edit_message(message, args[2])
                        elif command == "delete":
                            await self.delete_message(message)
                except:
                    self.safe_print("\n".join((message.content, str(message.author), channel.name)))
                    traceback.print_exc()
        elif command == "emote":
            emote = discord.utils.get(self.get_all_emojis(), name=args[0])
            if not emote:
                await self.send_message(message.channel, "Could not find emote")
                return
            await self.send_message(message.channel, str(emote.server))
            channel = discord.utils.get(self.get_all_channels(), name=args[1])
            async for channel_message in self.logs_from(channel):
                if channel_message.id == args[2]:
                    await self.add_reaction(channel_message, emote)
                    await asyncio.sleep(5)
                    await self.remove_reaction(channel_message, emote, channel.server.me)
            return

    async def cmd_remove_fresh(self, message):
        for user in message.mentions:
            await self.remove_fresh(user.id)

    async def schedule_removal(self, member, message="Scheduled the removal of {} from Fresh in 7 days", complain=True, **kwargs):
        if member.id in [job.id.split(" ")[-1] for job in self.jobstore.get_all_jobs()]:
            if complain:
                await self.safe_send_message(self.report_channel, "{} already scheduled for removal".format(member.mention))
            return
        await self.safe_send_message(self.report_channel, message.format(member.mention))
        self.scheduler.add_job(call_schedule,
                               'date',
                               id=self.get_id_args(self.remove_fresh, member.id),
                               run_date=get_next(**kwargs),
                               kwargs={"func": "remove_fresh",
                                       "arg": member.id})

    def job_missed(self, event):
        asyncio.ensure_future(call_schedule(*event.job_id.split(" ")))

    @staticmethod
    def get_id_args(func, arg):
        return "{} {}".format(func.__name__, arg)

    async def remove_fresh(self, user_id):
        server = discord.utils.get(self.servers, name="AwSW Fan Discord")
        try:
            user = discord.utils.get(server.members, id=user_id)
            role = discord.utils.get(server.roles, name="Fresh")
            if user and role:
                if role in user.roles:
                    await self.remove_roles(user, role)
                    await self.safe_send_message(self.report_channel, "Removed the fresh role from {}".format(user.mention))
                else:
                    await self.safe_send_message(self.report_channel, "{} has already had Fresh removed from them".format(user.mention))
            else:
                await self.safe_send_message(self.report_channel, "Something went wrong removing the fresh role from user: {} (user not found or Fresh role not found)".format(user_id))
        except Exception:
            traceback.print_exc()
            if self.config.debug_mode:
                await self.safe_send_message(self.report_channel, '```\n%s\n```' % traceback.format_exc())
            if user:
                await self.safe_send_message(self.report_channel, "Failed to remove the fresh role from {}".format(user.mention))
            else:
                await self.safe_send_message(self.report_channel, "Failed to remove the fresh role from {}".format(user_id))

    async def cmd_fresh_status(self, channel, server):
        jobs = self.jobstore.get_all_jobs()
        rtn = []
        for job in jobs:
            user_id, next_run_time = job.id.split()[-1], job.next_run_time
            user = discord.utils.get(server.members, id=user_id)
            if user:
                rtn.append("{}: {}".format(
                    user.mention,
                    next_run_time.strftime("%Y-%m-%d %H:%M:%S %z")))
            else:
                rtn.append("{}: {} (Left server)".format(
                    (await self.get_user_info(user_id)).mention,
                    next_run_time.strftime("%Y-%m-%d %H:%M:%S %z")
                ))
        await self.safe_send_message(channel, "\n".join(rtn))

    async def cmd_start_survey(self, channel_mentions):
        if channel_mentions[0] == self.survey_channel:
            return Response("There is already a survey running in this channel")
        self.survey_channel = channel_mentions[0]
        return Response("Started a survey, responses will be copied to {}".format(self.survey_channel.mention))

    async def cmd_end_survey(self, channel):
        self.survey_channel = None
        await self.safe_send_message(channel, "Stopped all surveys")

    async def handle_survey(self, user, channel, message_content):
        if await self.ask_yn(channel,
                             "Do you want your username to be visible to staff?\n"
                             "Your feedback will not be sent until a reaction is added"):
            message_content = user.mention + "`~~~`" + message_content
        else:
            message_content = "`~~~`" + message_content
        await self.safe_send_message(self.survey_channel, message_content)
        await self.safe_send_message(channel, "Your feedback was sent")

    async def ask_yn(self, channel, question, check=lambda message: True):
        message = await self.safe_send_message(channel, question)
        await self.add_reaction(message, "🇾")
        await self.add_reaction(message, "🇳")
        react = await self.wait_for_reaction(["🇾", "🇳"],
                                             message=message,
                                             check=lambda reaction, user: check(user) and user.id != self.user.id)
        return react.reaction.emoji == "🇾"

    async def cmd_unschedule(self, channel, message):
        rtn = []
        jobs = {job.id.split()[-1]: job for job in self.jobstore.get_all_jobs()}
        for user in message.mentions:
            if user.id in jobs:
                jobs[user.id].remove()
                rtn.append("Unscheduled {}".format(user.mention))
            else:
                rtn.append("{} isn't scheduled for removal".format(user.mention))
        await self.safe_send_message(channel, "\n".join(rtn))

    async def on_member_join(self, member):
        embed = discord.Embed(title="Joined the server",
                              description="Account Created: {}\n"
                                          "Ping: {}\n"
                                          "ID: {}".format(member.created_at,
                                                          member.mention,
                                                          member.id),
                              colour=0x00CC00)
        embed.set_author(name=member.name,
                         icon_url=member.avatar_url)
        if member.server.id == self.report_channel.server.id:
            await self.send_message(self.report_channel, embed=embed)

    async def on_member_remove(self, member):
        embed = discord.Embed(title="Left the server",
                              description="Ping: {}".format(member.mention),
                              colour=0xCC0000)
        embed.set_author(name=member.name,
                         icon_url=member.avatar_url)
        if member.server.id == self.report_channel.server.id:
            await self.send_message(self.report_channel, embed=embed)

    async def on_voice_state_update(self, before, after):
        if not all([before, after]):
            return
        if before.voice_channel == after.voice_channel:
            return
        if before.server.id not in self.players:
            return
        my_voice_channel = after.server.me.voice_channel  # This should always work, right?

        if not my_voice_channel:
            return
        if my_voice_channel not in (before.voice_channel, after.voice_channel):
            return  # Not my channel

        auto_paused = self.server_specific_data[after.server]['auto_paused']
        player = await self.get_player(my_voice_channel)

        if after == after.server.me and after.voice_channel:
            player.voice_client.channel = after.voice_channel

        if not self.config.auto_pause:
            return

        if sum(1 for m in my_voice_channel.voice_members if m != after.server.me):
            if auto_paused and player.is_paused:
                print("[config:autopause] Unpausing")
                self.server_specific_data[after.server]['auto_paused'] = False
                player.resume()
        else:
            if not auto_paused and player.is_playing:
                print("[config:autopause] Pausing")
                self.server_specific_data[after.server]['auto_paused'] = True
                player.pause()

    async def on_server_update(self, before: discord.Server, after: discord.Server):
        if before.region != after.region:
            self.safe_print("[Servers] \"%s\" changed regions: %s -> %s" % (after.name, before.region, after.region))

            await self.reconnect_voice_client(after)

    def parse_playlist(self, playlist):
        rtn = []
        for song_url in playlist:
            local = not song_url.startswith("http")
            if local:
                if "*" in song_url:
                    rtn.extend(sort_songs(glob.glob(song_url)))
                else:
                    rtn.append(song_url)
            else:
                rtn.append(song_url)
        return rtn


async def call_schedule(func=None, arg=None, user_id=None):
    if arg is None:
        await MusicBot.bot.remove_fresh(user_id)
        return
    await getattr(MusicBot.bot, func)(arg)

if __name__ == '__main__':
    bot = MusicBot()
    bot.run()
