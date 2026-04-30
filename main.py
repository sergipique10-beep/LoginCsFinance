import re
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse

from settings import BASE_URL, FRONTEND_URL, JWT_SECRET

app = FastAPI(title="Steam Login")

STEAM_OPENID_URL = "https://steamcommunity.com/openid/login"


@app.get("/")
def root():
    return {"message": "Server running. Go to /auth/steam to login with Steam."}


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

    validation_params = {**query_params, "openid.mode": "check_authentication"}
    async with httpx.AsyncClient() as client:
        validation = await client.post(STEAM_OPENID_URL, data=validation_params)

    if "is_valid:true" not in validation.text:
        raise HTTPException(status_code=401, detail="Steam authentication failed")

    claimed_id = query_params.get("openid.claimed_id", "")
    match = re.search(r"/openid/id/(\d+)$", claimed_id)
    if not match:
        raise HTTPException(status_code=400, detail="Could not parse Steam ID")

    steam_id = match.group(1)

    token = jwt.encode(
        {
            "steam_id": steam_id,
            "exp": datetime.now(timezone.utc) + timedelta(hours=24),
        },
        JWT_SECRET,
        algorithm="HS256",
    )

    return RedirectResponse(url=f"{FRONTEND_URL}/auth/callback?token={token}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8001, reload=True)
