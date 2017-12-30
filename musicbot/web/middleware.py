from aiohttp.web import HTTPForbidden, HTTPExpectationFailed


async def login_middleware(app, handler):

    async def middleware(request):
        if not (request.match_info.route.name or "").endswith("no_login"):
            access = request.cookies["access_token"]
            refresh = request.cookies["refresh_token"]
            server_id = request.cookies["server_id"]
            async with app["oauth2_handler"].get_oauth2_http(access, refresh) as http:
                user_info = await http.get_user_info("@me")
                access_token = http.token.replace("Bearer ", "", 1)
            user_id = user_info["id"]
            server = app["bot"].get_server(server_id)
            if not server:
                raise HTTPExpectationFailed("Bot is not a member of the server")
            member = server.get_member(user_id)
            if not member:
                raise HTTPForbidden("Not a member of the server")

            permissions = member.server_permissions
            if not permissions.manage_server:
                raise HTTPForbidden("No manage server")
            request.server = server
            request.user_info = user_info
            request.user_id = user_id
            response = await handler(request)
            response.set_cookie("access_token", access_token)
        else:
            response = await handler(request)

        return response

    return middleware