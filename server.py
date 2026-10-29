"""NetBench measurement server.

Runs two listeners on the same port number:
  UDP: echoes every datagram back unchanged (cyclic test)
  TCP: the first byte picks the test, request/response or bulk download

The client does all the timing, so the server needs no clock of its own.
"""
import argparse
import asyncio

from netbench import protocol as p


class UdpEcho(asyncio.DatagramProtocol):
    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.transport.sendto(data, addr)


async def handle_tcp(reader, writer):
    peer = writer.get_extra_info("peername")
    try:
        mode = await reader.readexactly(1)

        if mode == p.MODE_REQRESP:
            # Keep answering requests until the client closes the connection.
            while True:
                request = await reader.readexactly(p.REQ_SIZE)
                (resp_size,) = p.REQ_HEADER.unpack_from(request)
                writer.write(bytes(min(resp_size, p.MAX_RESP_SIZE)))
                await writer.drain()

        elif mode == p.MODE_BULK:
            (n_bytes,) = p.BULK_HEADER.unpack(await reader.readexactly(p.BULK_HEADER.size))
            n_bytes = min(n_bytes, p.MAX_BULK_BYTES)
            chunk = bytes(64 * 1024)
            sent = 0
            while sent < n_bytes:
                part = chunk[: n_bytes - sent]
                writer.write(part)
                await writer.drain()  # wait if the client/network can't keep up
                sent += len(part)

        else:
            print(f"{peer}: unknown mode {mode!r}, closing")
    except (asyncio.IncompleteReadError, ConnectionResetError):
        pass  # client closed the connection, the normal way a test ends
    finally:
        writer.close()


async def main(host: str, port: int):
    loop = asyncio.get_running_loop()
    udp, _ = await loop.create_datagram_endpoint(UdpEcho, local_addr=(host, port))
    tcp = await asyncio.start_server(handle_tcp, host, port)
    print(f"NetBench server on {host}:{port} (UDP echo + TCP)  Ctrl+C to stop")
    try:
        async with tcp:
            await tcp.serve_forever()
    finally:
        udp.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=9000)
    args = parser.parse_args()
    try:
        asyncio.run(main(args.host, args.port))
    except KeyboardInterrupt:
        pass