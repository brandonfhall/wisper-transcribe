package com.wisper.discord;

import java.io.*;
import java.net.StandardProtocolFamily;
import java.net.UnixDomainSocketAddress;
import java.nio.ByteBuffer;
import java.nio.channels.SocketChannel;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;

/**
 * Thread-safe writer for the JDA → Python wire protocol over a Unix domain socket.
 *
 * Wire format (each frame):
 *   [4-byte BE user_id_len][user_id UTF-8 bytes][4-byte BE pcm_len][pcm bytes]
 *
 * Control frames use user_id = "__ctrl__" with a 4-byte LE close-code as the payload.
 * Mixed audio uses user_id = "__mixed__".
 */
public class SocketWriter implements AutoCloseable {

    private final SocketChannel channel;
    private final ByteBuffer lenBuf = ByteBuffer.allocate(4);

    public SocketWriter(Path socketPath) throws IOException {
        var address = UnixDomainSocketAddress.of(socketPath);
        channel = SocketChannel.open(StandardProtocolFamily.UNIX);
        channel.connect(address);
        channel.configureBlocking(true);
    }

    /**
     * Write one PCM frame to the socket.
     *
     * @param userId  Discord user ID (or "__ctrl__" / "__mixed__")
     * @param pcm     raw 48 kHz stereo 16-bit PCM bytes
     */
    public synchronized void writeFrame(String userId, byte[] pcm) throws IOException {
        byte[] userIdBytes = userId.getBytes(StandardCharsets.UTF_8);

        // user_id_len (4-byte big-endian)
        lenBuf.clear();
        lenBuf.putInt(userIdBytes.length);
        lenBuf.flip();
        writeFully(lenBuf);

        // user_id bytes
        writeFully(ByteBuffer.wrap(userIdBytes));

        // pcm_len (4-byte big-endian)
        lenBuf.clear();
        lenBuf.putInt(pcm.length);
        lenBuf.flip();
        writeFully(lenBuf);

        // pcm bytes
        writeFully(ByteBuffer.wrap(pcm));
    }

    /**
     * Write a disconnect control frame.
     */
    public synchronized void writeDisconnect(int closeCode) throws IOException {
        ByteBuffer buf = ByteBuffer.allocate(4);
        buf.order(java.nio.ByteOrder.LITTLE_ENDIAN);
        buf.putInt(closeCode);
        writeFrame("__ctrl__", buf.array());
    }

    private void writeFully(ByteBuffer buf) throws IOException {
        while (buf.hasRemaining()) {
            channel.write(buf);
        }
    }

    @Override
    public synchronized void close() {
        try {
            channel.close();
        } catch (IOException ignored) {
        }
    }
}
