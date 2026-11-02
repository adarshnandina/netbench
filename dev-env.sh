# Set up this terminal window for NetBench. Run it from the project folder:
#
#     source dev-env.sh          use the local backend (Docker on this Mac)
#     source dev-env.sh azure    use the Azure VM (settings in .env.azure)
#
# It must be "source", not "sh dev-env.sh" or "./dev-env.sh": those run the
# script in a separate child process, and its exports would vanish when that
# process ends. "source" runs the lines in YOUR shell, so the variables stay.

if [ ! -f .env.example ]; then
    echo "Run this from the NetBench project folder."
    return 1
fi
[ -d .venv ] && source .venv/bin/activate

if [ "$1" = "azure" ]; then
    if [ ! -f .env.azure ]; then
        echo "No .env.azure yet. Create it with NETBENCH_API_URL, NETBENCH_API_KEY and NETBENCH_SERVER lines."
        return 1
    fi
    # Read KEY=value lines from .env.azure (no spaces around =).
    export NETBENCH_API_URL="$(grep '^NETBENCH_API_URL=' .env.azure | cut -d= -f2-)"
    export NETBENCH_API_KEY="$(grep '^NETBENCH_API_KEY=' .env.azure | cut -d= -f2-)"
    export NETBENCH_SERVER="$(grep '^NETBENCH_SERVER=' .env.azure | cut -d= -f2-)"
else
    export NETBENCH_API_URL="http://localhost:8000"
    export NETBENCH_API_KEY="$(grep '^NETBENCH_API_KEY=' .env | cut -d= -f2-)"
    export NETBENCH_SERVER="127.0.0.1"
fi

echo "NetBench environment ready (${1:-local}):"
echo "  python : $(which python)"
echo "  API    : $NETBENCH_API_URL"
echo "  server : $NETBENCH_SERVER"
echo "  key    : ${NETBENCH_API_KEY:0:4}… (${#NETBENCH_API_KEY} characters)"
