from discord.http import HTTPClient
from discord.errors import HTTPException


class Oauth2:
    def __init__(self, bot):
        self.bot = bot
        self.loop = self.bot.loop
        self.url = self.bot.config.url

        self.client_id = self.bot.user.id
        self.client_secret = self.bot.config.client_secret
        self.redirect_uri = "https://pyke.catbus.co.uk/apps/zhong/setup"

    async def refresh_token(self, refresh_token):
        data = {
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'grant_type': 'refresh_token',
            'refresh_token': refresh_token,
            'redirect_uri': self.redirect_uri
        }
        async with self.bot.aiosession.post("https://discordapp.com/api/v6/oauth2/token",
                                            data=data,
                                            headers={"Content-Type": "application/x-www-form-urlencoded"}) as res:
            json = await res.json()
            return json["refresh_token"], json["access_token"]

    async def exchange_token(self, access_token):
        data = {
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'grant_type': 'authorization_code',
            'code': access_token,
            'redirect_uri': self.redirect_uri
        }
        async with self.bot.aiosession.post("https://discordapp.com/api/v6/oauth2/token",
                                            data=data,
                                            headers={"Content-Type": "application/x-www-form-urlencoded"}) as res:
            json = await res.json()
            return json

    def get_oauth2_http(self, access_token, refresh_token):
        return Oauth2Http(self, access_token, refresh_token)


class Oauth2Http:
    def __init__(self, oauth2, access_token, refresh_token):
        self.oauth2 = oauth2
        self.refresh_token = refresh_token
        self.access_token = "Bearer "+access_token
        self.loop = oauth2.loop

    async def __aenter__(self):
        self.http = HTTPClient(None, loop=self.loop)
        self.http.token = self.access_token

        orig_request = self.http.request

        async def request(route, *, header_bypass_delay=None, **kwargs):
            try:
                return await orig_request(route, header_bypass_delay=header_bypass_delay, **kwargs)
            except HTTPException as e:
                if e.response.status == 401:
                    self.refresh_token, self.access_token = await self.oauth2.refresh_token(self.refresh_token)
                    self.http.token = "Bearer "+self.access_token
                    return await orig_request(route, header_bypass_delay=header_bypass_delay, **kwargs)
                raise

        self.http.request = request

        return self.http

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.http.close()
        return False
