import asyncio
import os
import sys

import decky

# Decky starts plugins without our user's environment. Point HOME at the deck user so the
# plugin shares SedotDeck's config (~/.config/sedotdeck) and install folder (~/Applications)
# with the desktop app, and give urllib the system CA bundle for HTTPS.
os.environ["HOME"] = decky.DECKY_USER_HOME
os.environ.pop("XDG_CONFIG_HOME", None)
for _ca in ("/etc/ssl/certs/ca-certificates.crt", "/etc/ca-certificates/extracted/tls-ca-bundle.pem"):
    if not os.environ.get("SSL_CERT_FILE") and os.path.exists(_ca):
        os.environ["SSL_CERT_FILE"] = _ca
sys.path.insert(0, os.path.join(decky.DECKY_PLUGIN_DIR, "py_modules"))

import sedotdeck  # noqa: E402  (bundled copy of sedotdeck.py)

# What the Game Mode panel may do. Launching apps, settings (token) and quitting stay in the desktop UI.
# Adding to Steam goes through SteamClient in the frontend: Steam is running in Game Mode.
ALLOWED = {"/api/state", "/api/check", "/api/update", "/api/add", "/api/remove", "/api/rollback",
           "/api/track_found", "/api/skip", "/api/launch_choices", "/api/set_launch"}
CHECK_EVERY = 6 * 3600


class Plugin:
    async def call(self, path: str, body: dict = None):
        if path not in ALLOWED:
            raise ValueError("Not available from Game Mode: %s" % path)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self.srv.handle, path, body or {})

    async def _background_checks(self):
        await asyncio.sleep(90)  # let Steam finish starting up first
        last_notified = None
        while True:
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self.srv.handle, "/api/check", {})
                while self.srv.busy():
                    await asyncio.sleep(2)
                state = await loop.run_in_executor(None, self.srv.handle, "/api/state", {})
                names = sorted(a["name"] for a in state["apps"] if a["has_update"] and a["installed"])
                if names and names != last_notified:
                    await decky.emit("updates_available", names)
                last_notified = names
            except Exception as e:
                decky.logger.warning("Background check failed: %s", e)
            await asyncio.sleep(CHECK_EVERY)

    async def _main(self):
        self.srv = sedotdeck.Server()
        self.task = asyncio.get_running_loop().create_task(self._background_checks())
        decky.logger.info("SedotDeck %s loaded, config at %s", sedotdeck.VERSION, sedotdeck.CONFIG_FILE)

    async def _unload(self):
        task = getattr(self, "task", None)
        if task:
            task.cancel()

    async def _uninstall(self):
        pass
