"""Run the API alone: `python -m server`.

No window, no webview. This is the mode `npm run dev` proxies against and the
mode to use when the question is "does the endpoint work", not "does the app
look right".
"""

import uvicorn

from atlas import config
from server.app import app

if __name__ == "__main__":
    # 127.0.0.1, not 0.0.0.0. There is no auth in this application — it is one
    # user on one machine — so binding it to anything reachable would publish
    # the archive and the credentials-shaped endpoints to the network.
    uvicorn.run(app, host="127.0.0.1", port=config.PORT, log_level="warning",
                access_log=False)
