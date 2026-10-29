"""Wire format shared by client and server. Both sides must agree on these bytes."""
import struct

# --- UDP cyclic test ---
# seq number (uint32) + client send time in ns (uint64), network byte order.
UDP_HEADER = struct.Struct("!IQ")

# --- TCP tests ---
# The first byte on a new TCP connection says which test it is.
MODE_REQRESP = b"R"
MODE_BULK = b"B"

# Request/response: every request is exactly REQ_SIZE bytes and starts with
# the response size the client wants back (uint32).
REQ_SIZE = 128
REQ_HEADER = struct.Struct("!I")

# Bulk: the client sends how many bytes it wants (uint64), the server streams them.
BULK_HEADER = struct.Struct("!Q")

# Safety caps: the server will be on the public internet, so it must not
# send arbitrary amounts of data just because someone asked.
MAX_RESP_SIZE = 64 * 1024
MAX_BULK_BYTES = 100 * 1000 * 1000