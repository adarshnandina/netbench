# Set up this terminal window for NetBench. Run it from the project folder with:
#
#     source dev-env.sh
#
# It must be "source", not "sh dev-env.sh" or "./dev-env.sh": those run the
# script in a separate child process, and its exports would vanish when that
# process ends. "source" runs the lines in YOUR shell, so the variables stay.

if [ ! -f .env ]; then
    echo "No .env here. Run this from the NetBench project folder."
    return 1
fi

[ -d .venv ] && source .venv/bin/activate

export NETBENCH_API_URL="${NETBENCH_API_URL:-http://localhost:8000}"
export NETBENCH_API_KEY="$(grep '^NETBENCH_API_KEY=' .env | cut -d= -f2)"

echo "NetBench environment ready:"
echo "  python : $(which python)"
echo "  API    : $NETBENCH_API_URL"
echo "  key    : ${NETBENCH_API_KEY:0:4}… (${#NETBENCH_API_KEY} characters)"
