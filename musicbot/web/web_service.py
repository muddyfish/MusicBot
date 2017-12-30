import aiohttp_jinja2
from jinja2 import FileSystemLoader
from aiohttp import web
import asyncio

from musicbot.permissions import PermissionsExtension
from musicbot.web.middleware import login_middleware


class WebService:
    def __init__(self, bot, host, port):
        self.bot = bot
        self.app = web.Application(middlewares=[login_middleware])
        self.loop = self.bot.loop
        self.url = self.bot.config.url

        aiohttp_jinja2.setup(self.app, loader=FileSystemLoader("./template/"))
        self.app.router.add_get('/static/css/generic.css', self.render_generic_css, name="css_no_login")
        self.app.router.add_static("/static/", "./static", name="static_no_login")

        self.app.router.add_post('/update', self.handle_update, name="update_post_no_login")
        self.app.router.add_get('/update', self.handle_update, name="update_get_no_login")
        self.app.router.add_get('/setup', self.handle_setup_server, name="setup_no_login")
        self.app.router.add_post('/setup', self.handle_post_setup_server)
        self.app.router.add_get('/set_permissions', self.handle_set_permissions)

        self.app["bot"] = self.bot
        self.app["oauth2_handler"] = self.bot.oauth2_handler

        self.handler = self.app.make_handler()
        self.web_server = self.loop.create_server(self.handler, host, port)

        self.client_id = self.bot.user.id
        self.client_secret = self.bot.config.client_secret

        self.srv = asyncio.ensure_future(self.web_server, loop=self.loop)
        self._startup = asyncio.ensure_future(self.app.startup(), loop=self.loop)

    async def handle_setup_server(self, request):
        args = request.GET
        code = args["code"]

        context = {"guild": self.bot.get_server("277442894904819714"),
                   "base_url": self.url,
                   "specific_info": self.bot.get_server_db("277442894904819714")}

        response = aiohttp_jinja2.render_template("setup.jinja2", request, context)
        response.set_cookie("server_id", "277442894904819714")
        response.set_cookie("access_token", "I11MLFDKjjK6I7ksGSQ4Wtxd9ACKXK")
        response.set_cookie("refresh_token", "DOedlHdLUQakpDOznkfddOpbqq8GmV")
        return response

        token = await self.exchange_token(code)
        guild = token["guild"]
        guild_id = guild["id"]
        assert guild_id == args["guild_id"]
        if guild_id not in (server.id for server in self.bot.servers):
            future = asyncio.Future(loop=self.loop)
            self.bot.server_listeners.append((lambda server: server.id == guild_id, future))
            server = await asyncio.wait_for(future, 0, loop=self.loop)
        else:
            server = next(server for server in self.bot.servers if server.id == guild_id)
        print(token, server)
        await self.bot.leave_server(self.bot.get_server(guild_id))
        return {"guild": server}

    async def handle_post_setup_server(self, request):
        data = await request.post()
        server_db = request.app["bot"].get_server_db(request.server.id)
        for channel in ("report_id", "debug_report_id", "warning_id", "survey_id", "autoconnect_id", "bind_to_channels"):
            assert isinstance(data[channel], str)
            setattr(server_db, channel, data[channel])
        self.bot.session.commit()
        return await self.handle_set_permissions(request)

    @aiohttp_jinja2.template("set_permissions.jinja2")
    async def handle_set_permissions(self, request):

        server = request.server
        commands = [name for name, func in request.app["bot"].get_commands().items() if not hasattr(func, "owner_only")]
        return {"commands": commands,
                "guild": server,
                "permissions_list": PermissionsExtension}

    async def render_generic_css(self, request):
        response = web.Response(status=200)
        response.content_type = 'text/css'
        response.charset = "utf-8"
        response.text = aiohttp_jinja2.render_string("generic.css", request, {"base_url": self.url})
        return response

    async def handle_update(self, request):
        update = await request.json()
        response = await self.bot.cmd_update(update)
        for server, data in self.bot.server_specific_data.items():
            await self.bot.safe_send_message(data["report_channel_dj"],
                                         "",
                                         embed=response.embed)
        return web.Response(text="")