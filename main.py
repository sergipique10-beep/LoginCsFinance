import re
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse

from settings import BASE_URL, STEAM_API_KEY

app = FastAPI(title="Steam Login")


@app.get("/")
def root():
    return {"message": "Server running. Go to /auth/steam to login with Steam."}


STEAM_OPENID_URL = "https://steamcommunity.com/openid/login"
STEAM_API_URL = "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/"


@app.get("/auth/steam", summary="Redirect to Steam login")
def steam_login():
    params = {
        "openid.ns": "http://specs.openid.net/auth/2.0",
        "openid.mode": "checkid_setup",
        "openid.return_to": f"{BASE_URL}/auth/steam/callback",
        "openid.realm": BASE_URL,
        "openid.identity": "http://specs.openid.net/auth/2.0/identifier_select",
        "openid.claimed_id": "http://specs.openid.net/auth/2.0/identifier_select",
    }
    return RedirectResponse(url=f"{STEAM_OPENID_URL}?{urlencode(params)}")


@app.get("/auth/steam/callback", summary="Steam OpenID callback")
async def steam_callback(request: Request):
    query_params = dict(request.query_params)

    # Ask Steam to validate the response
    validation_params = {**query_params, "openid.mode": "check_authentication"}
    async with httpx.AsyncClient() as client:
        validation = await client.post(STEAM_OPENID_URL, data=validation_params)

    if "is_valid:true" not in validation.text:
        raise HTTPException(status_code=401, detail="Steam authentication failed")

    # Extract 64-bit Steam ID from claimed_id
    claimed_id = query_params.get("openid.claimed_id", "")
    match = re.search(r"/openid/id/(\d+)$", claimed_id)
    if not match:
        raise HTTPException(status_code=400, detail="Could not parse Steam ID")

    steam_id = match.group(1)

    # Optionally fetch the public profile if an API key is set
    profile = {}
    if STEAM_API_KEY:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                STEAM_API_URL,
                params={"key": STEAM_API_KEY, "steamids": steam_id},
            )
        players = resp.json().get("response", {}).get("players", [])
        profile = players[0] if players else {}

    return {"steam_id": steam_id, "profile": profile}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8001, reload=True)
