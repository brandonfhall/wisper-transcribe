package com.wisper.discord;

import net.dv8tion.jda.api.audio.AudioReceiveHandler;
import net.dv8tion.jda.api.audio.CombinedAudio;
import net.dv8tion.jda.api.audio.UserAudio;
import org.jetbrains.annotations.NotNull;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.io.IOException;

/**
 * Per-user AudioReceiveHandler that forwards decoded 48 kHz stereo PCM
 * to the Python sidecar over the Unix socket.
 *
 * JDAVE decrypts DAVE packets transparently — by the time handleUserAudio
 * is called, audio is already decoded PCM.
 */
public class UserAudioDispatcher implements AudioReceiveHandler {

    private static final Logger log = LoggerFactory.getLogger(UserAudioDispatcher.class);

    private final SocketWriter socket;
    private final String userId;

    public UserAudioDispatcher(SocketWriter socket, String userId) {
        this.socket = socket;
        this.userId = userId;
    }

    @Override
    public void handleUserAudio(@NotNull UserAudio userAudio) {
        byte[] pcm = userAudio.getAudioData(1.0f); // 48 kHz stereo 16-bit PCM
        if (pcm.length == 0) return;
        try {
            socket.writeFrame(userId, pcm);
        } catch (IOException e) {
            log.warn("Failed to write audio frame for user {}: {}", userId, e.getMessage());
        }
    }

    /**
     * Forward the combined (mixed) audio track under the "__mixed__" user ID
     * so the Python side can archive it directly.
     */
    @Override
    public void handleCombinedAudio(@NotNull CombinedAudio combinedAudio) {
        byte[] pcm = combinedAudio.getAudioData(1.0f);
        if (pcm.length == 0) return;
        try {
            socket.writeFrame("__mixed__", pcm);
        } catch (IOException e) {
            log.warn("Failed to write mixed audio frame: {}", e.getMessage());
        }
    }

    @Override
    public boolean canReceiveCombined() {
        return true;
    }

    @Override
    public boolean canReceiveUser() {
        return true;
    }
}
